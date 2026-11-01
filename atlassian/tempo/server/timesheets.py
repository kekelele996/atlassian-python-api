# coding=utf-8
"""
Tempo Server Timesheets API module.
"""

from ...request_utils import get_default_logger
from .base import TempoServerBase

log = get_default_logger(__name__)

# ---------------------------------------------------------------------------
# Month-end close outcomes (per-timesheet status reported back to the caller)
# ---------------------------------------------------------------------------
CLOSE_SUBMITTED = "submitted"
CLOSE_REJECTED = "rejected"
CLOSE_FAILED = "failed"
CLOSE_SKIPPED = "skipped"

# Rejection / failure reason codes. The three business rejection rules map
# 1:1 to the month-end review ("缺项目账号 / 没有工时 / 超出本周期预算").
REASON_MISSING_ACCOUNT = "missing_account"
REASON_NO_WORKLOGS = "no_worklogs"
REASON_BUDGET_EXCEEDED = "budget_exceeded"
# Consistency / infrastructure problems are never auto-rejected; they are
# reported as "failed" so a later retry only has to touch these timesheets.
REASON_WORKLOG_MISMATCH = "worklog_mismatch"
REASON_APPROVAL_MISMATCH = "approval_mismatch"
REASON_READ_ERROR = "read_error"
REASON_BUDGETS_UNAVAILABLE = "budgets_unavailable"

_SECONDS_PER_HOUR = 3600


def _first_present(mapping, keys, default=None):
    """Return the first non-empty value for ``keys`` in a dict-like object."""
    if isinstance(mapping, dict):
        for key in keys:
            value = mapping.get(key)
            if value not in (None, ""):
                return value
    return default


