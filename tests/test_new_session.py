import asyncio
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("new_session", Path(__file__).parent.parent / "deploy" / "new_session.py")
ns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ns)


class FakeSession:
    def save(self):
        return "NEWSESSION"


class FakeMe:
    id = 42


class FakeClient:
    def __init__(self):
        self.session = FakeSession()
        self.asked = []

    async def start(self, phone, code_callback, password):
        self.asked = [phone(), code_callback(), password()]

    async def get_me(self):
        return FakeMe()

    async def disconnect(self):
        pass


def test_parse_and_build():
    lines, api_id, api_hash = ns.parse_env("TELEGRAM_API_ID=5\nTELEGRAM_API_HASH='abc'\nTELEGRAM_SESSION_STRING=OLD\nX=1\n")
    assert (api_id, api_hash) == (5, "abc")
    out = ns.build_env(lines, "NEW")
    assert "OLD" not in out and out.endswith("TELEGRAM_SESSION_STRING=NEW\n") and "X=1" in out


def test_login_asks_and_returns():
    fc = FakeClient()
    answers = iter(["+70000000000", "12345"])
    s, me = asyncio.run(ns.login(1, "h", lambda p: next(answers), lambda p: "pw", client_factory=lambda: fc))
    assert (s, me) == ("NEWSESSION", 42) and fc.asked == ["+70000000000", "12345", "pw"]
