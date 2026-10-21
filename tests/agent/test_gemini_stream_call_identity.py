"""Completed native calls remain distinct across event-local part positions."""

import json

import httpx
import pytest


def _event(arguments, *, signed=False, finish=None):
    parts = [{"functionCall": {"name": "read_file", "args": args}} for args in arguments]
    if signed:
        parts[0]["thoughtSignature"] = "synthetic-signature"
    candidate = {"content": {"parts": parts}}
    if finish:
        candidate["finishReason"] = finish
    return {"candidates": [candidate]}


@pytest.mark.parametrize("identical", [False, True])
def test_completed_calls_in_separate_events_have_independent_identity(identical):
    from agent.gemini_native_adapter import translate_stream_event

    first = {"path": "/srv/example/a.txt"}
    second = first if identical else {"path": "/srv/example/b.txt"}
    state = {}
    calls = []
    for args in (first, second):
        chunks = translate_stream_event(_event([args]), "synthetic-model", state)
        calls.extend(chunk.choices[0].delta.tool_calls[0] for chunk in chunks)

    assert [call.index for call in calls] == [0, 1]
    assert len({call.id for call in calls}) == 2
    assert [json.loads(call.function.arguments) for call in calls] == [first, second]


def test_signed_first_call_does_not_merge_later_unsigned_calls():
    from agent.gemini_native_adapter import translate_stream_event

    args = [{"path": f"/srv/example/{name}.txt"} for name in ("a", "b", "c")]
    state = {}
    calls = []
    for index, value in enumerate(args):
        chunks = translate_stream_event(_event([value], signed=index == 0), "synthetic-model", state)
        calls.extend(chunk.choices[0].delta.tool_calls[0] for chunk in chunks)

    assert [call.index for call in calls] == [0, 1, 2]
    assert len({call.id for call in calls}) == 3
    assert [json.loads(call.function.arguments) for call in calls] == args
    assert calls[0].extra_content == {"google": {"thought_signature": "synthetic-signature"}}
    assert getattr(calls[1], "extra_content", None) is None


@pytest.mark.parametrize("last_name", ["b", "c"])
def test_native_sse_parallel_calls_survive_real_stream_accumulator(monkeypatch, last_name):
    from agent.gemini_native_adapter import GeminiNativeClient, translate_gemini_response
    from run_agent import AIAgent

    arguments = [{"path": f"/srv/example/{name}.txt"} for name in ("a", "b", last_name)]
    events = [_event([args], signed=index == 0) for index, args in enumerate(arguments)]
    events.append({
        "candidates": [{"finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 10, "totalTokenCount": 30},
    })
    wire = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()

    class SyntheticStream(httpx.SyncByteStream):
        def __iter__(self):
            for offset in range(0, len(wire), 37):
                yield wire[offset:offset + 37]

    def handler(request):
        assert request.url.path.endswith(":streamGenerateContent")
        return httpx.Response(200, stream=SyntheticStream(), headers={"content-type": "text/event-stream"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        native = GeminiNativeClient(api_key="synthetic-key", http_client=http)
        agent = AIAgent(
            api_key="synthetic-key", base_url="https://api.example.com/v1", provider="custom",
            model="test/model", quiet_mode=True, skip_context_files=True, skip_memory=True,
        )
        agent.api_mode = "chat_completions"
        monkeypatch.setattr(agent, "_create_request_openai_client", lambda *a, **kw: native)
        monkeypatch.setattr(agent, "_close_request_openai_client", lambda *a, **kw: None)
        response = agent._interruptible_streaming_api_call({
            "model": "synthetic-model", "messages": [{"role": "user", "content": "Read synthetic fixtures."}],
        })

    calls = response.choices[0].message.tool_calls
    assert response.choices[0].finish_reason == "tool_calls"
    assert len(calls) == 3
    assert len({call.id for call in calls}) == 3
    assert [json.loads(call.function.arguments) for call in calls] == arguments
    assert calls[0].extra_content == {"google": {"thought_signature": "synthetic-signature"}}
    assert response.usage.completion_tokens == 10

    nonstream = translate_gemini_response(_event(arguments, signed=True, finish="STOP"), "synthetic-model")
    assert [json.loads(call.function.arguments) for call in calls] == [
        json.loads(call.function.arguments) for call in nonstream.choices[0].message.tool_calls
    ]


def test_true_token_exhaustion_still_reports_length():
    from agent.gemini_native_adapter import translate_stream_event

    event = {
        "candidates": [{"content": {"parts": [{"text": "Incomplete response"}]}, "finishReason": "MAX_TOKENS"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3, "totalTokenCount": 13},
    }
    chunks = translate_stream_event(event, "synthetic-model", {})
    assert chunks[0].choices[0].delta.content == "Incomplete response"
    assert chunks[-1].choices[0].finish_reason == "length"
    assert chunks[-1].usage.completion_tokens == 3