def _extract_entries(payload):
    """Extract a list of worklog/entry records from a timesheet payload."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("entries", "worklogs", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _extract_status(timesheet):
    """Read the raw approval status from a timesheet or approval record."""
    if not isinstance(timesheet, dict):
        return None
    status = _first_present(timesheet, ("status", "approvalStatus", "state"))
    if status is None and isinstance(timesheet.get("approval"), dict):
        status = _first_present(timesheet["approval"], ("status", "state"))
    return status


def normalize_approval_status(status):
    """
    Normalize the different status vocabularies Tempo/Jira may return into
    one of: ``open``, ``submitted``, ``approved`` or ``rejected``.
    """
    if status is None:
        return "open"
    text = str(status).strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return "open"
    if "reject" in text or "return" in text:
        return "rejected"
    if "submit" in text or "pending" in text or text in ("approval", "in_approval", "approving"):
        return "submitted"
    if "approv" in text and "not" not in text and "unapprov" not in text:
        return "approved"
    return "open"


def _entry_seconds(entry):
    """Return logged seconds for one worklog entry, tolerating field names."""
    value = _first_present(entry, ("timeSpentSeconds", "seconds", "durationSeconds"))
    if value is None:
        raw = entry.get("timeSpent")
        value = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
    try:
        return max(0, int(value)) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _ref_value(value, keys=("key", "id", "value", "name")):
    """Unwrap a reference that may be a plain value or a nested dict."""
    if isinstance(value, dict):
        value = _first_present(value, keys)
    if value in (None, ""):
        return None
    return str(value)


def _entry_account(entry, timesheet=None):
    """Project account of a worklog, falling back to timesheet level."""
    value = _ref_value(_first_present(entry, ("accountKey", "accountId", "account")))
    if value is None and isinstance(timesheet, dict):
        value = _ref_value(_first_present(timesheet, ("accountKey", "accountId", "account")))
    return value


def _entry_issue(entry):
    """Issue key/id referenced by one worklog entry."""
    return _ref_value(_first_present(entry, ("issueKey", "taskKey", "issueId", "issue")))


def _entry_date(entry):
    """Calendar date (``YYYY-MM-DD``) of one worklog entry, if present."""
    value = _first_present(entry, ("startDate", "workedDate", "date", "started"))
    if value is None:
        return None
    return str(value)[:10]


def _date_in_period(date, start, end):
    """Whether ``date`` falls into the closing period (inclusive)."""
    if not date:
        # Entries without a usable date cannot be excluded safely.
        return True
    if start and date < start:
        return False
    if end and date > end:
        return False
    return True


def _period_key(date):
    """Return the ``YYYY-MM`` period key for a ``YYYY-MM-DD`` date."""
    return str(date)[:7] if date else None


def _seconds_value(row, second_keys, hour_keys):
    """
    Read a duration from a budget row. Explicit second fields win; plain
    numeric fields (``amount``/``used`` ...) are interpreted as hours.
    """
    raw = _first_present(row, second_keys)
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    raw = _first_present(row, hour_keys)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw * _SECONDS_PER_HOUR)
    return None


def _budget_account(row):
    return _ref_value(_first_present(row, ("accountKey", "accountId", "account")))


def _budget_match(row, account, key, start):
    """Whether a budget row applies to ``account`` for the closing period."""
    if _budget_account(row) != account:
        return False
    row_key = _first_present(row, ("periodKey", "period", "month"))
    if isinstance(row_key, dict):
        row_key = None
    if isinstance(row_key, str) and row_key:
        return _period_key(row_key) == key or row_key == (start or "")
    row_start = _first_present(row, ("periodStart", "startDate", "dateFrom"))
    row_end = _first_present(row, ("periodEnd", "endDate", "dateTo"))
    row_start = str(row_start)[:10] if row_start else None
    row_end = str(row_end)[:10] if row_end else None
    if row_start is None and row_end is None:
        # Account-wide budget without period information.
        return True
    if start and row_end and start > row_end:
        return False
    if start and row_start and start < row_start:
        return False
    return True


class Timesheets(TempoServerBase):
    """
    Tempo Server Timesheets API client.

    Reference: https://www.tempo.io/server-api-documentation/timesheets
    """

    #: Validation reasons that cause a timesheet to be returned.
    BUSINESS_REJECT_REASONS = frozenset((REASON_MISSING_ACCOUNT, REASON_NO_WORKLOGS, REASON_BUDGET_EXCEEDED))

    def __init__(self, url, parent=None, *args, **kwargs):
        super(Timesheets, self).__init__(url, *args, **kwargs)
        self.parent = parent

    def get_timesheets(self, **kwargs):
        """Get all timesheets."""
        return self.parent.get("", **kwargs)

    def get_timesheet(self, timesheet_id, **kwargs):
        """Get timesheet by ID."""
        return self.parent.get(f"{timesheet_id}", **kwargs)

    def create_timesheet(self, data, **kwargs):
        """Create a new timesheet."""
        return self.parent.post("", data=data, **kwargs)

    def update_timesheet(self, timesheet_id, data, **kwargs):
        """Update an existing timesheet."""
        return self.parent.put(f"{timesheet_id}", data=data, **kwargs)

    def delete_timesheet(self, timesheet_id, **kwargs):
        """Delete a timesheet."""
        return self.parent.delete(f"{timesheet_id}", **kwargs)

    def get_timesheet_entries(self, timesheet_id, **kwargs):
        """Get timesheet entries."""
        return self.parent.get(f"{timesheet_id}/entries", **kwargs)

    def submit_timesheet(self, timesheet_id, **kwargs):
        """Submit a timesheet for approval."""
        return self.parent.post(f"{timesheet_id}/submit", **kwargs)

    def approve_timesheet(self, timesheet_id, **kwargs):
        """Approve a timesheet."""
        return self.parent.post(f"{timesheet_id}/approve", **kwargs)

    def reject_timesheet(self, timesheet_id, reason, **kwargs):
        """Reject a timesheet."""
        return self.parent.post(f"{timesheet_id}/reject", data={"reason": reason}, **kwargs)

    # ------------------------------------------------------------------
    # Month-end batch close ("关账入口")
    # ------------------------------------------------------------------
    def close_period(self, timesheet_ids, period=None, date_from=None, date_to=None, jira=None, budgets=None):
        """
        Review a batch of pending timesheets for month-end close.

        Every timesheet is checked independently against three business
        rules. Failing timesheets are returned (rejected) with explicit
        reasons; compliant ones are submitted for approval:

        1. ``missing_account``  - a worklog has no project account.
        2. ``no_worklogs``      - no logged hours inside the closing period.
        3. ``budget_exceeded``  - logged hours push the account over its
                                  period budget.

        The call never aborts on a single failure: items that error out are
        reported as ``failed`` while already completed items keep their
        state. Re-running the method is a safe resume - timesheets already
        submitted/approved/returned are read from the original timesheet
        and approval records and skipped, so nothing is submitted twice,
        and each open item is re-validated against fresh data.

        When a Jira Server client is provided, worklog totals are read from
        both Jira (``issue_get_worklog``) and Tempo for every touched issue;
        a mismatch (``worklog_mismatch``) fails the item instead of
        submitting it on inconsistent data. After every submit/reject the
        timesheet is re-read and the approval record must confirm the same
        result (``approval_mismatch`` otherwise).

        :param timesheet_ids: iterable of timesheet IDs to close.
        :param period: optional ``{"startDate": ..., "endDate": ...}`` dict
            or ``(start, end)`` tuple. Falls back to each timesheet's own
            period.
        :param date_from: optional explicit period start (inclusive).
        :param date_to: optional explicit period end (inclusive).
        :param jira: optional Jira Server client used to cross-check
            worklog totals against Tempo.
        :param budgets: optional list of budget rows. When omitted the
            budgets are read once through ``TempoServer.budgets``.
        :return: ``{"period": ..., "results": [...], "summary": {...}}``
            with one result per timesheet (input order) and aggregated
            counts. State changes stay on the server and remain visible via
            :meth:`get_timesheet` and the approval endpoints.
        """
        start, end = self._period_bounds(period, date_from, date_to, None)

        if budgets is None:
            try:
                budgets = self.parent.budgets.get_budgets() or []
            except Exception as exc:  # fail closed: budget rule must run
                log.error("Unable to read period budgets: %s", exc)
                budgets = None

        results = []
        for timesheet_id in timesheet_ids:
            try:
                results.append(self._close_one(timesheet_id, start, end, jira, budgets))
            except Exception as exc:  # never let one item poison the batch
                log.exception("Unexpected failure while closing timesheet %s", timesheet_id)
                results.append(self._item(timesheet_id, CLOSE_FAILED, [REASON_READ_ERROR], [str(exc)]))

        summary = {"total": len(results)}
        for outcome in (CLOSE_SUBMITTED, CLOSE_REJECTED, CLOSE_FAILED, CLOSE_SKIPPED):
            summary[outcome] = sum(1 for item in results if item["status"] == outcome)
        summary["completed"] = summary[CLOSE_SUBMITTED] + summary[CLOSE_REJECTED] + summary[CLOSE_SKIPPED]
        return {
            "period": {"start": start, "end": end},
            "results": results,
            "summary": summary,
        }

    def _close_one(self, timesheet_id, start, end, jira, budgets):
        """Validate and act on a single timesheet, returning a result dict."""
        timesheet = self.get_timesheet(timesheet_id)
        state = normalize_approval_status(_extract_status(timesheet))
        raw_status = _extract_status(timesheet)

        if state == "approved":
            return self._item(
                timesheet_id,
                CLOSE_SKIPPED,
                detail={"approval_status": raw_status, "skip_reason": "already_approved"},
            )
        if state == "submitted":
            # The approval record already shows an in-flight submission:
            # posting again would create a duplicate submission.
            return self._item(
                timesheet_id,
                CLOSE_SKIPPED,
                detail={"approval_status": raw_status, "skip_reason": "already_submitted"},
            )

        item_start, item_end = self._period_bounds(None, start, end, timesheet)
        reasons, messages, validation = self._validate(timesheet, timesheet_id, item_start, item_end, jira, budgets)
        reject_reasons = [reason for reason in reasons if reason in self.BUSINESS_REJECT_REASONS]
        blocker_reasons = [reason for reason in reasons if reason not in self.BUSINESS_REJECT_REASONS]

        if blocker_reasons:
            # Consistency/infra problems must not auto-return a timesheet;
            # leave it untouched so a later retry picks it up.
            detail = {"approval_status": raw_status}
            detail.update(validation)
            return self._item(timesheet_id, CLOSE_FAILED, reasons, messages, detail=detail)

        if state == "rejected" and reject_reasons:
            # Already returned for the same kind of problem on an earlier
            # run: keep the existing rejection, do not POST it again.
            detail = {"approval_status": raw_status, "skip_reason": "already_rejected"}
            detail.update(validation)
            return self._item(timesheet_id, CLOSE_SKIPPED, reasons, messages, detail=detail)

        if reject_reasons:
            return self._reject(timesheet_id, reject_reasons, messages, validation)
        return self._submit(timesheet_id, validation)

    def _validate(self, timesheet, timesheet_id, start, end, jira, budgets):
        """Run the business rules and the Jira/Tempo consistency checks."""
        reasons = []
        messages = []
        detail = {"period": {"start": start, "end": end}}

        entries = _extract_entries(timesheet)
        if not entries:
            entries = _extract_entries(self.get_timesheet_entries(timesheet_id))
        detail["entries"] = len(entries)

        period_entries = [
            entry for entry in entries if isinstance(entry, dict) and _date_in_period(_entry_date(entry), start, end)
        ]
        total_seconds = sum(_entry_seconds(entry) for entry in period_entries)
        detail["total_seconds"] = total_seconds

        if total_seconds <= 0:
            reasons.append(REASON_NO_WORKLOGS)
            messages.append("Timesheet has no worklogs within the closing period")
            return reasons, messages, detail

        # Rule 1: every logged entry needs a project account.
        missing_account_issues = []
        account_seconds = {}
        issue_seconds = {}
        for entry in period_entries:
            seconds = _entry_seconds(entry)
            if seconds <= 0:
                continue
            issue = _entry_issue(entry)
            if issue:
                issue_seconds[issue] = issue_seconds.get(issue, 0) + seconds
            account = _entry_account(entry, timesheet)
            if not account:
                missing_account_issues.append(issue or "entry#%s" % entry.get("id", "?"))
            else:
                account_seconds[account] = account_seconds.get(account, 0) + seconds

        if missing_account_issues:
            reasons.append(REASON_MISSING_ACCOUNT)
            detail["missing_account_entries"] = missing_account_issues
            messages.append(
                "Worklog entries without a project account: %s"
                % ", ".join(str(item) for item in missing_account_issues)
            )
        detail["issue_seconds"] = issue_seconds
        detail["account_seconds"] = account_seconds

        # Rule 2: account period budgets.
        if budgets is None:
            reasons.append(REASON_BUDGETS_UNAVAILABLE)
            messages.append("Period budgets could not be read, so the budget check cannot run")
        else:
            exceeded = []
            for account, seconds in sorted(account_seconds.items()):
                budget = next(
                    (
                        row
                        for row in budgets
                        if isinstance(row, dict) and _budget_match(row, account, _period_key(start), start)
                    ),
                    None,
                )
                if not budget:
                    continue
                amount = _seconds_value(
                    budget,
                    ("amountSeconds", "budgetedSeconds", "amountInSeconds", "limitSeconds"),
                    ("amount", "budget", "limit", "hours", "budgetedHours"),
                )
                used = _seconds_value(
                    budget,
                    ("usedSeconds", "spentSeconds", "consumedSeconds", "bookedSeconds"),
                    ("used", "spent", "consumed", "booked"),
                )
                projected = used if used is not None else seconds
                if amount is not None and projected > amount:
                    exceeded.append(
                        {
                            "account": account,
                            "budget_seconds": amount,
                            "used_seconds": projected,
                        }
                    )
            if exceeded:
                reasons.append(REASON_BUDGET_EXCEEDED)
                detail["budget_exceeded"] = exceeded
                messages.extend(
                    "Period budget exceeded for account %s: %sh used of %sh"
                    % (
                        item["account"],
                        item["used_seconds"] / _SECONDS_PER_HOUR,
                        item["budget_seconds"] / _SECONDS_PER_HOUR,
                    )
                    for item in exceeded
                )

        # Consistency rule: hours read from Jira and Tempo must be identical.
        if jira is not None:
            mismatches = []
            for issue in sorted(issue_seconds):
                jira_seconds = self._jira_issue_seconds(jira, issue, start, end)
                if jira_seconds != issue_seconds[issue]:
                    mismatches.append(
                        {
                            "issue": issue,
                            "tempo_seconds": issue_seconds[issue],
                            "jira_seconds": jira_seconds,
                        }
                    )
            if mismatches:
                reasons.append(REASON_WORKLOG_MISMATCH)
                detail["worklog_mismatches"] = mismatches
                messages.extend(
                    "Worklog total for %s differs: Tempo %ss vs Jira %ss"
                    % (item["issue"], item["tempo_seconds"], item["jira_seconds"])
                    for item in mismatches
                )

        return reasons, messages, detail

    def _jira_issue_seconds(self, jira, issue, start, end):
        """Sum in-period logged seconds for an issue as read from Jira."""
        response = jira.issue_get_worklog(issue)
        worklogs = response.get("worklogs") if isinstance(response, dict) else response
        if not isinstance(worklogs, list):
            worklogs = []
        return sum(
            _entry_seconds(worklog)
            for worklog in worklogs
            if isinstance(worklog, dict) and _date_in_period(_entry_date(worklog), start, end)
        )

    def _submit(self, timesheet_id, validation):
        """POST submit and verify the result from the approval record."""
        try:
            approval_record = self.submit_timesheet(timesheet_id)
        except Exception as exc:
            detail = dict(validation)
            detail["error"] = str(exc)
            return self._item(
                timesheet_id,
                CLOSE_FAILED,
                [REASON_READ_ERROR],
                ["Submit for approval failed: %s" % exc],
                action="submit",
                detail=detail,
            )

        verified = self._verify_approval(timesheet_id, approval_record, ("submitted", "approved"))
        if verified is not None:
            return verified

        detail = dict(validation)
        detail["approval_record"] = approval_record
        return self._item(
            timesheet_id,
            CLOSE_SUBMITTED,
            action="submit",
            messages=["Timesheet submitted for approval"],
            detail=detail,
        )

    def _reject(self, timesheet_id, reasons, messages, validation):
        """POST reject and verify the timesheet reads as rejected afterwards."""
        reason_text = "month-end close: %s" % ", ".join(reasons)
        try:
            approval_record = self.reject_timesheet(timesheet_id, reason_text)
        except Exception as exc:
            detail = dict(validation)
            detail["error"] = str(exc)
            return self._item(
                timesheet_id,
                CLOSE_FAILED,
                reasons,
                messages + ["Return failed: %s" % exc],
                action="reject",
                detail=detail,
            )

        verified = self._verify_approval(timesheet_id, approval_record, ("rejected",))
        if verified is not None:
            return verified

        detail = dict(validation)
        detail["approval_record"] = approval_record
        return self._item(
            timesheet_id,
            CLOSE_REJECTED,
            reasons,
            messages + ["Timesheet returned to its owner"],
            action="reject",
            detail=detail,
        )

    def _verify_approval(self, timesheet_id, approval_record, expected_states):
        """
        Re-read the timesheet after an action and require the approval
        record and the timesheet itself to report the same outcome.

        :return: a failed result dict when verification disagrees, else None.
        """
        record_state = None
        if isinstance(approval_record, dict):
            record_state = normalize_approval_status(_extract_status(approval_record))

        try:
            refreshed = self.get_timesheet(timesheet_id)
        except Exception as exc:
            return self._item(
                timesheet_id,
                CLOSE_FAILED,
                [REASON_APPROVAL_MISMATCH],
                ["Approval result could not be re-read after the action: %s" % exc],
                detail={"error": str(exc), "approval_record": approval_record},
            )

        current_state = normalize_approval_status(_extract_status(refreshed))
        if current_state not in expected_states:
            return self._item(
                timesheet_id,
                CLOSE_FAILED,
                [REASON_APPROVAL_MISMATCH],
                [
                    "Approval record expected %s but the timesheet now reads %s"
                    % (" or ".join(expected_states), current_state)
                ],
                detail={
                    "expected": list(expected_states),
                    "approval_status": _extract_status(refreshed),
                    "approval_record": approval_record,
                },
            )
        if record_state == "rejected" and "rejected" not in expected_states:
            return self._item(
                timesheet_id,
                CLOSE_FAILED,
                [REASON_APPROVAL_MISMATCH],
                ["Approval record reports 'rejected' although submission succeeded"],
                detail={"approval_record": approval_record},
            )
        return None

    @staticmethod
    def _period_bounds(period, date_from, date_to, timesheet):
        """Resolve the closing period bounds from all supported inputs."""
        start = str(date_from)[:10] if date_from else None
        end = str(date_to)[:10] if date_to else None
        if isinstance(period, dict):
            start = start or str(_first_present(period, ("startDate", "start", "dateFrom")) or "")[:10] or None
            end = end or str(_first_present(period, ("endDate", "end", "dateTo")) or "")[:10] or None
        elif isinstance(period, (list, tuple)) and len(period) == 2:
            start = start or (str(period[0])[:10] if period[0] else None)
            end = end or (str(period[1])[:10] if period[1] else None)

        if (start is None or end is None) and isinstance(timesheet, dict):
            period_obj = timesheet.get("period")
            start = (
                start
                or str(
                    _first_present(
                        period_obj if isinstance(period_obj, dict) else {}, ("startDate", "start", "dateFrom")
                    )
                    or ""
                )[:10]
                or None
            )
            end = (
                end
                or str(
                    _first_present(period_obj if isinstance(period_obj, dict) else {}, ("endDate", "end", "dateTo"))
                    or ""
                )[:10]
                or None
            )
            start = start or str(_first_present(timesheet, ("startDate", "periodStart", "dateFrom")) or "")[:10] or None
            end = end or str(_first_present(timesheet, ("endDate", "periodEnd", "dateTo")) or "")[:10] or None
        return start, end

    @staticmethod
    def _item(timesheet_id, status, reasons=None, messages=None, action=None, detail=None):
        return {
            "timesheet_id": timesheet_id,
            "status": status,
            "reasons": reasons or [],
            "messages": messages or [],
            "action": action,
            "detail": detail or {},
        }
