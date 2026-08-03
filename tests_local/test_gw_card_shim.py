#!/usr/bin/env python3
"""[LOCAL] The checked-in gw-card shim claims only what an artifact allows.

The gateway asks this module which taps to hand over, and whatever it claims,
it takes — a claimed tap never reaches native handling. The previous version
hardcoded 9 prefixes plus /oldest and /tasks and forwarded them to per-persona
scripts that existed on none of the hosts carrying it, so both slash commands
were dead for months on two gateways with nothing in any log.

The property that removes that class of failure: claim only what an artifact
says can be routed, and claim NOTHING without one. Then delegation is safe on
every host and no host needs this file edited or deleted.

Hermetic: HOME is redirected per case and the module re-imported, so each case
sees a different config directory. Run:
  <hermes-venv>/python tests_local/test_gw_card_shim.py
"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

N = 0


def ok(cond, msg):
    global N
    assert cond, f"FAIL: {msg}"
    N += 1
    print(f"  ok  {msg}")


def load(artifacts: dict):
    """Re-import the shim with a fresh HOME containing `artifacts`.

    Discovery happens at import time, which is what the gateway does, so the
    test has to re-import rather than poke module globals.
    """
    home = Path(tempfile.mkdtemp(prefix="shim-home-"))
    cfg = home / ".config" / "gw"
    cfg.mkdir(parents=True)
    for name, body in artifacts.items():
        p = cfg / name
        p.write_text(body if isinstance(body, str) else json.dumps(body),
                     encoding="utf-8")
    os.environ["HOME"] = str(home)
    sys.modules.pop("tools.gw_card_handler", None)
    return importlib.import_module("tools.gw_card_handler")


HANDLED = ["tc", "tcr", "tcx", "tcp", "tcb", "mtk", "treo", "gst", "gsu",
           "bt", "br", "brp", "brc", "bx"]
FULL = HANDLED + ["gda", "gdok", "gdm", "gds", "gdu", "gwc", "gwr", "gwu"]
ARTIFACT = {"version": 2, "prefixes": FULL, "handled_prefixes": HANDLED,
            "commands": ["/oldest", "/tasks"]}

_real_home = os.environ.get("HOME")
try:
    # ── no artifact: the default, and the case that was broken ───────────
    m = load({})
    ok(m.GW_CARD_PREFIXES == (), "with no artifact, no callback prefix is claimed")
    ok(m.GW_CARD_COMMANDS == (), "and no command is claimed")
    ok(m.PERSONA is None, "and no persona is inferred")
    ok(m.is_gw_card("tc|1") is False,
       "a task-card tap is NOT claimed on a host with no card layer — it "
       "reaches native handling instead of being swallowed")
    ok(m.is_gw_card_command("/tasks") is False,
       "/tasks is NOT claimed, which is the bug that killed it for months")
    ok(m.is_gw_card_command("/oldest") is False, "nor is /oldest")

    # ── exactly one artifact: claim its handled subset ───────────────────
    m = load({"manufacturia-cards-prefixes.json": ARTIFACT})
    ok(m.PERSONA == "manufacturia", "the persona is derived from the filename")
    ok(m.GW_CARD_PREFIXES == tuple(HANDLED),
       "the handled subset is claimed, all 14 of it")
    ok(m.is_gw_card("bt|x") is True,
       "including the brief prefixes the hardcoded list never had")
    ok(m.is_gw_card_command("/tasks@somebot") is True,
       "a command survives the @botname suffix Telegram appends")

    # The full inventory is NOT claimed: those belong to the host bot, and
    # claiming them would swallow taps this package cannot route.
    host_owned = [p for p in FULL if p not in HANDLED]
    ok(host_owned and all(not m.is_gw_card(f"{p}|x") for p in host_owned),
       "prefixes outside the handled subset are left to the host bot")

    # ── an artifact predating the ownership split ────────────────────────
    m = load({"p-cards-prefixes.json": {"version": 1, "prefixes": FULL,
                                        "commands": ["/oldest"]}})
    ok(m.GW_CARD_PREFIXES == (),
       "an artifact with no handled_prefixes key claims NOTHING rather than "
       "falling back to the full inventory — failing open to the host")

    # ── ambiguity refuses ────────────────────────────────────────────────
    m = load({"a-cards-prefixes.json": ARTIFACT, "b-cards-prefixes.json": ARTIFACT})
    ok(m.GW_CARD_PREFIXES == () and m.PERSONA is None,
       "two artifacts claim nothing — an arbitrary choice is how a tap gets "
       "routed to the wrong knowledge base")

    # ── malformed artifacts fail closed ──────────────────────────────────
    m = load({"p-cards-prefixes.json": "{ not json"})
    ok(m.GW_CARD_PREFIXES == (), "unparseable artifact claims nothing")
    m = load({"p-cards-prefixes.json": "[1, 2, 3]"})
    ok(m.GW_CARD_PREFIXES == (),
       "a JSON list is not an artifact — claims nothing rather than raising "
       "on .get, which would break the gateway's import")
    m = load({"p-cards-prefixes.json": {"handled_prefixes": None}})
    ok(m.GW_CARD_PREFIXES == (), "a null handled_prefixes claims nothing")

    # ── the handlers refuse to forward without a persona ─────────────────
    m = load({})
    ok(m._run(["--command", "/tasks"]) is None,
       "a handler called directly with no artifact refuses to forward rather "
       "than guessing a persona")

    print(f"\nALL {N} CHECKS PASSED — the shim claims only what an artifact "
          "allows, and nothing without one")
finally:
    if _real_home is not None:
        os.environ["HOME"] = _real_home
