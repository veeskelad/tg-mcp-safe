"""Стражи chats/groups на фейковом клиенте: без сети."""
import asyncio
import importlib
import json
import sys
import types as pytypes
from datetime import datetime, timezone
from pathlib import Path

import pytest
from telethon.tl import types

import tg_mcp_safe
from tg_mcp_safe import app
from tg_mcp_safe.guard import redact
from tg_mcp_safe.guard.files import FileGuard

try:
    import tg_mcp_safe.tools  # noqa: F401
except ImportError:  # соседние модули ещё не дописаны: грузим свои напрямую
    pkg = pytypes.ModuleType("tg_mcp_safe.tools")
    pkg.__path__ = [str(Path(tg_mcp_safe.__file__).parent / "tools")]
    sys.modules["tg_mcp_safe.tools"] = pkg
chats = importlib.import_module("tg_mcp_safe.tools.chats")
groups = importlib.import_module("tg_mcp_safe.tools.groups")

SECRET = "glpat-" + "Ab3dEf6hIj9kLm2nOp5q"
CODE = "Login code: " + "73" + "519"
NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, dialogs=None, entity=None, responses=None):
        self.calls = []
        self._dialogs = dialogs or []
        self._entity = entity
        self._responses = responses or {}

    async def get_dialogs(self, **kw):
        self.calls.append(("get_dialogs", kw))
        return self._dialogs

    async def get_entity(self, x):
        self.calls.append(("get_entity", x))
        return self._entity

    async def get_input_entity(self, x):
        self.calls.append(("get_input_entity", x))
        return x

    async def upload_file(self, *a, **kw):
        self.calls.append(("upload_file", a))
        return object()

    async def __call__(self, req):
        self.calls.append(("rpc", type(req).__name__))
        return self._responses.get(type(req).__name__)


class Dlg:
    def __init__(self, ent, msg):
        self.entity, self.id, self.message = ent, ent.id, msg
        self.unread_count, self.archived, self.pinned = 1, False, False
        self.unread_mentions_count = 0
        self.dialog = pytypes.SimpleNamespace(unread_mark=False, notify_settings=None)


def run(coro):
    return json.loads(asyncio.run(coro))


@pytest.fixture
def fake(monkeypatch):
    def install(cl):
        monkeypatch.setattr(app, "clients", {"main": cl})
        return cl
    return install


def test_list_chats_service_preview_masked(fake):
    svc = types.User(id=777000, first_name="Telegram")
    msg = types.Message(id=5, peer_id=types.PeerUser(777000), date=NOW, message=CODE, from_id=types.PeerUser(777000))
    cl = fake(FakeClient(dialogs=[Dlg(svc, msg)]))
    out = run(chats.list_chats())
    item = out["items"][0]
    assert item["id"] == 777000 and item["service"] is True
    assert item["last"]["text"] == redact.SERVICE_MASK
    assert "73519" not in json.dumps(out, ensure_ascii=False)
    assert cl.calls == [("get_dialogs", {"limit": 20, "archived": None})]


def test_get_chats_service_preview_masked(fake):
    svc = types.User(id=777000, first_name="Telegram")
    msg = types.Message(id=5, peer_id=types.PeerUser(777000), date=NOW, message=CODE)
    fake(FakeClient(dialogs=[Dlg(svc, msg)]))
    out = run(chats.get_chats())
    assert out["items"][0]["last"]["text"] == redact.SERVICE_MASK
    assert "73519" not in json.dumps(out, ensure_ascii=False)


def test_edit_chat_about_secret_blocked(fake):
    cl = fake(FakeClient())
    out = run(groups.edit_chat_about(-1001, "ключ " + SECRET))
    assert out["error"]["code"] == "outgoing_secret"
    assert SECRET not in json.dumps(out, ensure_ascii=False)
    assert cl.calls == []


@pytest.mark.parametrize("call", [
    lambda: groups.edit_chat_title(-1001, SECRET),
    lambda: groups.create_group(SECRET, [1]),
    lambda: groups.create_channel("ok", about=SECRET),
    lambda: chats.create_forum_topic(-1001, SECRET),
])
def test_outgoing_titles_blocked(fake, call):
    cl = fake(FakeClient())
    out = run(call())
    assert out["error"]["code"] == "outgoing_secret"
    assert cl.calls == []


def test_edit_chat_photo_outside_roots(fake, monkeypatch, tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "photo.jpg"
    outside.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(app, "files", FileGuard([str(root)], None))
    cl = fake(FakeClient())
    out = run(groups.edit_chat_photo(-1001, str(outside)))
    assert out["error"]["code"] == "outside_roots"
    assert cl.calls == []


def test_recent_actions_mask_message_text(fake):
    ch = types.Channel(id=1001, title="g", photo=types.ChatPhotoEmpty(), date=NOW, megagroup=True, access_hash=1)
    prev = types.Message(id=9, peer_id=types.PeerChannel(1001), date=NOW, message="token " + SECRET)
    new = types.Message(id=9, peer_id=types.PeerChannel(1001), date=NOW, message="убрал")
    ev = types.ChannelAdminLogEvent(id=1, date=NOW, user_id=42,
                                    action=types.ChannelAdminLogEventActionEditMessage(prev_message=prev, new_message=new))
    res = types.channels.AdminLogResults(events=[ev], chats=[], users=[])
    fake(FakeClient(entity=ch, responses={"GetAdminLogRequest": res}))
    out = run(groups.get_recent_actions("@g"))
    text = json.dumps(out, ensure_ascii=False)
    assert SECRET not in text
    item = out["items"][0]
    assert item["action"] == "EditMessage" and item["prev_message"]["sensitive"] is True
    assert item["new_message"]["text"] == "убрал"
