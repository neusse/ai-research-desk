"""The ChatGPT plan path (desk/chatgpt.py through maxplan), with Codex faked. No model is called."""

from pathlib import Path

import pytest

from desk import chatgpt, maxplan


def test_roles_map_to_the_strong_and_fast_models():
    assert chatgpt.model_for("opus") == chatgpt.STRONG == chatgpt.model_for("claude-opus-5-5")
    assert chatgpt.model_for("sonnet") == chatgpt.FAST == chatgpt.model_for("claude-sonnet-5")
    assert chatgpt.model_for("gpt-6-sol") == "gpt-6-sol" and chatgpt.model_for("o4") == "o4"


def test_every_call_is_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(chatgpt, "codex_exe", lambda: ["codex"])
    monkeypatch.delenv("DESK_CHATGPT_EFFORT", raising=False)
    cmd = chatgpt.command("gpt-6-luna", "medium", tmp_path, tmp_path / "s.md", tmp_path / "x.json", tmp_path / "o.txt")
    joined = " ".join(cmd)
    for flag in ("--ignore-user-config", "--ignore-rules", "project_doc_max_bytes=0", "model_instructions_file=",
                 "include_environment_context=false", 'web_search="disabled"', "features.apps=false", "--sandbox read-only",
                 "--ephemeral", "--output-schema", 'model_reasoning_effort="medium"'):
        assert flag in joined, flag
    assert "--dangerously" not in joined and cmd[-1] == "-"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DESK_CODEX_HOME", str(tmp_path / "home"))
    env = chatgpt.child_env()
    assert "OPENAI_API_KEY" not in env and env["CODEX_HOME"] == str(tmp_path / "home")


def test_desk_effort_overrides_an_engines_default(monkeypatch, tmp_path):
    monkeypatch.setattr(chatgpt, "codex_exe", lambda: ["codex"])
    monkeypatch.setenv("DESK_CHATGPT_EFFORT", "max")
    cmd = chatgpt.command("gpt-6.1-sol", "medium", tmp_path, tmp_path / "s.md", None,
                          tmp_path / "o.txt")
    joined = " ".join(cmd)
    assert 'model_reasoning_effort="max"' in joined
    assert 'model_reasoning_effort="medium"' not in joined


def test_role_effort_overrides_only_its_model_lane(monkeypatch, tmp_path):
    monkeypatch.delenv("DESK_CHATGPT_EFFORT", raising=False)
    monkeypatch.setenv("DESK_CHATGPT_STRONG_EFFORT", "medium")
    monkeypatch.setenv("DESK_CHATGPT_FAST_EFFORT", "max")
    monkeypatch.setattr(chatgpt, "STRONG", "gpt-manager")
    monkeypatch.setattr(chatgpt, "FAST", "gpt-analyst")
    files = [tmp_path / n for n in ("system", "schema", "last")]

    strong = " ".join(chatgpt.command(chatgpt.STRONG, "high", tmp_path, *files))
    fast = " ".join(chatgpt.command(chatgpt.FAST, "high", tmp_path, *files))

    assert 'model_reasoning_effort="medium"' in strong
    assert 'model_reasoning_effort="max"' in fast


def test_a_command_or_tool_in_the_event_stream_is_caught():
    events = "\n".join(['{"type":"item.completed","item":{"type":"reasoning"}}',
                        '{"type":"item.completed","item":{"type":"command_execution"}}',
                        '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":3}}'])
    ev = chatgpt.read_events(events)
    assert ev["stray"] == ["command_execution"] and ev["usage"]["input_tokens"] == 12


def _fake(monkeypatch, **result):
    base = {"text": '{"rating": "Hold"}', "data": {"rating": "Hold"}, "model": "gpt-6-luna", "seconds": 1.0,
            "failed": False, "error": "", "stray": [], "usage": {"input": 10, "output": 2}}
    monkeypatch.setattr(chatgpt, "run", lambda *a, **k: {**base, **result})
    monkeypatch.setenv("DESK_PLAN", "chatgpt")


SCHEMA = {"type": "object", "properties": {"rating": {"type": "string", "enum": ["Buy", "Hold", "Sell"]}},
          "required": ["rating"], "additionalProperties": False}


def test_a_chatgpt_answer_is_checked_like_a_claude_one(monkeypatch):
    _fake(monkeypatch)
    assert maxplan.ask("q", "s", model="opus", schema=SCHEMA).data == {"rating": "Hold"}
    assert maxplan.summary()["billing"] == "chatgpt"
    _fake(monkeypatch, data={"rating": "Potato"}, text='{"rating": "Potato"}')
    monkeypatch.setattr(maxplan.time, "sleep", lambda s: None)
    with pytest.raises(maxplan.MaxCallError, match="schema"):
        maxplan.ask("q", "s", schema=SCHEMA)


def test_tool_use_fails_the_call(monkeypatch):
    _fake(monkeypatch, stray=["command_execution"])
    monkeypatch.setattr(maxplan.time, "sleep", lambda s: None)
    with pytest.raises(maxplan.MaxCallError, match="may only answer"):
        maxplan.ask("q", "s", schema=SCHEMA)


@pytest.mark.parametrize("notice", ["You've hit your usage limit. Try again later.",
                                    "Not logged in. Run codex login.", "401 Unauthorized"])
def test_a_spent_plan_or_an_expired_login_stops_the_run(monkeypatch, notice):
    _fake(monkeypatch, failed=True, text="", data=None, error=notice)
    with pytest.raises(maxplan.UsageLimit):
        maxplan.ask("q", "s", schema=SCHEMA)


def test_the_default_plan_is_still_claude(monkeypatch):
    monkeypatch.delenv("DESK_PLAN", raising=False)
    assert maxplan.plan() == "claude"


ENVELOPE = {"type": "object", "properties": {
    "action": {"type": "string", "enum": ["tool_calls", "final"]},
    "tool_calls": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["name", "arguments"]}},
    "content": {"type": "string", "maxLength": 5000}}, "required": ["action"]}


def test_a_loose_schema_is_made_strict_and_the_answer_is_translated_back():
    s = chatgpt.strict(ENVELOPE)
    assert s["additionalProperties"] is False and s["required"] == ["action", "tool_calls", "content"]
    item = s["properties"]["tool_calls"]["anyOf"][0]["items"]
    assert item["properties"]["arguments"]["type"] == "string" and item["additionalProperties"] is False
    assert "maxLength" not in str(s)
    answer = {"action": "tool_calls", "content": None,
              "tool_calls": [{"name": "get_stock_data", "arguments": '{"symbol": "AKAM"}'}]}
    back = chatgpt.unstrict(answer, ENVELOPE)
    assert back == {"action": "tool_calls", "tool_calls": [{"name": "get_stock_data", "arguments": {"symbol": "AKAM"}}]}
    assert maxplan.schema_problem(back, ENVELOPE) is None
