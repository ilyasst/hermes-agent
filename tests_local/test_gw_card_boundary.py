#!/usr/bin/env python3
"""[LOCAL] gw-card gateway boundary — the six-case matrix (gw#46).

The gateway used to gate on its own hardcoded prefix tuple, acknowledge the
tap, and only then delegate. Three consequences, all observed in the fleet:
the list drifted (9 / 9 / 14 across hosts against a handled set of 14); a
missing or broken handler produced an acknowledged, silently dropped tap;
and the transport's own toasts were dead because the query was already
answered.

This matrix pins the corrected boundary. It exercises the REAL
_handle_callback_query, not a re-implementation of it.

Hermetic: fake handler modules injected into sys.modules, no network, no
Telegram, no gateway process. Run:
  <hermes-venv>/python tests_local/test_gw_card_boundary.py
"""
import asyncio
import logging
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gateway.platforms.telegram as T

N = 0


def ok(cond, msg):
    global N
    assert cond, f"FAIL: {msg}"
    N += 1
    print(f"  ok  {msg}")


class _ReachedNative(Exception):
    """Raised by the sentinel below when execution continues past the
    gw-card block, which is how 'the native chain was reached' is observed."""


class _Sentinel:
    def __getattr__(self, item):
        raise _ReachedNative(item)


class _Query:
    def __init__(self, data):
        self.data = data
        self.answered = 0
        self.message = _Sentinel()

    async def answer(self, *a, **k):
        self.answered += 1


class _Update:
    def __init__(self, q):
        self.callback_query = q


class _Blocker:
    """Simulate the module genuinely not being installed.

    Emptying a package __path__ is not enough - the real file is on disk in
    this checkout and gets found anyway, which the first version of this test
    proved by shelling out to the real handler. A meta-path finder that raises
    with the EXACT module name is what the loader actually sees on a host
    where cards were never installed.
    """
    target = "tools.gw_card_handler"

    def find_spec(self, name, path=None, target=None):
        if name == self.target:
            raise ModuleNotFoundError(
                f"No module named {name!r}", name=name)
        return None


_BLOCKER = _Blocker()


def _install(module):
    """Put a fake tools.gw_card_handler in place, or block it entirely."""
    sys.modules.pop("tools.gw_card_handler", None)
    sys.modules.pop("tools", None)
    while _BLOCKER in sys.meta_path:
        sys.meta_path.remove(_BLOCKER)
    if module is None:
        sys.meta_path.insert(0, _BLOCKER)
        return
    pkg = types.ModuleType("tools")
    pkg.__path__ = []
    sys.modules["tools"] = pkg
    sys.modules["tools.gw_card_handler"] = module


def _handler(is_card, on_call=None, calls=None):
    m = types.ModuleType("tools.gw_card_handler")
    m.is_gw_card = is_card

    async def handle(query, data, name):
        if calls is not None:
            calls.append((data, name))
        if on_call is not None:
            on_call()
    m.handle_gw_card_callback = handle
    return m


class _CaptureLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


class _Adapter(T.TelegramAdapter):
    """`name` is a read-only property on the real adapter; override it rather
    than constructing a full adapter (which would want config and a token)."""
    name = "demo"


def _run(data, module):
    """Drive the real _handle_callback_query; return (query, reached_native)."""
    _install(module)
    adapter = _Adapter.__new__(_Adapter)
    q = _Query(data)
    reached = False
    try:
        asyncio.run(adapter._handle_callback_query(_Update(q), None))
    except _ReachedNative:
        reached = True
    return q, reached


