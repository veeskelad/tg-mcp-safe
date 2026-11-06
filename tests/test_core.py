import json
import os

from tg_mcp_safe import app, config
from tg_mcp_safe.guard.errors import GuardError

FAKE_SESSION = "1BVtsOK8" + "Bu" * 40 + "Zq9"
FAKE_HASH = "0123456789abcdef" + "fedcba9876543210"


def test_config_scrubs_env(monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.delenv("TG_MCP_ENV_FILE", raising=False)
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", FAKE_HASH)
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", FAKE_SESSION)
    cfg = config.load()
    assert "TELEGRAM_SESSION_STRING" not in os.environ
    assert "TELEGRAM_API_HASH" not in os.environ
    assert FAKE_SESSION not in repr(cfg) and FAKE_SESSION not in repr(cfg.accounts)
    assert FAKE_SESSION in cfg.secrets


def test_config_from_credentials_dir(monkeypatch, tmp_path):
    (tmp_path / "telegram.env").write_text(f"TELEGRAM_API_ID=5\nTELEGRAM_API_HASH={FAKE_HASH}\nTELEGRAM_SESSION_STRING_WORK='{FAKE_SESSION}'\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    cfg = config.load()
    assert cfg.api_id == 5 and [a.label for a in cfg.accounts] == ["work"]


def test_canary_blocks_server_secret():
    app.configure([FAKE_SESSION], app.files)
    out = json.loads(app.finalize({"x": "abc " + FAKE_SESSION}))
    # строка либо замаскирована как случайный токен, либо весь ответ заблокирован
    assert FAKE_SESSION not in json.dumps(out)
    raw = app.finalize("plain")
    assert raw == "plain"


def test_canary_whole_block(monkeypatch):
    app.configure(["short-but-known-secret"], app.files)
    out = json.loads(app.finalize({"x": "short-but-known-secret"}))
    assert out["error"]["code"] == "blocked"


def test_map_exception_hides_details():
    e = app.map_exception("t", RuntimeError("секрет внутри " + FAKE_SESSION))
    assert FAKE_SESSION not in json.dumps(e, ensure_ascii=False)
    assert e["error"]["code"] == "internal"
    g = app.map_exception("t", GuardError("file_denied", "нельзя"))
    assert g["error"] == {"code": "file_denied", "reason": "нельзя"}
    v = app.map_exception("t", ValueError("token: " + "glpat-" + "Ab3dEf6hIj9kLm2nOp5q"))
    assert "glpat-" not in json.dumps(v, ensure_ascii=False)
