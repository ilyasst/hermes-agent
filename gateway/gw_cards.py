"""Optional bridge to a GW Cards handler rendered into this Hermes checkout.

GW owns the cards contract and renders ``tools.gw_card_handler`` during its
host-specific installation. Hermes does not manufacture a command list or a
callback prefix list: it imports that generated module only when it is present
and uses the module's own predicates. No handler means no interception and no
advertising.
"""
from __future__ import annotations

import importlib
import logging
from types import ModuleType


logger = logging.getLogger(__name__)


def handler() -> ModuleType | None:
    """Return a complete generated handler, or ``None`` without side effects."""
    try:
        mod = importlib.import_module("tools.gw_card_handler")
    except ModuleNotFoundError:
        return None
    except Exception:
        logger.exception("GW Cards handler could not be imported; leaving update untouched")
        return None

    required = (
        "is_gw_card",
        "handle_gw_card_callback",
        "is_gw_card_command",
        "handle_gw_card_command",
    )
    if not all(callable(getattr(mod, name, None)) for name in required):
        logger.error("GW Cards handler is incomplete; leaving update untouched")
        return None
    return mod


def command_menu_entries() -> list[tuple[str, str]]:
    """Menu entries the installed handler can actually dispatch.

    The generated handler exposes its declared commands and reads the
    host-specific artifact itself. Descriptions are optional presentation
    metadata: a malformed artifact may lose them, but must never add a command
    the handler did not declare.
    """
    mod = handler()
    if mod is None:
        return []

    commands = tuple(
        command.lstrip("/").strip()
        for command in (getattr(mod, "GW_CARD_COMMANDS", ()) or ())
        if isinstance(command, str) and command.lstrip("/").strip()
    )
    if not commands:
        return []

    descriptions: dict[str, str] = {}
    try:
        artifact = mod._artifact()
        for entry in artifact.get("command_menu") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("command") or "").lstrip("/").strip()
            if name:
                descriptions[name] = str(entry.get("description") or "GW task cards")
    except Exception:
        pass

    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for command in commands:
        if command not in seen:
            seen.add(command)
            out.append((command, descriptions.get(command, "GW task cards")))
    return out
