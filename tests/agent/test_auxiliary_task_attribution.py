"""[LOCAL MOD] Auxiliary clients for an explicit provider carry their task.

caproute's history held no ``aux.*`` operation at all: call_llm resolved the
compression endpoint (provider ``custom``) and asked ``_get_cached_client``
for a client without ``task=``, so ``for_task`` saw None and the client was
built with the job's operation. The cache key also left the task out for
non-``auto`` providers, so one task's client (and its headers) could serve
another task.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from agent.auxiliary_client import _client_cache_key, async_call_llm, call_llm

ENDPOINT = ("custom", "thinking_no", "http://localhost:8800/v1", None, None)


def _client(async_mode=False):
    client = MagicMock()
    client.base_url = ENDPOINT[2]
    if async_mode:
        client.chat.completions.create = AsyncMock(return_value={"ok": True})
    else:
        client.chat.completions.create.return_value = {"ok": True}
    return client


def _run(async_mode):
    client = _client(async_mode)
    with patch("agent.auxiliary_client._resolve_task_provider_model", return_value=ENDPOINT), \
         patch("agent.auxiliary_client._get_cached_client",
               return_value=(client, "thinking_no")) as cached, \
         patch("agent.auxiliary_client._validate_llm_response",
               side_effect=lambda resp, _task, **_kw: resp):
        messages = [{"role": "user", "content": "x"}]
        if async_mode:
            asyncio.run(async_call_llm(task="compression", messages=messages))
        else:
            call_llm(task="compression", messages=messages)
    return cached.call_args.kwargs


def test_sync_call_passes_task_to_client_cache():
    assert _run(async_mode=False).get("task") == "compression"


def test_async_call_passes_task_to_client_cache():
    assert _run(async_mode=True).get("task") == "compression"


def test_cache_key_separates_tasks_for_explicit_provider():
    def key(task):
        return _client_cache_key("custom", async_mode=False, base_url=ENDPOINT[2], task=task)

    assert len({key(None), key("compression"), key("approval")}) == 3
    assert key("compression") == key("compression")
