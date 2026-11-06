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


class FakeQR:
    def __init__(self, script):
        self.script = list(script)
        self.url = "tg://login?token=fake"
        self.recreated = 0

    async def wait(self):
        step = self.script.pop(0)
        if step is not None:
            raise step

    async def recreate(self):
        self.recreated += 1


class FakeQRClient(FakeClient):
    def __init__(self, script):
        super().__init__()
        self.qr = FakeQR(script)
        self.password = None
        self.connected = False

    async def connect(self):
        self.connected = True

    async def qr_login(self):
        return self.qr

    async def sign_in(self, password=None):
        self.password = password


def test_qr_timeout_then_scan():
    fc = FakeQRClient([asyncio.TimeoutError(), None])
    shown = []
    s, me = asyncio.run(ns.login_qr(1, "h", shown.append, lambda p: "pw", client_factory=lambda: fc))
    assert (s, me) == ("NEWSESSION", 42)
    assert fc.qr.recreated == 1 and len(shown) == 2 and fc.password is None
    assert "█" in shown[0] or "▀" in shown[0] or "▄" in shown[0]


def test_qr_with_2fa():
    from telethon.errors import SessionPasswordNeededError
    fc = FakeQRClient([SessionPasswordNeededError(request=None)])
    s, _ = asyncio.run(ns.login_qr(1, "h", lambda t: None, lambda p: "pw2", client_factory=lambda: fc))
    assert s == "NEWSESSION" and fc.password == "pw2"


def test_qr_not_scanned():
    import pytest
    fc = FakeQRClient([asyncio.TimeoutError()] * 3)
    with pytest.raises(SystemExit):
        asyncio.run(ns.login_qr(1, "h", lambda t: None, lambda p: "x", client_factory=lambda: fc, attempts=3))
