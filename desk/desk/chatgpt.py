"""The ChatGPT plan, through OpenAI's Codex CLI in non-interactive mode (`codex exec`).

The counterpart of the Claude path in maxplan.py: a run on DESK_PLAN=chatgpt sends every model
call here instead, billed to the user's ChatGPT subscription, never to an API key.

Codex is a coding agent, so every call is locked down to a plain question and answer:
  - its own home (DESK_CODEX_HOME, default ~/.hedge-desk/codex), logged in once with
    `codex login`, so the user's personal AGENTS.md, config, MCP servers and memories never reach
    the analysis. On the author's machine the shared home added 16k tokens of personal
    instructions to every call;
  - `--ignore-user-config` and `--ignore-rules`, and no project instructions;
  - the caller's system prompt replaces Codex's agent prompt (`model_instructions_file`), and the
    environment, permission, app and collaboration blocks are switched off;
  - `--sandbox read-only` in an empty scratch folder, `--ephemeral` (no session saved), web
    search and the account's apps server off; a canary file outside the folder stayed unread. A call whose event stream shows any command or tool use is rejected: the model
    was asked a question, not given a task;
  - OPENAI_API_KEY is removed from the child, so Codex cannot fall back to the metered API;
  - `--output-schema` asks for JSON in the caller's shape, and the answer is checked again in
    code (maxplan.schema_problem), as on the Claude path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CRED_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY")

# Role -> model. The desk's calls name Claude models (or the "opus" / "sonnet" roles); on this plan
# the strong role runs on STRONG and every other on FAST. Both are settings, because the model
# list on a ChatGPT plan changes over time.
STRONG = os.environ.get("DESK_CHATGPT_STRONG", "gpt-6.1-sol")
FAST = os.environ.get("DESK_CHATGPT_FAST", "gpt-6.1-sol")
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
ALLOWED_ITEMS = {"reasoning", "agent_message"}


class ChatGPTError(RuntimeError):
    """The call did not produce a usable answer."""


def home() -> Path:
    return Path(os.environ.get("DESK_CODEX_HOME") or Path.home() / ".hedge-desk" / "codex")


def model_for(model: str) -> str:
    """The ChatGPT model for a desk role or a Claude model id. A gpt-* name passes through."""
    m = (model or "").lower()
    if m.startswith("gpt-") or (len(m) > 1 and m[0] == "o" and m[1].isdigit()):
        return model
    return STRONG if ("opus" in m) else FAST


def codex_exe() -> list[str]:
    """The native codex.exe when it can be found (cmd.exe mangles long arguments), else the shim."""
    shim = shutil.which("codex")
    if not shim:
        raise ChatGPTError("The codex CLI is not on PATH. Install it with `npm install -g @openai/codex`, "
                           "then log the desk in once: see the README.")
    base = Path(shim).parent / "node_modules" / "@openai" / "codex"
    native = sorted(base.glob("node_modules/@openai/codex-*/vendor/*/bin/codex.exe")) if base.exists() else []
    if native:
        return [str(native[0])]
    if sys.platform == "win32" and shim.lower().endswith((".cmd", ".bat", ".ps1")):
        return ["cmd.exe", "/c", shim]
    return [shim]


def child_env() -> dict:
    env = dict(os.environ)
    for key in CRED_VARS:
        env.pop(key, None)
    env["CODEX_HOME"] = str(home())
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def command(model: str, effort: str | None, workdir: Path, system_file: Path,
            schema_file: Path | None, last_file: Path) -> list[str]:
    cmd = [*codex_exe(), "exec", "--ignore-user-config", "--ignore-rules",
           "-c", "project_doc_max_bytes=0",
           "-c", f"model_instructions_file={json.dumps(str(system_file))}",
           "-c", "include_environment_context=false", "-c", "include_permissions_instructions=false",
           "-c", "include_apps_instructions=false", "-c", "include_collaboration_mode_instructions=false",
           # Web search off under both of its names: tools.web_search is not enough, and the ChatGPT
           # account's own apps server (codex_apps) offers search too. Measured 2026-10-05: with
           # these, a prompt inviting a search and one asking to read a file both got neither.
           "-c", 'web_search="disabled"', "-c", "features.apps=false", "-c", 'approval_policy="never"',
           "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "--json",
           "-m", model, "-C", str(workdir), "-o", str(last_file)]
    # The global override remains the highest-precedence emergency knob. Normally the two model
    # lanes can use different effort: managers coordinate occasionally, while the many analyst
    # calls can be tuned independently. Read settings at call time so the dashboard needn't be
    # restarted after an environment-file reload.
    lane_effort = (os.environ.get("DESK_CHATGPT_STRONG_EFFORT") if model == STRONG else
                   os.environ.get("DESK_CHATGPT_FAST_EFFORT") if model == FAST else None)
    selected_effort = (os.environ.get("DESK_CHATGPT_EFFORT") or lane_effort or effort or "").lower()
    if selected_effort in EFFORTS:
        cmd += ["-c", f"model_reasoning_effort={json.dumps(selected_effort)}"]
    if schema_file:
        cmd += ["--output-schema", str(schema_file)]
    return [*cmd, "-"]


def read_events(stdout: str) -> dict:
    """Usage, the error text, and any item that is not reasoning or the answer."""
    usage, errors, stray = {}, [], []
    for line in stdout.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = e.get("type")
        if kind == "turn.completed":
            usage = e.get("usage") or {}
        elif kind in ("error", "turn.failed"):
            errors.append(str(e.get("message") or (e.get("error") or {}).get("message") or e))
        item = e.get("item") or {}
        if item.get("type") and item["type"] not in ALLOWED_ITEMS:
            stray.append(item["type"])
    return {"usage": usage, "errors": errors, "stray": stray}


# OpenAI's structured output accepts only "strict" schemas: every object closed and every
# property required, and a handful of keywords. The desk's schemas (and TradingAgents' tool
# envelope, whose `arguments` is an open object) are looser, so they are translated for the call
# and the answer is translated back, then checked against the ORIGINAL schema in maxplan.
_STRICT_DROP = {"minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems", "default",
                "examples", "format", "deprecated", "readOnly", "writeOnly", "$schema", "$comment"}
OPEN = "__json_object__"


def strict(schema):
    """A strict-mode copy of *schema*. An open object becomes a string holding JSON (marked with
    OPEN in its description); an optional property becomes required and nullable."""
    if not isinstance(schema, dict):
        return schema
    out = {k: v for k, v in schema.items() if k not in _STRICT_DROP}
    if "oneOf" in out:
        out["anyOf"] = out.pop("oneOf")
    for key in ("anyOf", "allOf"):
        if key in out:
            out[key] = [strict(x) for x in out[key]]
    kind = out.get("type")
    if kind == "object" or (isinstance(kind, list) and "object" in kind):
        props = out.get("properties")
        if not props:
            return {"type": "string", "description": f"{OPEN} A JSON object, written as a string."}
        required = set(out.get("required") or [])
        new = {}
        for name, sub in props.items():
            sub = strict(sub)
            if name not in required:
                sub = {"anyOf": [sub, {"type": "null"}]}
            new[name] = sub
        out["properties"] = new
        out["required"] = list(props)
        out["additionalProperties"] = False
    if kind == "array" or (isinstance(kind, list) and "array" in kind):
        if isinstance(out.get("items"), dict):
            out["items"] = strict(out["items"])
    return out


def unstrict(value, schema):
    """Undo strict(): decode JSON-string objects and drop the nulls of optional properties."""
    if not isinstance(schema, dict):
        return value
    kind = schema.get("type")
    if (kind == "object" or (isinstance(kind, list) and "object" in kind)) and isinstance(value, (dict, str)):
        props = schema.get("properties")
        if not props:
            if isinstance(value, str):
                try:
                    decoded = json.loads(value) if value.strip() else {}
                except json.JSONDecodeError:
                    return value
                return decoded
            return value
        if isinstance(value, dict):
            required = set(schema.get("required") or [])
            return {k: unstrict(v, props.get(k, {})) for k, v in value.items()
                    if not (v is None and k not in required)}
    if (kind == "array" or (isinstance(kind, list) and "array" in kind)) and isinstance(value, list):
        return [unstrict(v, schema.get("items") or {}) for v in value]
    for key in ("anyOf", "oneOf"):
        if key in schema and value is not None:
            for option in schema[key]:
                if isinstance(option, dict) and option.get("type") == "object":
                    return unstrict(value, option)
    return value


def run(user: str, system: str, model: str, effort: str | None, schema: dict | None,
        timeout: int) -> dict:
    """One call. Returns {text, data, usage, model, seconds, failed, error, stray}; the caller
    (maxplan) decides what a failure means, because the usage-limit wording is shared there."""
    work = Path(tempfile.mkdtemp(prefix="desk-codex-"))
    system_file, last_file = work / "system.md", work / "last.txt"
    system_file.write_text(system or "Answer from the material given.", encoding="utf-8")
    schema_file = None
    if schema:
        schema_file = work / "schema.json"
        schema_file.write_text(json.dumps(strict(schema)), encoding="utf-8")
    scratch = work / "empty"
    scratch.mkdir()
    started = time.time()
    try:
        proc = subprocess.run(command(model, effort, scratch, system_file, schema_file, last_file),
                              input=user.encode("utf-8"), capture_output=True, env=child_env(),
                              timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace").strip()
        text = last_file.read_text(encoding="utf-8").strip() if last_file.exists() else ""
        code = proc.returncode
    except subprocess.TimeoutExpired:
        out, err, text, code = "", f"codex exec timed out after {timeout}s", "", -1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    ev = read_events(out)
    data = None
    if schema and text:
        try:
            data = json.loads(text[text.index("{"):]) if "{" in text else None
        except json.JSONDecodeError:
            data = None
        data = unstrict(data, schema) if isinstance(data, dict) else None
    failed = code != 0 or bool(ev["errors"]) or not text or (schema is not None and data is None)
    u = ev["usage"]
    return {"text": text, "data": data, "model": model, "seconds": round(time.time() - started, 1),
            "failed": failed, "error": " | ".join(ev["errors"]) or err[-400:], "stray": ev["stray"],
            "usage": {"input": u.get("input_tokens", 0), "cache_read": u.get("cached_input_tokens", 0),
                      "cache_write": u.get("cache_write_input_tokens", 0), "output": u.get("output_tokens", 0),
                      "reasoning": u.get("reasoning_output_tokens", 0)}}


def available() -> tuple[bool, str]:
    """Is there a codex CLI, and is the desk's own Codex home logged in?"""
    try:
        exe = codex_exe()
    except ChatGPTError as exc:
        return False, str(exc)
    if not (home() / "auth.json").exists():
        return False, (f"the desk's Codex home ({home()}) is not logged in; run "
                       f"`CODEX_HOME={home()} codex login` once")
    return True, exe[0]
