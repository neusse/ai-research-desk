"""Codex round 3: a crashed runner's leftover calls are stopped (G1), the output-token budget is a
research setting (G2), and a data source picked by which credentials exist is part of the inputs (G3).
Harmless sleeper processes and synthetic settings only."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import desk.provenance as prov
from desk import DeskError, engines
from desk.procs import owned

# A stand-in team runner: starts a "model call" that outlives it, then exits with the given code.
RUNNER = r"""
import json, subprocess, sys, time
from pathlib import Path
out, code = Path(sys.argv[1]), int(sys.argv[2])
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
(out.parent / "child.pid").write_text(str(child.pid))
if code == 0:
    out.write_text(json.dumps({"engine": "x", "rating": "Hold"}))
sys.exit(code)
"""


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.split(")")[-1].split()[0] != "Z"
    except FileNotFoundError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _gone(pid: int, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.2)
    return not _alive(pid)


@pytest.mark.parametrize("code", [3, 0])
def test_a_runner_that_exits_takes_its_leftover_calls_with_it(tmp_path, code):
    script = tmp_path / "runner.py"
    script.write_text(RUNNER, encoding="utf-8")
    out = tmp_path / "out.json"
    run = lambda: engines._run(Path(sys.executable), tmp_path, [script, out, code], out, tmp_path / "log.txt",
                               60, "x", lambda *_: None)
    if code:
        with pytest.raises(DeskError):
            run()
    else:
        assert run()["rating"] == "Hold"
    child = int((tmp_path / "child.pid").read_text())
    try:
        assert _gone(child), "the runner's leftover call is still running"
        assert owned(tmp_path) == []
    finally:
        if _alive(child):
            subprocess.run(["taskkill", "/PID", str(child), "/F"] if os.name == "nt" else ["kill", "-9", str(child)],
                           capture_output=True)


@pytest.fixture
def known_source(monkeypatch, tmp_path):
    monkeypatch.setattr(prov, "source_digest", lambda folder: "abc123")
    monkeypatch.setattr(prov, "EDGE_ENV", tmp_path / "no-edge.env")


def test_the_output_token_budget_is_a_research_setting_and_credentials_are_not(known_source):
    a = prov.expected("quant", "claude", "2026-10-06", env={"TRADINGAGENTS_MAX_TOKENS": "1000"})
    b = prov.expected("quant", "claude", "2026-10-06", env={"TRADINGAGENTS_MAX_TOKENS": "8000"})
    assert prov.differs(a, b) == "its research settings differs"
    secret = {"TRADINGAGENTS_ACCESS_TOKEN": "tok-x", "TRADINGAGENTS_API_KEY": "key-x",
              "HEDGE_FUND_SECRET": "sec-x", "EDGE_DESK_PASSWORD": "pw-x"}
    recorded = json.dumps([prov.expected(e, "claude", "2026-10-06", "test-2", env=secret) for e in ("quant", "vets", "edge")])
    assert not any(v in recorded for v in secret.values())


def test_the_engine_s_own_budget_setting_is_the_one_recorded():
    config = prov.TA_DIR / "tradingagents" / "default_config.py"
    if not config.exists():
        pytest.skip("TradingAgents is not checked out here")
    assert '"TRADINGAGENTS_MAX_TOKENS"' in config.read_text(encoding="utf-8")
    assert not prov._SKIP.search("TRADINGAGENTS_MAX_TOKENS")


@pytest.mark.parametrize("before,after", [({}, {"FINANCIAL_DATASETS_API_KEY": "fd-x"}),
                                          ({"FINANCIAL_DATASETS_API_KEY": "fd-x"}, {})])
def test_a_data_provider_switched_by_a_credential_reruns_the_veterans(known_source, before, after):
    a = prov.expected("vets", "claude", "2026-10-06", "test-2", env=before)
    b = prov.expected("vets", "claude", "2026-10-06", "test-2", env=after)
    assert {a["data"]["provider"], b["data"]["provider"]} == {"free", "financialdatasets"}
    assert prov.differs(a, b) == "its data sources differs"
    assert "fd-x" not in json.dumps([a, b])


def test_an_explicit_provider_stays_stable_whatever_keys_exist(known_source):
    a = prov.expected("vets", "claude", "2026-10-06", "test-2", env={"HEDGE_FUND_DATA_PROVIDER": "free"})
    b = prov.expected("vets", "claude", "2026-10-06", "test-2",
                      env={"HEDGE_FUND_DATA_PROVIDER": "free", "FINANCIAL_DATASETS_API_KEY": "fd-x"})
    assert prov.differs(a, b) is None


def test_alpaca_keys_in_a_shared_file_count_and_their_values_are_never_recorded(known_source, tmp_path):
    keys = tmp_path / "alpaca.env"
    keys.write_text("APCA_API_KEY_ID=id-x\nAPCA_API_SECRET_KEY=secret-x\n", encoding="utf-8")
    without = prov.expected("edge", "claude", "2026-10-06", env={})
    with_keys = prov.expected("edge", "claude", "2026-10-06", env={"ALPACA_ENV_FILE": str(keys)})
    assert (without["data"]["alpaca"], with_keys["data"]["alpaca"]) == (False, True)
    assert prov.differs(without, with_keys) == "its data sources differs"
    assert "secret-x" not in json.dumps(with_keys) and "id-x" not in json.dumps(with_keys)


def test_lowercase_paper_alpaca_keys_select_alpaca_without_recording_secrets(known_source):
    env = {"alpaca_paper_api_key": "paper-id", "alpaca_paper_secret": "paper-secret"}
    recorded = prov.expected("edge", "chatgpt", "2026-10-06", env=env)
    assert recorded["data"]["alpaca"] is True
    assert "paper-id" not in json.dumps(recorded) and "paper-secret" not in json.dumps(recorded)
