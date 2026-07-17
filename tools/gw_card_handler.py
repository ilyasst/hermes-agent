"""[LOCAL] Bridge gw task/state card taps to the standalone gw handler.

The Hermes gateway owns the @nalyze_bobot Telegram long-poll, so a separate
poller for interactive gw cards would 409-conflict. Instead, gw cards are
delivered by nalyze-cards-sender (send-only) and their button taps arrive
here; the gateway's _handle_callback_query forwards gw-prefixed callbacks to
this module, which runs the actual handler (review_bot.handle_callback) in its
OWN process (manufacturia-card-callback.py) to keep gw/shalashaska imports out of
the gateway. Best-effort and non-blocking; never raises into the loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# gw-card callback_data prefixes (task + state cards).
GW_CARD_PREFIXES = ("tc", "tcr", "tcx", "tcp", "tcb", "mtk", "treo",
                    "gst", "gsu")

_PY = str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python")
_SCRIPT = str(Path.home() / ".config" / "gw" / "cards-bot"
              / "manufacturia-card-callback.py")


def is_gw_card(data: str | None) -> bool:
    return bool(data) and data.split("|", 1)[0] in GW_CARD_PREFIXES


async def handle_gw_card_callback(query, data: str,
                                  adapter_name: str = "telegram") -> bool:
    """Forward a gw-card callback_query to the standalone handler process.
    query is a python-telegram-bot CallbackQuery; we serialise it to the raw
    Telegram dict the handler expects. Non-blocking (async subprocess)."""
    try:
        cq = query.to_dict()
    except Exception:  # noqa: BLE001
        frm = getattr(query, "from_user", None)
        cq = {"id": getattr(query, "id", ""), "data": data,
              "from": {"id": getattr(frm, "id", 0)}}
    try:
        proc = await asyncio.create_subprocess_exec(
            _PY, _SCRIPT, json.dumps(cq),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            logger.warning("[%s] gw-card handler rc=%s err=%s", adapter_name,
                           proc.returncode, (err or b"").decode()[:300])
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("[%s] gw-card subprocess failed: %s", adapter_name, exc)
        return False
