"""Optional bridge to a GW Cards handler rendered into this Hermes checkout.

GW owns the cards contract and renders ``tools.gw_card_handler`` during its
host-specific installation. Hermes does not manufacture a command list or a
callback prefix list: it imports that generated module only when it is present
and uses the module's own predicates. No handler means no interception and no
advertising.
"""
from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from types import ModuleType


logger = logging.getLogger(__name__)


# A module-name import is not an installation boundary: another checkout (or a
# prior import) can supply ``tools.gw_card_handler`` through sys.path.  The
# generated handler belongs to THIS Hermes checkout unless an operator has
# explicitly supplied a different file for this process.
HANDLER_PATH_ENV = "HERMES_GW_CARDS_HANDLER_PATH"


def _handler_path() -> Path | None:
    override = os.environ.get(HANDLER_PATH_ENV)
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            logger.error("GW Cards handler override must be an absolute path")
            return None
        return path
    return Path(__file__).resolve().parents[1] / "tools" / "gw_card_handler.py"


def handler() -> ModuleType | None:
    """Return a complete generated handler, or ``None`` without side effects."""
    path = _handler_path()
    if path is None or not path.is_file():
        return None
    try:
        # Do not import by the handler's public-looking package name.  The
        # private name and explicit spec make the selected file, rather than
        # sys.path or sys.modules, the authority for a Cards installation.
        spec = importlib.util.spec_from_file_location(
            "_hermes_gw_cards_handler", path
        )
        if spec is None or spec.loader is None:
            logger.error("GW Cards handler has no loadable module spec")
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
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
