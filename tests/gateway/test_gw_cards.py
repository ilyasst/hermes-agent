"""The optional GW Cards bridge claims only a complete generated handler."""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType

from gateway import gw_cards


def _handler(*, commands=("/gwtasks",), artifact=None):
    mod = ModuleType("tools.gw_card_handler")
    mod.GW_CARD_COMMANDS = commands
    mod.is_gw_card = lambda _data: False
    mod.is_gw_card_command = lambda _text: False

    async def callback(*_args):
        return None

    async def command(*_args):
        return None

    mod.handle_gw_card_callback = callback
    mod.handle_gw_card_command = command
    mod._artifact = lambda: artifact if artifact is not None else {}
    return mod


def test_absent_handler_advertises_nothing(monkeypatch):
    monkeypatch.delitem(sys.modules, "tools.gw_card_handler", raising=False)
    assert gw_cards.command_menu_entries() == []


def test_menu_is_bound_to_handler_commands_not_artifact(monkeypatch):
    mod = _handler(
        commands=("/gwtasks", "/oldest", "/gwtasks"),
        artifact={"command_menu": [
            {"command": "gwtasks", "description": "Task backlog"},
            {"command": "foreign", "description": "Must not be exposed"},
        ]},
    )
    monkeypatch.setitem(sys.modules, "tools.gw_card_handler", mod)

    assert gw_cards.command_menu_entries() == [
        ("gwtasks", "Task backlog"),
        ("oldest", "GW task cards"),
    ]


def test_incomplete_handler_is_not_a_command_owner(monkeypatch):
    mod = ModuleType("tools.gw_card_handler")
    mod.GW_CARD_COMMANDS = ("/gwtasks",)
    monkeypatch.setitem(sys.modules, "tools.gw_card_handler", mod)

    assert gw_cards.handler() is None
    assert gw_cards.command_menu_entries() == []


def test_bad_artifact_keeps_only_dispatchable_generic_entries(monkeypatch):
    mod = _handler(commands=("/gwtasks",))
    mod._artifact = lambda: (_ for _ in ()).throw(ValueError("bad artifact"))
    monkeypatch.setitem(sys.modules, "tools.gw_card_handler", mod)

    assert gw_cards.command_menu_entries() == [("gwtasks", "GW task cards")]


def test_telegram_adapter_dispatches_the_same_generated_handler(monkeypatch):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    seen = []
    mod = _handler()
    mod.is_gw_card = lambda data: data == "card|1"
    mod.is_gw_card_command = lambda text: text == "/gwtasks"

    async def callback(query, data, adapter_name):
        seen.append(("callback", query, data, adapter_name))

    async def command(message, text, adapter_name):
        seen.append(("command", message, text, adapter_name))

    mod.handle_gw_card_callback = callback
    mod.handle_gw_card_command = command
    monkeypatch.setitem(sys.modules, "tools.gw_card_handler", mod)

    adapter = object.__new__(TelegramAdapter)
    monkeypatch.setattr(TelegramAdapter, "name", property(lambda _self: "telegram"))
    message = type("Message", (), {"text": "/gwtasks"})()
    adapter._effective_update_message = lambda _update: message

    asyncio.run(adapter._handle_command(object(), None))
    query = type("Query", (), {"data": "card|1"})()
    asyncio.run(adapter._handle_callback_query(
        type("Update", (), {"callback_query": query})(), None
    ))

    assert seen == [
        ("command", message, "/gwtasks", "telegram"),
        ("callback", query, "card|1", "telegram"),
    ]
