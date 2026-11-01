# coding=utf-8
"""
Test cases for Tempo Server API clients using pytest.
"""

import copy

import pytest
from unittest.mock import patch
from requests import HTTPError

from atlassian.tempo import TempoServer
from atlassian.tempo.server.timesheets import (
    CLOSE_FAILED,
    CLOSE_REJECTED,
    CLOSE_SKIPPED,
    CLOSE_SUBMITTED,
    REASON_APPROVAL_MISMATCH,
    REASON_BUDGET_EXCEEDED,
    REASON_BUDGETS_UNAVAILABLE,
    REASON_MISSING_ACCOUNT,
    REASON_NO_WORKLOGS,
    REASON_WORKLOG_MISMATCH,
)


@pytest.fixture
def tempo_server():
    """Fixture for TempoServer client."""
    return TempoServer(url="https://test.atlassian.net", token="test-token", cloud=False)


class TestTempoServer:
    """Test cases for base TempoServer client."""

    def test_init_defaults(self):
        """Test TempoServer client initialization with default values."""
        tempo = TempoServer(url="https://test.atlassian.net", token="test-token")
        assert tempo.api_version == "1"
        assert tempo.api_root == "rest/tempo-core/1"

    def test_init_custom_values(self):
        """Test TempoServer client initialization with custom values."""
        tempo = TempoServer(
            url="https://test.atlassian.net", token="test-token", api_version="2", api_root="custom/api/root"
        )
        assert tempo.api_version == "2"
        assert tempo.api_root == "custom/api/root"

    def test_specialized_modules_exist(self, tempo_server):
        """Test that specialized modules are properly initialized."""
        assert tempo_server.accounts is not None
        assert tempo_server.teams is not None
        assert tempo_server.planner is not None
        assert tempo_server.budgets is not None
        assert tempo_server.timesheets is not None
        assert tempo_server.servlet is not None
        assert tempo_server.events is not None

    @patch.object(TempoServer, "get")
    def test_get_health(self, mock_get, tempo_server):
        """Test get_health method."""
        mock_get.return_value = {"status": "healthy"}
        result = tempo_server.get_health()
        mock_get.assert_called_once_with("health", **{})
        assert result == {"status": "healthy"}

    @patch.object(TempoServer, "get")
    def test_get_metadata(self, mock_get, tempo_server):
        """Test get_metadata method."""
        mock_get.return_value = {"version": "1.0.0"}
        result = tempo_server.get_metadata()
        mock_get.assert_called_once_with("metadata", **{})
        assert result == {"version": "1.0.0"}


class TestTempoServerAccounts:
    """Test cases for TempoServer accounts module."""

    @pytest.fixture
    def tempo_server(self):
        """Fixture for TempoServer client."""
        return TempoServer(url="https://test.atlassian.net", token="test-token")

    @patch.object(TempoServer, "get")
    def test_get_accounts(self, mock_get, tempo_server):
        """Test get_accounts method."""
        mock_get.return_value = [{"id": 1, "name": "Test Account"}]
        result = tempo_server.accounts.get_accounts()
        mock_get.assert_called_once_with("", **{})
        assert result == [{"id": 1, "name": "Test Account"}]

    @patch.object(TempoServer, "get")
    def test_get_account(self, mock_get, tempo_server):
        """Test get_account method."""
        mock_get.return_value = {"id": 1, "name": "Test Account"}
        result = tempo_server.accounts.get_account(1)
        mock_get.assert_called_once_with("1", **{})
        assert result == {"id": 1, "name": "Test Account"}


