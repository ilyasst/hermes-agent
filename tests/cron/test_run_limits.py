"""Behavior tests for bounded autonomous cron runs."""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest

from cron.run_limits import (
    CronRunLimits,
    CronRunMonitor,
    metrics_markdown,
    optional_positive_int,
    terminal_signal,
)


@pytest.mark.parametrize("value", [0, -1, True, "1.0", "many"])
def test_positive_integer_limit_validation_rejects_ambiguous_values(value):
    with pytest.raises(ValueError, match="positive integer"):
        optional_positive_int(value, "max_tool_calls")


def test_job_limits_override_defaults_and_require_ordered_tool_dispatch():
    limits = CronRunLimits.from_job(
        {
            "max_turns": "7",
            "max_tool_calls": 4,
            "wall_timeout_seconds": "+30",
            "stop_on_terminal_signal": True,
        },
        default_max_turns=500,
    )

    assert limits == CronRunLimits(
        max_turns=7,
        max_tool_calls=4,
        wall_timeout_seconds=30,
        stop_on_terminal_signal=True,
    )
    assert limits.requires_sequential_tools is True


@pytest.mark.parametrize("reason", ["recorded", "already_recorded"])
def test_valid_record_attempt_is_terminal_in_direct_and_wrapped_results(reason):
    payload = {
        "_hermes": {
            "kind": "record_attempt",
            "terminal": True,
            "reason": reason,
        }
    }

    expected = {
        "record_attempt": True,
        "terminal": True,
        "reason": reason,
    }
    assert terminal_signal(payload) == expected
    assert terminal_signal(json.dumps(payload)) == expected
    assert terminal_signal(json.dumps({"output": json.dumps(payload)})) == expected


def test_untrusted_terminal_reason_cannot_stop_the_run():
    assert terminal_signal(
        {"_hermes": {"kind": "record_attempt", "terminal": True,
                     "reason": "arbitrary-text"}}
    ) == {"record_attempt": True, "terminal": False, "reason": None}


def test_monitor_stops_after_successful_record_and_reports_content_free_metrics():
    ticks = iter([10.0, 10.125])
    monitor = CronRunMonitor(
        CronRunLimits(max_turns=5, stop_on_terminal_signal=True),
        clock=lambda: next(ticks),
    )

    assert monitor.tool_started() is None
    assert monitor.tool_completed(
        {"_hermes": {"kind": "record_attempt", "terminal": True,
                     "reason": "recorded"}}
    ) == "recorded"
    assert monitor.tool_started() == "recorded"

    metrics = monitor.finish(turns=2)
    assert metrics == {
        "turns": 2,
        "tool_calls": 1,
        "duration_ms": 125,
        "record_attempts": 1,
        "terminal_reason": "recorded",
    }
    rendered = metrics_markdown(metrics)
    assert "Turns: 2" in rendered
    assert "Tool calls: 1" in rendered
    assert "Record attempts: 1" in rendered
    assert "`recorded`" in rendered


def test_tool_call_budget_denies_the_first_call_beyond_the_limit():
    monitor = CronRunMonitor(CronRunLimits(max_turns=5, max_tool_calls=2))

    assert monitor.tool_started() is None
    assert monitor.tool_completed({"ok": True}) is None
    assert monitor.tool_started() is None
    assert monitor.tool_completed({"ok": True}) == "tool_call_budget"
    assert monitor.tool_started() == "tool_call_budget"
    assert monitor.tool_calls == 2


def test_wall_timeout_uses_elapsed_wall_clock_and_preserves_terminal_success():
    now = [100.0]
    monitor = CronRunMonitor(
        CronRunLimits(max_turns=5, wall_timeout_seconds=3),
        clock=lambda: now[0],
    )

    now[0] = 102.99
    assert monitor.wall_timeout_reached() is False
    now[0] = 103.0
    assert monitor.wall_timeout_reached() is True
    assert monitor.finish(turns=1)["terminal_reason"] == "wall_timeout"


def test_metrics_never_echo_untrusted_terminal_text():
    rendered = metrics_markdown(
        {
            "turns": 1,
            "tool_calls": 2,
            "duration_ms": 3,
            "record_attempts": 0,
            "terminal_reason": "private record content",
        }
    )

    assert "private record content" not in rendered
    assert "`agent_error`" in rendered


@pytest.fixture
def cron_store(tmp_path, monkeypatch):
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    monkeypatch.setattr("cron.jobs.HERMES_DIR", tmp_path)
    monkeypatch.setattr("cron.jobs.CRON_DIR", cron_dir)
    monkeypatch.setattr("cron.jobs.JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", cron_dir / "output")
    return tmp_path


def test_create_update_and_tool_format_round_trip_run_limits(cron_store):
    from cron.jobs import create_job, update_job
    from tools.cronjob_tools import CRONJOB_SCHEMA, _format_job

    job = create_job(
        prompt="Process one synthetic work item",
        schedule="every 1h",
        max_turns=6,
        max_tool_calls=9,
        wall_timeout_seconds=120,
        stop_on_terminal_signal=True,
    )

    formatted = _format_job(job)
    assert formatted["max_turns"] == 6
    assert formatted["max_tool_calls"] == 9
    assert formatted["wall_timeout_seconds"] == 120
    assert formatted["stop_on_terminal_signal"] is True
    updated = update_job(job["id"], {"max_tool_calls": "11"})
    assert updated["max_tool_calls"] == 11
    for field in (
        "max_turns",
        "max_tool_calls",
        "wall_timeout_seconds",
        "stop_on_terminal_signal",
    ):
        assert field in CRONJOB_SCHEMA["parameters"]["properties"]


