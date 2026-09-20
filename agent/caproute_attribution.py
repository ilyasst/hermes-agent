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


def _clean(value: object) -> str:
    """Bounded and printable: this becomes a header and a router log line."""
    text = "".join(
        character for character in str(value)
        if character.isprintable() and character not in "\r\n\t"
    )
    return text.strip()[:MAX_VALUE_CHARS]


def headers() -> dict[str, str]:
    """Attribution for this process, or empty when nobody asked.

    ``CAPROUTE_APP`` alone does not count as asking: caproute already
    knows we are hermes, and only the job context is new information.
    """
    found = {}
    for header, variable in _FIELDS:
        cleaned = _clean(os.environ.get(variable) or "")
        if cleaned:
            found[header] = cleaned
    if not found:
        return {}
    found["X-Caproute-App"] = _clean(os.environ.get("CAPROUTE_APP") or "hermes")
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