class TestTempoServerTeams:
    """Test cases for TempoServer teams module."""

    @pytest.fixture
    def tempo_server(self):
        """Fixture for TempoServer client."""
        return TempoServer(url="https://test.atlassian.net", token="test-token")

    @patch.object(TempoServer, "get")
    def test_get_teams(self, mock_get, tempo_server):
        """Test get_teams method."""
        mock_get.return_value = [{"id": 1, "name": "Test Team"}]
        result = tempo_server.teams.get_teams()
        mock_get.assert_called_once_with("", **{})
        assert result == [{"id": 1, "name": "Test Team"}]

    @patch.object(TempoServer, "get")
    def test_get_team(self, mock_get, tempo_server):
        """Test get_team method."""
        mock_get.return_value = {"id": 1, "name": "Test Team"}
        result = tempo_server.teams.get_team(1)
        mock_get.assert_called_once_with("1", **{})
        assert result == {"id": 1, "name": "Test Team"}


class TestTempoServerPlanner:
    """Test cases for TempoServer planner module."""

    @pytest.fixture
    def tempo_server(self):
        """Fixture for TempoServer client."""
        return TempoServer(url="https://test.atlassian.net", token="test-token")

    @patch.object(TempoServer, "get")
    def test_get_plans(self, mock_get, tempo_server):
        """Test get_plans method."""
        mock_get.return_value = [{"id": 1, "name": "Test Plan"}]
        result = tempo_server.planner.get_plans()
        mock_get.assert_called_once_with("", **{})
        assert result == [{"id": 1, "name": "Test Plan"}]

    @patch.object(TempoServer, "get")
    def test_get_plan(self, mock_get, tempo_server):
        """Test get_plan method."""
        mock_get.return_value = {"id": 1, "name": "Test Plan"}
        result = tempo_server.planner.get_plan(1)
        mock_get.assert_called_once_with("1", **{})
        assert result == {"id": 1, "name": "Test Plan"}


class TestTempoServerBudgets:
    """Test cases for TempoServer budgets module."""

    @pytest.fixture
    def tempo_server(self):
        """Fixture for TempoServer client."""
        return TempoServer(url="https://test.atlassian.net", token="test-token")

    @patch.object(TempoServer, "get")
    def test_get_budgets(self, mock_get, tempo_server):
        """Test get_budgets method."""
        mock_get.return_value = [{"id": 1, "name": "Test Budget"}]
        result = tempo_server.budgets.get_budgets()
        mock_get.assert_called_once_with("", **{})
        assert result == [{"id": 1, "name": "Test Budget"}]

    @patch.object(TempoServer, "get")
    def test_get_budget(self, mock_get, tempo_server):
        """Test get_budget method."""
        mock_get.return_value = {"id": 1, "name": "Test Budget"}
        result = tempo_server.budgets.get_budget(1)
        mock_get.assert_called_once_with("1", **{})
        assert result == {"id": 1, "name": "Test Budget"}


class TestTempoServerTimesheets:
    """Test cases for TempoServer timesheets module."""

    @pytest.fixture
    def tempo_server(self):
        """Fixture for TempoServer client."""
        return TempoServer(url="https://test.atlassian.net", token="test-token")

    @patch.object(TempoServer, "get")
    def test_get_timesheets(self, mock_get, tempo_server):
        """Test get_timesheets method."""
        mock_get.return_value = [{"id": 1, "name": "Test Timesheet"}]
        result = tempo_server.timesheets.get_timesheets()
        mock_get.assert_called_once_with("", **{})
        assert result == [{"id": 1, "name": "Test Timesheet"}]

    @patch.object(TempoServer, "get")
    def test_get_timesheet(self, mock_get, tempo_server):
        """Test get_timesheet method."""
        mock_get.return_value = {"id": 1, "name": "Test Timesheet"}
        result = tempo_server.timesheets.get_timesheet(1)
        mock_get.assert_called_once_with("1", **{})
        assert result == {"id": 1, "name": "Test Timesheet"}


