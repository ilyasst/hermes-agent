"""gw-card handler for the Hermes gateway — the CHECKED-IN fallback.

`gw install render-cards` writes a GENERATED version of this file over this
one on a host that consumes cards; that version has its persona baked in at
render time. This file is what a host has before that happens, and on hosts
where it never will.

## Why this file must claim nothing by default

The gateway asks this module which taps to hand over, and whatever it claims,
it takes: a claimed tap does not reach normal gateway handling. So a hardcoded
list here is not merely stale, it is a swallow.

The previous version of this file hardcoded 9 callback prefixes and the
`/oldest` and `/tasks` commands, and forwarded them to per-persona scripts that
existed on none of the hosts carrying it. Both slash commands were dead for
months on two gateways with nothing in any log — because a handler here is
contractually forbidden from raising into the gateway loop, so its failures are
invisible by construction.

The rule that removes that whole class of failure: **claim only what an
artifact says can actually be routed, and claim nothing when there is no
artifact.** A host with no card layer installed then hands nothing over, so
delegation is safe everywhere and no host needs this file edited or deleted.

## Discovering the artifact without knowing the persona

The generated version knows its persona. This one cannot, so it looks for
exactly one `*-cards-prefixes.json` under the gw config directory.

Exactly one: two personas on a host would make the choice arbitrary, and an
arbitrary choice is how a tap gets routed to the wrong knowledge base. Zero or
several means claim nothing — the same safe default as no artifact at all.

Nothing here raises. The gateway logs and continues, but it must never die on
a card.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path.home() / ".config" / "gw"
_ARTIFACT_SUFFIX = "-cards-prefixes.json"
_MODULE = "gw.cards.transports.hermes"


def _discover():
    """(artifact, persona) — ({}, None) when nothing routable is installed."""
    try:
        found = sorted(_CONFIG_DIR.glob("*" + _ARTIFACT_SUFFIX))
    except OSError:
        return {}, None
    if not found:
        return {}, None
    if len(found) > 1:
        logger.warning(
            "gw-card: %d card artifacts in %s — claiming nothing rather than "
            "guessing which persona a tap belongs to", len(found), _CONFIG_DIR)
        return {}, None
    path = found[0]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("gw-card: unreadable artifact %s (%s) — claiming nothing",
                       path, exc)
        return {}, None
    if not isinstance(data, dict):
        logger.warning("gw-card: artifact %s is not an object — claiming nothing",
                       path)
        return {}, None
    return data, path.name[: -len(_ARTIFACT_SUFFIX)]


_ARTIFACT, PERSONA = _discover()

# `handled_prefixes` is the subset gw.cards can actually route. `prefixes` is
# the full wire inventory, including prefixes the host bot owns — claiming
# those would swallow taps meant for the host. An absent key means an artifact
# older than that split: claim nothing and fail OPEN to the host rather than
# over-claim.
GW_CARD_PREFIXES = tuple(_ARTIFACT.get("handled_prefixes") or ())
GW_CARD_COMMANDS = tuple(_ARTIFACT.get("commands") or ())

if PERSONA and not GW_CARD_PREFIXES:
    logger.warning("gw-card: artifact for %r declares no handled prefixes — "
                   "claiming nothing", PERSONA)


def is_gw_card(data: str | None) -> bool:
    return bool(data) and data.split("|", 1)[0] in GW_CARD_PREFIXES


def is_gw_card_command(text: str | None) -> bool:
    if not text:
        return False
    return text.split()[0].split("@")[0].lower() in GW_CARD_COMMANDS


def _run(args, stdin=None):
    """Blocking subprocess call. Never raises; failures are logged."""
    if not PERSONA:
        # Not reachable through the predicates, which claim nothing without a
        # persona. Kept because a caller could invoke a handler directly, and
        # forwarding to an unknown persona is worse than not forwarding.
        logger.error("gw-card: asked to handle a card with no artifact present")
        return None
    try:
        return subprocess.run(
            [sys.executable, "-m", _MODULE, "--persona", PERSONA] + args,
            input=stdin, capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        logger.error("gw-card: could not run %s %s: %s", _MODULE, args[:2], exc)
        return None


def _report(what, res):
    """Surface the one-shot's outcome in the GATEWAY log.

    The entrypoint exits 0 even when it refuses, and reports on stdout;
    capture_output means none of that reaches the gateway unless it is logged
    here. Without this a tap can be intercepted, fail completely, and still look
    successful — silence reading as success, which is the failure this file
    exists to stop.
    """
    if res is None:
        return
    out = (res.stdout or "").strip()
    err = (res.stderr or "").strip()
    if res.returncode != 0:
        logger.error("gw-card %s failed (rc=%s): %s", what, res.returncode,
                     err or out or "no output")
        return
    if out:
        logger.info("gw-card %s: %s", what, out)
    if err:
        logger.warning("gw-card %s stderr: %s", what, err)


async def _run_async(args, stdin=None):
    """Off the event loop: the gateway's poll loop must not block on a card."""
    return await asyncio.to_thread(_run, args, stdin)


def _cq_dict(query, data):
    """The callback_query shape gw.cards expects, from the gateway object."""
    try:
        return query.to_dict()
    except Exception:  # noqa: BLE001
        frm = getattr(query, "from_user", None)
        msg = getattr(query, "message", None)
        chat = getattr(msg, "chat", None)
        return {
            "id": getattr(query, "id", ""),
            "data": data,
            "from": {"id": getattr(frm, "id", 0)},
            "message": {
                "message_id": getattr(msg, "message_id", None),
                "chat": {"id": getattr(chat, "id", 0)},
                "text": getattr(msg, "text", None) or "",
            },
        }


async def handle_gw_card_callback(query, data: str,
                                  adapter_name: str = "telegram") -> None:
    """Forward one card tap.

    Does NOT acknowledge the query: gw.cards is the sole acknowledger for taps
    it claims, so acking here would race it and could acknowledge a tap that
    then failed to apply.
    """
    try:
        payload = json.dumps(_cq_dict(query, data))
    except Exception as exc:  # noqa: BLE001
        logger.error("gw-card: could not serialise callback (%s)", exc)
        return
    _report("callback", await _run_async(["--callback", "-"], stdin=payload))


async def handle_gw_card_command(message, text: str,
                                 adapter_name: str = "telegram") -> None:
    """Forward one slash command."""
    frm = getattr(message, "from_user", None)
    args = ["--command", text]
    uid = getattr(frm, "id", None)
    if uid is not None:
        args += ["--user-id", str(uid)]
    _report("command", await _run_async(args))
