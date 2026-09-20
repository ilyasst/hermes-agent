#!/usr/bin/env python3
"""FORK-LOCAL: caproute demand attribution headers.

Our fleet routes through caproute, which decides model placement from
recorded demand. caproute can name the process on the other end of the
socket but not what a call was for; only the caller knows that. foxhound
spawns one `hermes chat` per task, so the parent exports CAPROUTE_* and
this process stamps every call with it.

The contract these pin: silent when unasked, bounded when asked, and
never carrying anything but ids.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock


class _Stub:
    """Just the surface the methods touch, with the real ones bound on.

    Not an AIAgent: constructing one pulls the whole runtime. Binding the
    two methods under test keeps this a unit test of the header logic.
    """

    def __init__(self, existing=None):
        import run_agent
        self._client_kwargs = {}
        if existing is not None:
            self._client_kwargs["default_headers"] = existing
        for name in ("_caproute_attribution_headers",
                     "_with_caproute_attribution"):
            setattr(self, name,
                    getattr(run_agent.AIAgent, name).__get__(self))


def _apply(stub, env):
    """Headers as they would reach the OpenAI client, or None."""
    import run_agent
    with mock.patch.dict(os.environ, env, clear=False):
        for key in ("CAPROUTE_OPERATION", "CAPROUTE_JOB", "CAPROUTE_RUN_ID",
                    "CAPROUTE_WORK_ITEM_TYPE", "CAPROUTE_WORK_ITEM_ID",
                    "CAPROUTE_APP"):
            if key not in env:
                os.environ.pop(key, None)
        merged = stub._with_caproute_attribution(dict(stub._client_kwargs))
    return merged.get("default_headers")


class CaprouteAttributionTests(unittest.TestCase):
    def test_no_env_means_no_headers_at_all(self):
        """Upstream behaviour must be untouched when nobody asked."""
        self.assertIsNone(_apply(_Stub(), {}))

    def test_no_env_does_not_disturb_existing_headers(self):
        existing = {"User-Agent": "curl/8.7.1"}
        self.assertEqual(_apply(_Stub(existing), {}), existing)

    def test_a_parent_job_is_stamped_on_the_request(self):
        got = _apply(_Stub(), {"CAPROUTE_OPERATION": "execute",
                               "CAPROUTE_RUN_ID": "run-7"})
        self.assertEqual(got["X-Caproute-Operation"], "execute")
        self.assertEqual(got["X-Caproute-Run-Id"], "run-7")
        self.assertEqual(got["X-Caproute-App"], "hermes")
        self.assertEqual(got["X-Caproute-Pid"], str(os.getpid()))

    def test_the_app_name_can_be_overridden_by_the_parent(self):
        got = _apply(_Stub(), {"CAPROUTE_OPERATION": "plan",
                               "CAPROUTE_APP": "foxhound"})
        self.assertEqual(got["X-Caproute-App"], "foxhound")

    def test_existing_headers_survive_alongside(self):
        got = _apply(_Stub({"User-Agent": "curl/8.7.1"}),
                     {"CAPROUTE_JOB": "nightly"})
        self.assertEqual(got["User-Agent"], "curl/8.7.1")
        self.assertEqual(got["X-Caproute-Job"], "nightly")

    def test_control_characters_are_stripped(self):
        """These become HTTP headers and land in a router's log."""
        got = _apply(_Stub(), {"CAPROUTE_JOB": "nig\r\nInjected: yes\thtly"})
        self.assertNotIn("\r", got["X-Caproute-Job"])
        self.assertNotIn("\n", got["X-Caproute-Job"])
        self.assertNotIn("\t", got["X-Caproute-Job"])

    def test_values_are_bounded(self):
        got = _apply(_Stub(), {"CAPROUTE_RUN_ID": "x" * 500})
        self.assertEqual(len(got["X-Caproute-Run-Id"]), 160)

    def test_an_empty_value_is_not_a_request_for_headers(self):
        self.assertIsNone(_apply(_Stub(), {"CAPROUTE_OPERATION": "   "}))

    def test_app_alone_does_not_trigger_stamping(self):
        """caproute already knows we are hermes; only context is new."""
        self.assertIsNone(_apply(_Stub(), {"CAPROUTE_APP": "foxhound"}))

    def test_every_field_maps_to_its_header(self):
        got = _apply(_Stub(), {
            "CAPROUTE_OPERATION": "review", "CAPROUTE_JOB": "j",
            "CAPROUTE_RUN_ID": "r", "CAPROUTE_WORK_ITEM_TYPE": "task",
            "CAPROUTE_WORK_ITEM_ID": "T12"})
        self.assertEqual(got["X-Caproute-Operation"], "review")
        self.assertEqual(got["X-Caproute-Job"], "j")
        self.assertEqual(got["X-Caproute-Run-Id"], "r")
        self.assertEqual(got["X-Caproute-Work-Item-Type"], "task")
        self.assertEqual(got["X-Caproute-Work-Item-Id"], "T12")



class ClientConstructionTests(unittest.TestCase):
    """The hook must sit on the path a normal run actually takes.

    It was first placed in `_apply_client_headers_for_base_url`, which is
    reached only on credential-refresh paths. A real agent turn never
    called it, so the headers were built and never sent — the router still
    logged `operation=unknown`. `_create_openai_client` is the single
    documented `OpenAI(**client_kwargs)` call site.
    """

    def test_every_openai_construction_site_is_stamped(self):
        """Both of them. Stamping only the obvious one was the bug.

        The agent's own client carried the headers while the auxiliary
        client — the one that actually serves a chat turn — did not, so
        every real request stayed unlabelled and the tests still passed.
        """
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[1]
        sites = []
        for path in ((root / "agent" / "agent_runtime_helpers.py"),
                     (root / "agent" / "auxiliary_client.py")):
            text = path.read_text()
            for match in re.finditer(r"^\s*(?:return |client = )[^\n]*OpenAI\(",
                                     text, re.M):
                # Vertex mints its own token against Google, not caproute.
                if "base_url=base_url)" in text[match.start():match.end() + 40] \
                        and "get_vertex_config" in text[
                            max(0, match.start() - 900):match.start()]:
                    continue
                window = text[max(0, match.start() - 400):match.start()]
                sites.append((path.name, "with_attribution" in window))
        self.assertTrue(sites, "no OpenAI() construction sites found")
        unstamped = [name for name, stamped in sites if not stamped]
        self.assertEqual(unstamped, [], f"unstamped OpenAI() sites: {unstamped}")

    def test_kwargs_are_untouched_when_nothing_is_asked(self):
        import run_agent
        stub = _Stub()
        original = {"api_key": "k", "base_url": "u"}
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("CAPROUTE_OPERATION", "CAPROUTE_JOB",
                        "CAPROUTE_RUN_ID", "CAPROUTE_WORK_ITEM_TYPE",
                        "CAPROUTE_WORK_ITEM_ID"):
                os.environ.pop(key, None)
            got = stub._with_caproute_attribution(original)
        self.assertIs(got, original)

    def test_the_callers_kwargs_are_not_mutated(self):
        import run_agent
        stub = _Stub()
        original = {"api_key": "k"}
        with mock.patch.dict(os.environ, {"CAPROUTE_RUN_ID": "r"},
                             clear=False):
            got = stub._with_caproute_attribution(original)
        self.assertNotIn("default_headers", original)
        self.assertIn("X-Caproute-Run-Id", got["default_headers"])


if __name__ == "__main__":
    unittest.main()