def _run_scheduler_job(tmp_path, job, fake_agent_type, *, wait_result=None):
    fake_db = MagicMock()
    patches = [
        patch("cron.scheduler._hermes_home", tmp_path),
        patch("cron.scheduler._resolve_origin", return_value=None),
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state.SessionDB", return_value=fake_db),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "synthetic-key",
                "base_url": "https://example.invalid/v1",
                "provider": "openrouter",
                "api_mode": "chat_completions",
            },
        ),
        patch("run_agent.AIAgent", fake_agent_type),
    ]
    if wait_result is not None:
        patches.append(
            patch("cron.scheduler.concurrent.futures.wait", return_value=wait_result)
        )
    with contextlib.ExitStack() as stack:
        for context_manager in patches:
            stack.enter_context(context_manager)
        from cron.scheduler import run_job
        return run_job(job)


@pytest.mark.parametrize("reason", ["recorded", "already_recorded"])
def test_scheduler_treats_valid_record_outcomes_as_silent_success(
    tmp_path, reason
):
    instances = []

    class TerminalAgent:
        _format_turn_completion_explanation = staticmethod(lambda _reason: "")

        def __init__(self, *args, **kwargs):
            self.start = kwargs["tool_start_callback"]
            self.complete = kwargs["tool_complete_callback"]
            self.interruptions = []
            instances.append(self)

        def interrupt(self, message=None):
            self.interruptions.append(message)

        def run_conversation(self, _prompt):
            self.start("call-1", "terminal", {})
            self.complete(
                "call-1",
                "terminal",
                {},
                json.dumps(
                    {
                        "output": json.dumps(
                            {"_hermes": {"kind": "record_attempt",
                                         "terminal": True, "reason": reason}}
                        )
                    }
                ),
            )
            return {
                "final_response": "interrupted after terminal signal",
                "failed": True,
                "completed": False,
                "api_call_count": 2,
            }

        def close(self):
            pass

    success, output, final_response, error = _run_scheduler_job(
        tmp_path,
        {
            "id": "bounded-job",
            "name": "Bounded job",
            "prompt": "Process one synthetic work item",
            "stop_on_terminal_signal": True,
        },
        TerminalAgent,
    )

    assert success is True
    assert error is None
    assert final_response == "[SILENT]"
    assert f"Terminal reason: `{reason}`" in output
    assert "Record attempts: 1" in output
    assert instances[0].interruptions
    assert instances[0]._force_sequential_tool_calls is True


def test_scheduler_marks_explicit_turn_budget_exhaustion_as_failure(tmp_path):
    class TurnLimitedAgent:
        _format_turn_completion_explanation = staticmethod(lambda _reason: "")

        def __init__(self, *args, **kwargs):
            self.max_iterations = kwargs["max_iterations"]

        def run_conversation(self, _prompt):
            return {
                "final_response": "fallback",
                "completed": False,
                "failed": False,
                "api_call_count": self.max_iterations,
                "turn_exit_reason": (
                    f"max_iterations_reached({self.max_iterations}/"
                    f"{self.max_iterations})"
                ),
            }

        def close(self):
            pass

    success, output, final_response, error = _run_scheduler_job(
        tmp_path,
        {
            "id": "turn-limited-job",
            "name": "Turn limited",
            "prompt": "Process one synthetic work item",
            "max_turns": 2,
        },
        TurnLimitedAgent,
    )

    assert success is False
    assert final_response == ""
    assert "model-turn budget" in error
    assert "Terminal reason: `turn_budget`" in output


def test_scheduler_stops_and_fails_when_tool_budget_is_consumed(tmp_path):
    instances = []

    class ToolLimitedAgent:
        _format_turn_completion_explanation = staticmethod(lambda _reason: "")

        def __init__(self, *args, **kwargs):
            self.start = kwargs["tool_start_callback"]
            self.complete = kwargs["tool_complete_callback"]
            self.interruptions = []
            instances.append(self)

        def interrupt(self, message=None):
            self.interruptions.append(message)

        def run_conversation(self, _prompt):
            self.start("call-1", "read_file", {})
            self.complete("call-1", "read_file", {}, {"ok": True})
            self.start("call-2", "read_file", {})
            self.complete("call-2", "read_file", {}, {"ok": True})
            return {
                "final_response": "interrupted",
                "failed": True,
                "completed": False,
                "api_call_count": 1,
            }

        def close(self):
            pass

    success, output, final_response, error = _run_scheduler_job(
        tmp_path,
        {
            "id": "tool-limited-job",
            "name": "Tool limited",
            "prompt": "Process one synthetic work item",
            "max_tool_calls": 2,
        },
        ToolLimitedAgent,
    )

    assert success is False
    assert final_response == ""
    assert "tool-call budget" in error
    assert "Tool calls: 2" in output
    assert "Terminal reason: `tool_call_budget`" in output
    assert instances[0].interruptions


def test_scheduler_wall_timeout_works_when_inactivity_timeout_is_disabled(
    tmp_path, monkeypatch
):
    instances = []

    class WaitingAgent:
        def __init__(self, *args, **kwargs):
            self.interruptions = []
            instances.append(self)

        def interrupt(self, message=None):
            self.interruptions.append(message)

        def run_conversation(self, _prompt):
            return {"final_response": "late"}

        def close(self):
            pass

    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")
    with patch(
        "cron.run_limits.CronRunMonitor.wall_timeout_reached", return_value=True
    ):
        success, output, final_response, error = _run_scheduler_job(
            tmp_path,
            {
                "id": "wall-limited-job",
                "name": "Wall limited",
                "prompt": "Process one synthetic work item",
                "wall_timeout_seconds": 1,
            },
            WaitingAgent,
            wait_result=(set(), set()),
        )

    assert success is False
    assert final_response == ""
    assert "wall-clock limit" in error
    assert "Terminal reason: `wall_timeout`" in output
    assert instances[0].interruptions