def main():
    cap = _CaptureLog()
    T.logger.addHandler(cap)
    T.logger.setLevel(logging.ERROR)

    def logs():
        return "\n".join(cap.records)

    def clear():
        cap.records.clear()

    # ── 1. predicate true -> delegate exactly once, never pre-answered ──
    calls = []
    q, reached = _run("tc|1|done", _handler(lambda d: True, calls=calls))
    ok(len(calls) == 1 and calls[0] == ("tc|1|done", "demo"),
       "a claimed tap is delegated EXACTLY once, with data and adapter name")
    ok(q.answered == 0,
       "and the gateway never pre-answers — gw.cards is the sole "
       "acknowledger, which is what makes its toasts work again")
    ok(not reached, "a claimed tap does not continue into the native chain")

    # ── 2. predicate false -> untouched, native chain reached ──
    clear()
    calls = []
    q, reached = _run("gda|4", _handler(lambda d: False, calls=calls))
    ok(not calls, "a host-owned prefix is NOT delegated")
    ok(q.answered == 0, "and is not acknowledged by the gw block")
    ok(reached, "it falls through to the native chain, still handleable")
    ok(not logs(), "and produces no error output — it is a normal event")

    # ── 3. the exact optional module absent -> QUIET fail-open ──
    clear()
    q, reached = _run("tc|1|done", None)
    ok(reached,
       "with no handler installed the tap falls through to the native chain")
    ok(q.answered == 0,
       "and is NOT acknowledged — never ack-and-drop (this is #36's shape)")
    ok(logs() == "",
       "and NOTHING is logged: absence is the steady state on a host that "
       "does not run cards, and a line per native callback would train "
       "everyone to ignore the one that matters")

    # ── 3b. installed BUT BROKEN must NOT be silenced by the same path ──
    clear()
    broken = types.ModuleType("tools.gw_card_handler")

    def _raise_inner():
        raise ModuleNotFoundError("No module named 'gw'", name="gw")
    broken.__getattr__ = lambda n: _raise_inner()
    _install(broken)
    adapter = _Adapter.__new__(_Adapter)
    q2 = _Query("tc|1|done")
    reached2 = False
    try:
        asyncio.run(adapter._handle_callback_query(_Update(q2), None))
    except _ReachedNative:
        reached2 = True
    ok("installed but its imports fail" in logs(),
       "a module that IS installed but whose own imports fail IS logged — "
       "broad ModuleNotFoundError suppression would have hidden it")
    ok(reached2 and q2.answered == 0,
       "and it still fails open, unacknowledged")

    # ── 4. predicate raises -> logged, fail open ──
    clear()
    calls = []

    def boom(d):
        raise RuntimeError("artifact unreadable")
    q, reached = _run("tc|1|done", _handler(boom, calls=calls))
    ok("gw-card predicate failed" in logs(),
       "a predicate that raises is LOGGED (unlike plain absence)")
    ok(not calls and reached and q.answered == 0,
       "and fails open: nothing delegated, native chain reached, no ack")

    # ── 5. claimed handler raises -> logged, unacknowledged, NO re-claim ──
    clear()

    def explode():
        raise RuntimeError("bridge down")
    q, reached = _run("tc|1|done", _handler(lambda d: True, on_call=explode))
    ok("gw-card callback failed" in logs(), "a post-claim failure is logged")
    ok(q.answered == 0,
       "and left UNACKNOWLEDGED — visibly stuck beats silently swallowed")
    ok(not reached,
       "and does NOT fall through: once claimed the tap is ours, and a "
       "native handler does not own this prefix")

    # ── 6. the command hook is untouched ──
    src = Path(T.__file__).read_text(encoding="utf-8")
    ok("is_gw_card_command, handle_gw_card_command" in src,
       "the command hook still imports its own predicate and handler")
    ok(src.count("_should_process_message(msg, is_command=True)") == 1,
       "and still falls through to native command handling")
    ok('"tc", "tcr", "tcx"' not in src and '"gst", "gsu")' not in src,
       "no hardcoded prefix tuple remains anywhere in the file")

    print(f"\nALL {N} CHECKS PASSED — gateway boundary: claim from the "
          "artifact, delegate before acknowledging, quiet only for absence")


if __name__ == "__main__":
    main()
