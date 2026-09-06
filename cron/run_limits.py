"""Per-job limits and terminal signals for autonomous cron agents."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


TERMINAL_SIGNAL_KEY = "_hermes"
TERMINAL_SUCCESS_REASONS = frozenset({"recorded", "already_recorded"})
TERMINAL_STOP_REASONS = TERMINAL_SUCCESS_REASONS | {"claim_lost"}


def optional_positive_int(value: Any, field: str) -> int | None:
    """Normalize an optional positive integer job field."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if normalized <= 0 or str(value).strip() not in {str(normalized), f"+{normalized}"}:
        raise ValueError(f"{field} must be a positive integer")
    return normalized


@dataclass(frozen=True)
class CronRunLimits:
    max_turns: int
    max_tool_calls: int | None = None
    wall_timeout_seconds: int | None = None
    stop_on_terminal_signal: bool = False

    @classmethod
    def from_job(cls, job: dict, *, default_max_turns: int) -> "CronRunLimits":
        max_turns = optional_positive_int(job.get("max_turns"), "max_turns")
        max_tool_calls = optional_positive_int(
            job.get("max_tool_calls"), "max_tool_calls"
        )
        wall_timeout = optional_positive_int(
            job.get("wall_timeout_seconds"), "wall_timeout_seconds"
        )
        return cls(
            max_turns=max_turns or max(1, int(default_max_turns)),
            max_tool_calls=max_tool_calls,
            wall_timeout_seconds=wall_timeout,
            stop_on_terminal_signal=bool(job.get("stop_on_terminal_signal", False)),
        )

    @property
    def requires_sequential_tools(self) -> bool:
        return self.max_tool_calls is not None or self.stop_on_terminal_signal


def _json_object(value: Any) -> dict | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def terminal_signal(result: Any) -> dict | None:
    """Return a validated terminal-tool signal, if one is present.

    Terminal tool results wrap process stdout in an ``output`` field. Direct
    plugin tools may return the signal object itself, so both shapes are
    accepted. Arbitrary reason text is never propagated into metrics.
    """
    outer = _json_object(result)
    if not outer:
        return None
    candidate = _json_object(outer.get("output")) or outer
    signal = candidate.get(TERMINAL_SIGNAL_KEY)
    if not isinstance(signal, dict) or signal.get("kind") != "record_attempt":
        return None
    reason = signal.get("reason")
    return {
        "record_attempt": True,
        "terminal": (
            signal.get("terminal") is True and reason in TERMINAL_STOP_REASONS
        ),
        "reason": reason if reason in TERMINAL_STOP_REASONS else None,
    }


class CronRunMonitor:
    """Thread-safe content-free counters and stop decisions for one run."""

    def __init__(
        self,
        limits: CronRunLimits,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._clock = clock
        self._started_at = clock()
        self._lock = threading.Lock()
        self.tool_calls = 0
        self.record_attempts = 0
        self.terminal_reason: str | None = None

    def tool_started(self) -> str | None:
        """Reserve one execution slot, or return the active stop reason."""
        with self._lock:
            if self.terminal_reason:
                return self.terminal_reason
            limit = self.limits.max_tool_calls
            if limit is not None and self.tool_calls >= limit:
                self.terminal_reason = "tool_call_budget"
                return self.terminal_reason
            self.tool_calls += 1
            return None

    def tool_completed(self, result: Any) -> str | None:
        with self._lock:
            signal = terminal_signal(result)
            if signal and signal["record_attempt"]:
                self.record_attempts += 1
            if (self.limits.stop_on_terminal_signal and signal
                    and signal["terminal"]):
                self.terminal_reason = str(signal["reason"])
            elif (self.limits.max_tool_calls is not None
                  and self.tool_calls >= self.limits.max_tool_calls
                  and not self.terminal_reason):
                self.terminal_reason = "tool_call_budget"
            return self.terminal_reason

    def wall_timeout_reached(self) -> bool:
        limit = self.limits.wall_timeout_seconds
        if limit is None:
            return False
        with self._lock:
            if self.terminal_reason:
                return False
            if self._clock() - self._started_at < limit:
                return False
            self.terminal_reason = "wall_timeout"
            return True

    def mark_terminal(self, reason: str) -> None:
        with self._lock:
            if not self.terminal_reason:
                self.terminal_reason = reason

    def finish(self, *, turns: int, reason: str | None = None) -> dict:
        with self._lock:
            if not self.terminal_reason:
                self.terminal_reason = reason or "completed"
            return {
                "turns": max(0, int(turns)),
                "tool_calls": self.tool_calls,
                "duration_ms": max(
                    0, int((self._clock() - self._started_at) * 1000)
                ),
                "record_attempts": self.record_attempts,
                "terminal_reason": self.terminal_reason,
            }


def metrics_markdown(metrics: dict) -> str:
    """Render only fixed labels, numbers, and a normalized reason enum."""
    reason = str(metrics.get("terminal_reason") or "completed")
    if reason not in TERMINAL_STOP_REASONS | {
        "completed", "tool_call_budget", "turn_budget", "wall_timeout",
        "inactivity_timeout", "agent_error",
    }:
        reason = "agent_error"
    return (
        "\n\n## Run metrics\n\n"
        f"- Turns: {max(0, int(metrics.get('turns', 0)))}\n"
        f"- Tool calls: {max(0, int(metrics.get('tool_calls', 0)))}\n"
        f"- Duration: {max(0, int(metrics.get('duration_ms', 0)))} ms\n"
        f"- Record attempts: {max(0, int(metrics.get('record_attempts', 0)))}\n"
        f"- Terminal reason: `{reason}`\n"
    )