class TestTempoServerServlet:
    """Test cases for TempoServer servlet module."""

    @pytest.fixture
    def tempo_server(self):
        """Fixture for TempoServer client."""
        return TempoServer(url="https://test.atlassian.net", token="test-token")

    @patch.object(TempoServer, "get")
    def test_get_worklogs(self, mock_get, tempo_server):
        """Test get_worklogs method."""
        mock_get.return_value = [{"id": 1, "timeSpentSeconds": 3600}]
        result = tempo_server.servlet.get_worklogs()
        mock_get.assert_called_once_with("", **{})
        assert result == [{"id": 1, "timeSpentSeconds": 3600}]

    @patch.object(TempoServer, "get")
    def test_get_worklog(self, mock_get, tempo_server):
        """Test get_worklog method."""
        mock_get.return_value = {"id": 1, "timeSpentSeconds": 3600}
        result = tempo_server.servlet.get_worklog(1)
        mock_get.assert_called_once_with("1", **{})
        assert result == {"id": 1, "timeSpentSeconds": 3600}


class TestTempoServerEvents:
    """Test cases for TempoServer events module."""

    @pytest.fixture
    def tempo_server(self):
        """Fixture for TempoServer client."""
        return TempoServer(url="https://test.atlassian.net", token="test-token")

    @patch.object(TempoServer, "get")
    def test_get_events(self, mock_get, tempo_server):
        """Test get_events method."""
        mock_get.return_value = [{"id": 1, "type": "worklog_created"}]
        result = tempo_server.events.get_events()
        mock_get.assert_called_once_with("", **{})
        assert result == [{"id": 1, "type": "worklog_created"}]

    @patch.object(TempoServer, "get")
    def test_get_event(self, mock_get, tempo_server):
        """Test get_event method."""
        mock_get.return_value = {"id": 1, "type": "worklog_created"}
        result = tempo_server.events.get_event(1)
        mock_get.assert_called_once_with("1", **{})
        assert result == {"id": 1, "type": "worklog_created"}


# ---------------------------------------------------------------------------
# Month-end batch close tests
# ---------------------------------------------------------------------------
PERIOD = {"startDate": "2024-01-01", "endDate": "2024-01-31"}

DEFAULT_BUDGETS = [{"id": "b1", "accountKey": "ACC-1", "periodKey": "2024-01", "amount": 10, "used": 4}]


def make_entry(issue, seconds, account="ACC-1", date="2024-01-10"):
    return {
        "id": "%s-%s-%s" % (issue, date, seconds),
        "issueKey": issue,
        "timeSpentSeconds": seconds,
        "accountKey": account,
        "startDate": date,
    }


def make_timesheet(timesheet_id, status="OPEN", entries=(), include_entries=True):
    timesheet = {"id": timesheet_id, "status": status, "period": dict(PERIOD)}
    if include_entries:
        timesheet["entries"] = list(entries)
    return timesheet


class FakeJira:
    """Minimal Jira Server stand-in answering issue_get_worklog."""

    def __init__(self, worklogs=None, fail=()):
        self.worklogs = worklogs or {}
        self.fail = set(fail)
        self.calls = []

    def issue_get_worklog(self, issue):
        self.calls.append(issue)
        if issue in self.fail:
            raise HTTPError("jira unavailable")
        return {"worklogs": copy.deepcopy(self.worklogs.get(issue, []))}


