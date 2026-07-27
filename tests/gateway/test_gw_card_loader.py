"""[LOCAL] gw-card optional-capability loader (gw#51).

The gateway delegates gw-card taps and commands to an OPTIONAL module,
``tools.gw_card_handler``. Whether that module is present is a per-host fact:
on a cards host it is installed; on every other host its absence is the normal
steady state, and gw#36 makes that permanent for two more hosts.

So the loader has to distinguish two things a bare ``except ImportError``
cannot:

    module simply not installed  -> expected. MUST be silent, or a host that
                                    does not run cards logs a line for every
                                    tap and every slash command, and the one
                                    line that matters is never noticed.
    module present but unusable  -> a real fault, and MUST be reported.

The callback path was given that discrimination in gw#46. The command path
kept a broad ``except Exception`` with an unconditional ``logger.error``, so a
live drill on a cards host with the handler moved away produced
``gw-card command hook error: No module named 'tools.gw_card_handler'`` for a
single slash command. This pins both paths against the same loader.

These tests build real packages on disk rather than faking ``sys.modules``:
the guard turns on ``ModuleNotFoundError.name``, and a missing SUBMODULE
(``tools.gw_card_handler``) reports a different name than a missing PACKAGE
(``tools``). A fake that skips the package gets a different exception than
production and would prove the wrong thing.

Stdlib + pytest only; no network, no gateway, no live host.
"""

import importlib.util
import logging
import sys

import pytest

from gateway.platforms.telegram import (
    _gw_card_command_hooks,
    _gw_card_hooks,
)

COMPLETE = """
def is_gw_card(data): return True
def handle_gw_card_callback(*a): pass
def is_gw_card_command(text): return True
def handle_gw_card_command(*a): pass
"""

# A stale deployment: the callback pair shipped, the command pair did not.
PARTIAL = """
def is_gw_card(data): return True
def handle_gw_card_callback(*a): pass
"""

BROKEN = "import a_module_that_does_not_exist_xyz\n"


def _resolves_tools(finder):
    """Can this meta-path finder resolve the real ``tools`` package?"""
    find_spec = getattr(finder, "find_spec", None)
    if find_spec is None:
        return False
    try:
        return find_spec("tools", None) is not None
    except Exception:
        return False


@pytest.fixture
def isolate_tools(monkeypatch):
    """Make the test's ``tools`` package the only one that can be imported.

    hermes-agent is installed editable, which puts a custom finder on
    ``sys.meta_path`` resolving ``tools`` to the real checkout. Meta-path
    finders run BEFORE ``sys.path``, so prepending a directory does not
    override it — an earlier version of this file did exactly that and the
    'absent' cases silently imported the live handler instead.
    """
    monkeypatch.setattr(
        sys, "meta_path",
        [f for f in sys.meta_path if not _resolves_tools(f)],
        raising=False)


@pytest.fixture
def tools_pkg(tmp_path, monkeypatch, isolate_tools):
    """Install a real importable ``tools`` package and yield a writer for its
    optional ``gw_card_handler`` submodule.

    Real files on disk, not a ``sys.modules`` fake: the loader's guard turns
    on ``ModuleNotFoundError.name``, and only genuine import machinery
    produces the distinction between a missing SUBMODULE and a missing
    PACKAGE that the guard depends on.
    """
    pkg = tmp_path / "tools"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))

    def write(source):
        if source is not None:
            (pkg / "gw_card_handler.py").write_text(source)
        for name in [n for n in sys.modules
                     if n == "tools" or n.startswith("tools.")]:
            monkeypatch.delitem(sys.modules, name, raising=False)

    return write


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


class TestAbsentIsSilent:
    """The gw#36 steady state. This is the whole point of the loader."""

    def test_both_paths_fail_open_without_logging(self, tools_pkg, caplog):
        tools_pkg(None)
        with caplog.at_level(logging.DEBUG):
            assert _gw_card_hooks("tg") == (None, None)
            assert _gw_card_command_hooks("tg") == (None, None)
        assert _errors(caplog) == [], (
            "a host that does not run cards must not log for an absent "
            "optional module — that is one line per tap and per command"
        )

    def test_command_path_silent_for_repeated_commands(self, tools_pkg, caplog):
        """The observed defect: one error per slash command, forever."""
        tools_pkg(None)
        with caplog.at_level(logging.DEBUG):
            for _ in range(5):
                _gw_card_command_hooks("tg")
        assert _errors(caplog) == []


class TestPresentIsUsable:
    def test_both_pairs_are_returned(self, tools_pkg, caplog):
        tools_pkg(COMPLETE)
        with caplog.at_level(logging.DEBUG):
            is_card, handle = _gw_card_hooks("tg")
            is_cmd, handle_cmd = _gw_card_command_hooks("tg")
        assert all(callable(f) for f in (is_card, handle, is_cmd, handle_cmd))
        assert _errors(caplog) == []


class TestUnusableIsLoud:
    """Everything that is not plain absence has to be reported."""

    def test_partial_module_reports_the_missing_pair(self, tools_pkg, caplog):
        tools_pkg(PARTIAL)
        with caplog.at_level(logging.DEBUG):
            assert _gw_card_hooks("tg")[0] is not None      # callbacks fine
            assert _gw_card_command_hooks("tg") == (None, None)
        msgs = [r.getMessage() for r in _errors(caplog)]
        assert any("is_gw_card_command" in m for m in msgs), msgs
        assert not any("is_gw_card_command" in m for m in msgs
                       if "handle_gw_card_callback" in m)

    def test_broken_module_is_reported_not_swallowed(self, tools_pkg, caplog):
        tools_pkg(BROKEN)
        with caplog.at_level(logging.DEBUG):
            assert _gw_card_hooks("tg") == (None, None)
        msgs = [r.getMessage() for r in _errors(caplog)]
        assert any("imports fail" in m for m in msgs), msgs

    def test_missing_tools_package_is_reported(self, monkeypatch, caplog,
                                               isolate_tools):
        """A missing PACKAGE is not the same as a missing submodule.

        The repo ships ``tools/``, so its absence is a real fault and stays
        loud — the guard only silences the exact submodule name. Note this
        depends on ``isolate_tools``: without it an editable install resolves
        ``tools`` anyway and the case cannot be constructed at all.
        """
        for name in [n for n in sys.modules
                     if n == "tools" or n.startswith("tools.")]:
            monkeypatch.delitem(sys.modules, name, raising=False)
        assert importlib.util.find_spec("tools") is None, (
            "isolate_tools failed: a real tools package is still importable"
        )
        with caplog.at_level(logging.DEBUG):
            assert _gw_card_hooks("tg") == (None, None)
        assert _errors(caplog) != []
