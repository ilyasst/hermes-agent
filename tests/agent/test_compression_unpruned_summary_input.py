"""[LOCAL MOD] The summarizer reads real tool outputs, not Phase 1 stubs.

Phase 1 of compress() replaces old tool results with one-line stubs before
the summary is written. Without the local mod the summarizer only saw lines
like "[terminal] ran `rg x` -> exit 0, 1 lines output" and could not keep any
finding, or reported real search results as empty.
"""
import json
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor, _summarize_tool_result


@pytest.fixture()
def compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(
            model="test/model",
            threshold_percent=0.5,
            protect_first_n=2,
            protect_last_n=4,
            quiet_mode=True,
        )


def _transcript(pairs=30):
    messages = [
        {"role": "system", "content": "You are a research agent."},
        {"role": "user", "content": "Find who confirmed the lab booking."},
    ]
    for i in range(pairs):
        call_id = f"call-{i}"
        messages.append({"role": "assistant", "content": f"Searching source {i}.", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "terminal", "arguments": json.dumps({"command": f"research-search q{i}"})}}]})
        output = f"FINDING-{i}: mail UID {9000 + i} from Person {i} confirms item {i}.\n" + ("detail line\n" * 300)
        messages.append({"role": "tool", "tool_call_id": call_id,
                         "content": json.dumps({"output": output, "exit_code": 0})})
    messages.append({"role": "user", "content": "Continue."})
    return messages


def test_terminal_stub_counts_decoded_lines_and_keeps_a_preview():
    content = json.dumps({"output": "first hit: UID 93701 sent 2026-11-06\nsecond\nthird\n", "exit_code": 0})
    stub = _summarize_tool_result("terminal", json.dumps({"command": "outlook search x"}), content)
    assert "3 lines output" in stub
    assert "UID 93701" in stub


def test_terminal_stub_for_empty_output_has_no_preview():
    stub = _summarize_tool_result("terminal", json.dumps({"command": "true"}),
                                  json.dumps({"output": "", "exit_code": 0}))
    assert stub.endswith("0 lines output")


def test_summarizer_reads_unpruned_tool_results(compressor):
    seen = {}

    def fake_summary(self, turns, *args, **kwargs):
        seen["text"] = self._serialize_for_summary(turns)
        return "## Goal\nsynthetic summary"

    with patch.object(ContextCompressor, "_generate_summary", fake_summary):
        compressor.compress(_transcript(), current_tokens=90000, force=True)
    text = seen.get("text", "")
    assert text, "compression did not reach the summarizer"
    # Old results were stubbed by Phase 1, yet their findings reach the summary.
    assert "FINDING-1: mail UID 9001 from Person 1" in text
    assert "FINDING-5: mail UID 9005" in text
    assert "lines output" not in text.split("FINDING-1", 1)[1][:200]


def test_summarizer_input_stays_capped(compressor):
    seen = {}

    def fake_summary(self, turns, *args, **kwargs):
        seen["text"] = self._serialize_for_summary(turns)
        return "## Goal\nsynthetic summary"

    with patch.object(ContextCompressor, "_generate_summary", fake_summary):
        compressor.compress(_transcript(pairs=80), current_tokens=95000, force=True)
    # Per-message cap still applies to restored originals.
    for chunk in seen["text"].split("[TOOL RESULT ")[1:]:
        assert len(chunk) <= ContextCompressor._CONTENT_MAX + 200
