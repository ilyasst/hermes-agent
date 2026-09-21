"""FORK-LOCAL (ilyasst): demand attribution for our caproute router.

Our fleet routes model calls through caproute, and which models each
machine runs is decided from what caproute records. caproute identifies
the process on the far end of the socket by itself, so it already knows
a call came from hermes. What it cannot know is what the call was *for* —
only the caller holds that.

foxhound spawns one `hermes chat` per task, so the job is constant for
the life of the process and the environment is the channel: the parent
exports ``CAPROUTE_*`` and every client this process builds carries it.

Lives here, not in `run_agent`, because the headers have to be attached
at **every** place an OpenAI client is constructed. There are two, and
attaching at only the obvious one is exactly the bug this module was
written after: the agent's own client carried the headers and the
auxiliary client — the one that actually served the chat — did not, so
the router went on logging `operation=unknown` while the tests passed.

Silent unless asked. With none of the variables set, callers get their
kwargs back unchanged, so a Hermes outside our fleet is byte-identical
to upstream and merges stay clean.
"""

from __future__ import annotations

import contextlib
import contextvars
import os

#: Long enough for a uuid or a profile id, short enough that a header
#: cannot become a payload. caproute truncates at the same bound.
MAX_VALUE_CHARS = 160

_FIELDS = (
    ("X-Caproute-Operation", "CAPROUTE_OPERATION"),
    ("X-Caproute-Job", "CAPROUTE_JOB"),
    ("X-Caproute-Run-Id", "CAPROUTE_RUN_ID"),
    ("X-Caproute-Work-Item-Type", "CAPROUTE_WORK_ITEM_TYPE"),
    ("X-Caproute-Work-Item-Id", "CAPROUTE_WORK_ITEM_ID"),
)


#: The auxiliary function a client is being built for, if any.
#:
#: Hermes already routes each function to its own capability
#: (`auxiliary.compression`, `auxiliary.approval`, …) and already passes a
#: `task=` name into `resolve_provider_client`. A client is built per
#: function and reused, so the name is fixed for that client's life and
#: belongs in its default headers rather than on each request.
#:
#: A ContextVar rather than a parameter because `resolve_provider_client`
#: constructs clients down several branches — sync, async, and per-provider
#: — and threading an argument through all of them would be easy to miss on
#: the next one added.
_task = contextvars.ContextVar("caproute_task", default=None)


@contextlib.contextmanager
def for_task(task: str | None):
    """Name the function any client built in this block is serving."""
    token = _task.set(task or None)
    try:
        yield
    finally:
        _task.reset(token)


def _clean(value: object) -> str:
    """Bounded and printable: this becomes a header and a router log line."""
    text = "".join(
        character for character in str(value)
        if character.isprintable() and character not in "\r\n\t"
    )
    return text.strip()[:MAX_VALUE_CHARS]


def headers() -> dict[str, str]:
    """Attribution for this client, or empty when nobody asked.

    Enabled by any ``CAPROUTE_*`` variable, including ``CAPROUTE_APP``
    alone. That is a change from the first version, which treated APP
    alone as nothing to say because caproute can already identify the
    process. With a task name there IS something to say that no peer
    lookup can infer — which of the agent's functions spent the call —
    and the gateway, being long-lived, never carries a per-job
    environment, so APP is the only switch it can be given.

    Still silent with no CAPROUTE_* set at all. That matters beyond tidy
    merges: without a switch, a Hermes pointed at a third-party endpoint
    would send our internal function names to it.

    The task wins over the environment's operation when both exist. The
    environment says which foxhound job spawned this process; the task
    says what this particular client does, which is the finer and more
    useful of the two. The job is kept separately in `X-Caproute-Job`, so
    nothing is lost.
    """
    found = {}
    for header, variable in _FIELDS:
        cleaned = _clean(os.environ.get(variable) or "")
        if cleaned:
            found[header] = cleaned
    app = _clean(os.environ.get("CAPROUTE_APP") or "")
    if not found and not app:
        return {}
    task = _clean(_task.get() or "")
    if task:
        found["X-Caproute-Operation"] = f"aux.{task}"
    found["X-Caproute-App"] = app or "hermes"
    found["X-Caproute-Process"] = "hermes-agent"
    found["X-Caproute-Pid"] = str(os.getpid())
    return found


def with_attribution(client_kwargs: dict) -> dict:
    """Client kwargs carrying this run's attribution.

    Returns the original object untouched when there is nothing to add,
    and never mutates what it was given: callers pass stored dicts around
    and an in-place edit would leak into later requests.
    """
    extra = headers()
    if not extra:
        return client_kwargs
    merged = dict(client_kwargs)
    existing = dict(merged.get("default_headers") or {})
    existing.update(extra)
    merged["default_headers"] = existing
    return merged