class CloseEnvironment:
    """Patches a TempoServer instance with in-memory timesheet state."""

    def __init__(self, records):
        self.tempo = TempoServer(url="https://test.example.com", token="test-token")
        self.records = {record["id"]: record for record in records}
        self.submit_failures = set()
        self.reject_failures = set()
        # IDs for which the approval state is deliberately NOT moved after
        # the submit/reject call (to simulate inconsistent server records).
        self.submit_no_state_flip = set()
        self.reject_no_state_flip = set()
        self.submitted = []
        self.rejected = []
        self._patches = []

    def __enter__(self):
        self.submit_mock = patch.object(self.tempo.timesheets, "submit_timesheet", side_effect=self._submit).start()
        self.reject_mock = patch.object(self.tempo.timesheets, "reject_timesheet", side_effect=self._reject).start()
        self.get_mock = patch.object(self.tempo.timesheets, "get_timesheet", side_effect=self._get_timesheet).start()
        self.entries_mock = patch.object(self.tempo.timesheets, "get_timesheet_entries", return_value=[]).start()
        self.budgets_mock = patch.object(self.tempo.budgets, "get_budgets", return_value=list(DEFAULT_BUDGETS)).start()
        self._patches = [
            self.submit_mock,
            self.reject_mock,
            self.get_mock,
            self.entries_mock,
            self.budgets_mock,
        ]
        return self

    def __exit__(self, *exc):
        patch.stopall()
        return False

    def _get_timesheet(self, timesheet_id):
        return copy.deepcopy(self.records[timesheet_id])

    def _submit(self, timesheet_id):
        if timesheet_id in self.submit_failures:
            raise HTTPError("submit failed")
        self.submitted.append(timesheet_id)
        if timesheet_id not in self.submit_no_state_flip:
            self.records[timesheet_id]["status"] = "SUBMITTED"
        return {"id": timesheet_id, "status": "SUBMITTED"}

    def _reject(self, timesheet_id, reason):
        if timesheet_id in self.reject_failures:
            raise HTTPError("reject failed")
        self.rejected.append((timesheet_id, reason))
        if timesheet_id not in self.reject_no_state_flip:
            self.records[timesheet_id]["status"] = "REJECTED"
        return {"id": timesheet_id, "status": "REJECTED", "reason": reason}

    def run(self, timesheet_ids, jira=None, **kwargs):
        return self.tempo.timesheets.close_period(list(timesheet_ids), jira=jira, **kwargs)


def result_by_id(batch_result):
    return {item["timesheet_id"]: item for item in batch_result["results"]}


class TestTempoServerTimesheetClosePeriod:
    """Batch month-end close behaviour."""

    def test_compliant_timesheets_are_submitted(self):
        records = [
            make_timesheet(1, entries=[make_entry("DEV-1", 3600)]),
            make_timesheet(2, entries=[make_entry("DEV-2", 7200)]),
        ]
        jira = FakeJira({"DEV-1": [make_entry("DEV-1", 3600)], "DEV-2": [make_entry("DEV-2", 7200)]})
        with CloseEnvironment(records) as env:
            batch = env.run([1, 2], jira=jira)

        results = result_by_id(batch)
        assert results[1]["status"] == CLOSE_SUBMITTED
        assert results[2]["status"] == CLOSE_SUBMITTED
        assert env.submitted == [1, 2]
        assert env.rejected == []
        # The outcome stays visible on the original timesheet records.
        assert env.records[1]["status"] == "SUBMITTED"
        assert batch["summary"] == {
            "total": 2,
            "submitted": 2,
            "rejected": 0,
            "failed": 0,
            "skipped": 0,
            "completed": 2,
        }

    def test_missing_project_account_is_returned(self):
        records = [
            make_timesheet(1, entries=[make_entry("DEV-1", 3600, account=None)]),
            make_timesheet(2, entries=[make_entry("DEV-2", 3600)]),
        ]
        jira = FakeJira({"DEV-1": [make_entry("DEV-1", 3600)], "DEV-2": [make_entry("DEV-2", 3600)]})
        with CloseEnvironment(records) as env:
            batch = env.run([1, 2], jira=jira)

        results = result_by_id(batch)
        assert results[1]["status"] == CLOSE_REJECTED
        assert results[1]["reasons"] == [REASON_MISSING_ACCOUNT]
        assert results[1]["action"] == "reject"
        assert results[1]["messages"]
        assert env.rejected[0][0] == 1
        assert REASON_MISSING_ACCOUNT in env.rejected[0][1]
        assert env.records[1]["status"] == "REJECTED"
        assert results[2]["status"] == CLOSE_SUBMITTED

    def test_no_worklogs_is_returned(self):
        records = [
            make_timesheet(1, entries=[]),
            # Entries outside the closing period also mean "no hours this period".
            make_timesheet(2, entries=[make_entry("DEV-2", 3600, date="2024-02-02")]),
            make_timesheet(3, entries=[make_entry("DEV-3", 3600)]),
        ]
        jira = FakeJira({"DEV-3": [make_entry("DEV-3", 3600)]})
        with CloseEnvironment(records) as env:
            batch = env.run([1, 2, 3], jira=jira)

        results = result_by_id(batch)
        assert results[1]["reasons"] == [REASON_NO_WORKLOGS]
        assert results[1]["status"] == CLOSE_REJECTED
        assert results[2]["reasons"] == [REASON_NO_WORKLOGS]
        assert results[3]["status"] == CLOSE_SUBMITTED
        assert [timesheet_id for timesheet_id, _ in env.rejected] == [1, 2]

    def test_entries_fetched_when_not_embedded(self):
        records = [make_timesheet(1, include_entries=False)]
        jira = FakeJira({"DEV-1": [make_entry("DEV-1", 3600)]})
        with CloseEnvironment(records) as env:
            env.entries_mock.return_value = [make_entry("DEV-1", 3600)]
            batch = env.run([1], jira=jira)

        env.entries_mock.assert_called_once_with(1)
        assert result_by_id(batch)[1]["status"] == CLOSE_SUBMITTED

    def test_period_budget_exceeded_is_returned(self):
        budgets = [{"id": "b1", "accountKey": "ACC-1", "periodKey": "2024-01", "amount": 2}]
        records = [
            make_timesheet(1, entries=[make_entry("DEV-1", 3 * 3600)]),
            make_timesheet(2, entries=[make_entry("DEV-2", 3600)]),
        ]
        jira = FakeJira({"DEV-1": [make_entry("DEV-1", 3 * 3600)], "DEV-2": [make_entry("DEV-2", 3600)]})
        with CloseEnvironment(records) as env:
            env.budgets_mock.return_value = budgets
            batch = env.run([1, 2], jira=jira)

        results = result_by_id(batch)
        assert results[1]["status"] == CLOSE_REJECTED
        assert results[1]["reasons"] == [REASON_BUDGET_EXCEEDED]
        assert results[1]["detail"]["budget_exceeded"][0]["budget_seconds"] == 2 * 3600
        assert results[1]["detail"]["budget_exceeded"][0]["used_seconds"] == 3 * 3600
        assert results[2]["status"] == CLOSE_SUBMITTED

    def test_mid_batch_failure_keeps_completed_state_and_continues(self):
        records = [make_timesheet(i, entries=[make_entry("DEV-%d" % i, 3600)]) for i in range(1, 8)]
        jira = FakeJira({"DEV-%d" % i: [make_entry("DEV-%d" % i, 3600)] for i in range(1, 8)})
        with CloseEnvironment(records) as env:
            env.submit_failures = {5}
            batch = env.run(list(range(1, 8)), jira=jira)

        results = result_by_id(batch)
        assert results[5]["status"] == CLOSE_FAILED
        assert results[5]["action"] == "submit"
        assert "submit failed" in results[5]["detail"]["error"]
        # Earlier items keep their submitted state; later items are processed.
        assert [i for i in range(1, 8) if results[i]["status"] == CLOSE_SUBMITTED] == [1, 2, 3, 4, 6, 7]
        assert env.submitted == [1, 2, 3, 4, 6, 7]
        assert batch["summary"]["failed"] == 1
        assert batch["summary"]["submitted"] == 6

    def test_retry_skips_completed_and_only_processes_open_item(self):
        records = [make_timesheet(i, entries=[make_entry("DEV-%d" % i, 3600)]) for i in range(1, 8)]
        jira = FakeJira({"DEV-%d" % i: [make_entry("DEV-%d" % i, 3600)] for i in range(1, 8)})
        with CloseEnvironment(records) as env:
            env.submit_failures = {5}
            env.run(list(range(1, 8)), jira=jira)

            # Retry: only timesheet 5 is still open; the rest are already submitted.
            env.submit_failures.clear()
            env.submit_mock.reset_mock()
            retry_batch = env.run(list(range(1, 8)), jira=jira)

        results = result_by_id(retry_batch)
        assert results[5]["status"] == CLOSE_SUBMITTED
        assert [i for i in range(1, 8) if results[i]["status"] == CLOSE_SKIPPED] == [1, 2, 3, 4, 6, 7]
        assert all(results[i]["detail"]["skip_reason"] == "already_submitted" for i in (1, 2, 3, 4, 6, 7))
        # No duplicate submissions for the already completed timesheets.
        env.submit_mock.assert_called_once_with(5)
        assert retry_batch["summary"] == {
            "total": 7,
            "submitted": 1,
            "rejected": 0,
            "failed": 0,
            "skipped": 6,
            "completed": 7,
        }

    def test_retry_revalidates_unfinished_item_against_fresh_data(self):
        records = [
            make_timesheet(1, entries=[make_entry("DEV-1", 3600)]),
            make_timesheet(2, entries=[make_entry("DEV-2", 3600)]),
        ]
        jira = FakeJira({"DEV-1": [make_entry("DEV-1", 3600)], "DEV-2": [make_entry("DEV-2", 3600)]})
        with CloseEnvironment(records) as env:
            env.submit_failures = {2}
            env.run([1, 2], jira=jira)

            # Before retry the owner's worklogs were removed: still unfinished
            # but no longer compliant, so it must be returned instead.
            env.records[2]["entries"] = []
            env.submit_failures.clear()
            retry_batch = env.run([1, 2], jira=FakeJira({"DEV-1": [make_entry("DEV-1", 3600)]}))

        results = result_by_id(retry_batch)
        assert results[1]["status"] == CLOSE_SKIPPED
        assert results[2]["status"] == CLOSE_REJECTED
        assert results[2]["reasons"] == [REASON_NO_WORKLOGS]
        assert 2 not in env.submitted

    def test_already_submitted_or_approved_are_not_posted_again(self):
        records = [
            make_timesheet(1, status="SUBMITTED", entries=[make_entry("DEV-1", 3600)]),
            make_timesheet(2, status="APPROVED", entries=[make_entry("DEV-2", 3600)]),
        ]
        with CloseEnvironment(records) as env:
            batch = env.run([1, 2], jira=FakeJira())

        results = result_by_id(batch)
        assert results[1]["status"] == CLOSE_SKIPPED
        assert results[1]["detail"]["skip_reason"] == "already_submitted"
        assert results[2]["detail"]["skip_reason"] == "already_approved"
        env.submit_mock.assert_not_called()
        env.reject_mock.assert_not_called()

    def test_already_rejected_invalid_is_kept_rejected_valid_resubmits(self):
        records = [
            make_timesheet(1, status="REJECTED", entries=[make_entry("DEV-1", 3600, account=None)]),
            make_timesheet(2, status="REJECTED", entries=[make_entry("DEV-2", 3600)]),
        ]
        jira = FakeJira({"DEV-1": [make_entry("DEV-1", 3600)], "DEV-2": [make_entry("DEV-2", 3600)]})
        with CloseEnvironment(records) as env:
            batch = env.run([1, 2], jira=jira)

        results = result_by_id(batch)
        assert results[1]["status"] == CLOSE_SKIPPED
        assert results[1]["detail"]["skip_reason"] == "already_rejected"
        assert results[1]["reasons"] == [REASON_MISSING_ACCOUNT]
        assert results[2]["status"] == CLOSE_SUBMITTED
        env.reject_mock.assert_not_called()
        env.submit_mock.assert_called_once_with(2)

    def test_jira_tempo_worklog_mismatch_fails_without_mutation(self):
        records = [make_timesheet(1, entries=[make_entry("DEV-1", 2 * 3600)])]
        jira = FakeJira({"DEV-1": [make_entry("DEV-1", 3600)]})
        with CloseEnvironment(records) as env:
            batch = env.run([1], jira=jira)

        item = result_by_id(batch)[1]
        assert item["status"] == CLOSE_FAILED
        assert item["reasons"] == [REASON_WORKLOG_MISMATCH]
        mismatch = item["detail"]["worklog_mismatches"][0]
        assert mismatch == {"issue": "DEV-1", "tempo_seconds": 2 * 3600, "jira_seconds": 3600}
        env.submit_mock.assert_not_called()
        env.reject_mock.assert_not_called()

    def test_out_of_period_jira_worklog_ignored_in_cross_check(self):
        records = [make_timesheet(1, entries=[make_entry("DEV-1", 3600)])]
        jira = FakeJira({"DEV-1": [make_entry("DEV-1", 3600), make_entry("DEV-1", 5 * 3600, date="2024-02-05")]})
        with CloseEnvironment(records) as env:
            batch = env.run([1], jira=jira)
        assert result_by_id(batch)[1]["status"] == CLOSE_SUBMITTED

    def test_cross_check_is_optional_without_jira_client(self):
        records = [make_timesheet(1, entries=[make_entry("DEV-1", 3600)])]
        with CloseEnvironment(records) as env:
            batch = env.run([1])
        assert result_by_id(batch)[1]["status"] == CLOSE_SUBMITTED

    def test_approval_result_mismatch_after_submit_fails_item_only(self):
        records = [
            make_timesheet(1, entries=[make_entry("DEV-1", 3600)]),
            make_timesheet(2, entries=[make_entry("DEV-2", 3600)]),
        ]
        jira = FakeJira({"DEV-1": [make_entry("DEV-1", 3600)], "DEV-2": [make_entry("DEV-2", 3600)]})
        with CloseEnvironment(records) as env:
            # Submit HTTP call succeeds for #1 but its approval record does not move.
            env.submit_no_state_flip = {1}
            batch = env.run([1, 2], jira=jira)

        results = result_by_id(batch)
        assert results[1]["status"] == CLOSE_FAILED
        assert results[1]["reasons"] == [REASON_APPROVAL_MISMATCH]
        # The batch still completes the next timesheet.
        assert results[2]["status"] == CLOSE_SUBMITTED

    def test_budgets_unavailable_fails_closed_and_is_retryable(self):
        records = [make_timesheet(1, entries=[make_entry("DEV-1", 3600)])]
        with CloseEnvironment(records) as env:
            env.budgets_mock.side_effect = HTTPError("budgets down")
            first_batch = env.run([1])
            assert result_by_id(first_batch)[1]["status"] == CLOSE_FAILED
            assert result_by_id(first_batch)[1]["reasons"] == [REASON_BUDGETS_UNAVAILABLE]
            env.submit_mock.assert_not_called()

            env.budgets_mock.side_effect = None
            env.budgets_mock.return_value = list(DEFAULT_BUDGETS)
            retry_batch = env.run([1], jira=FakeJira({"DEV-1": [make_entry("DEV-1", 3600)]}))
            assert result_by_id(retry_batch)[1]["status"] == CLOSE_SUBMITTED

    def test_explicit_budgets_skip_budget_lookup(self):
        records = [make_timesheet(1, entries=[make_entry("DEV-1", 3600)])]
        with CloseEnvironment(records) as env:
            env.run([1], jira=FakeJira({"DEV-1": [make_entry("DEV-1", 3600)]}), budgets=[])
            env.budgets_mock.assert_not_called()

    def test_result_keeps_input_order_and_shape(self):
        records = [
            make_timesheet(1, entries=[make_entry("DEV-1", 3600, account=None)]),
            make_timesheet(2, entries=[]),
            make_timesheet(3, entries=[make_entry("DEV-3", 3600)]),
        ]
        jira = FakeJira({"DEV-1": [make_entry("DEV-1", 3600)], "DEV-3": [make_entry("DEV-3", 3600)]})
        with CloseEnvironment(records) as env:
            batch = env.run([1, 2, 3], jira=jira, period=PERIOD)

        assert [item["timesheet_id"] for item in batch["results"]] == [1, 2, 3]
        assert batch["period"] == {"start": "2024-01-01", "end": "2024-01-31"}
        for item in batch["results"]:
            assert set(item) == {"timesheet_id", "status", "reasons", "messages", "action", "detail"}
        assert batch["summary"]["rejected"] == 2
        assert batch["summary"]["submitted"] == 1
        assert batch["summary"]["completed"] == 3
