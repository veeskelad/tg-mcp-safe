"""Страж сообщений в инструментах: без сети, на фейковом клиенте."""
import importlib
import json
import sys
import types as pytypes
from datetime import datetime, timezone
from pathlib import Path

import pytest
from telethon.tl import types

from tg_mcp_safe import app


def _load(name):
    # Пакет tools/__init__ импортирует все модули; пока их пишут параллельно, грузим свои напрямую.
    try:
        return importlib.import_module(f"tg_mcp_safe.tools.{name}")
    except ImportError:
        if "tg_mcp_safe.tools" not in sys.modules:
            pkg = pytypes.ModuleType("tg_mcp_safe.tools")
            pkg.__path__ = [str(Path(app.__file__).parent / "tools")]
            sys.modules["tg_mcp_safe.tools"] = pkg
        return importlib.import_module(f"tg_mcp_safe.tools.{name}")


messages = _load("messages")
events = _load("events")

TOKEN = "glpat-" + "Ab3dEf6hIj9kLm2nOp5q"
SERVICE = 777000
FRIEND = 1234567
NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)


def _msg(mid, sender, text, **kw):
    return pytypes.SimpleNamespace(id=mid, date=NOW, sender_id=sender, chat_id=sender, message=text,
                                   out=False, media=None, reply_markup=kw.pop("markup", None), **kw)


def _markup():
    return types.ReplyInlineMarkup(rows=[types.KeyboardButtonRow(buttons=[
        types.KeyboardButtonCallback(text="OK", data=b"ok")])])


class FakeClient:
    def __init__(self, history):
        self.history = history  # chat_id -> {msg_id: msg}
        self.calls = []

    async def get_entity(self, x):
        return types.User(id=int(x), first_name="Telegram" if int(x) == SERVICE else "Friend")

    async def get_messages(self, entity, ids=None, limit=None, **kw):
        box = self.history.get(entity.id, {})
        if ids is None:
            return sorted(box.values(), key=lambda m: -m.id)[: limit or 1]
        if isinstance(ids, list):
            return [box.get(i) for i in ids]
        return box.get(ids)

    async def forward_messages(self, *a, **kw):
        self.calls.append(("forward", a))
        return []

    async def send_message(self, *a, **kw):
        self.calls.append(("send", a))
        return pytypes.SimpleNamespace(id=99)

    async def __call__(self, req):
        self.calls.append(("rpc", type(req).__name__))
        return None


@pytest.fixture
def fake(monkeypatch):
    cl = FakeClient({
        SERVICE: {5: _msg(5, SERVICE, "Код для входа: 12345", markup=_markup())},
        FRIEND: {7: _msg(7, FRIEND, "привет"), 8: _msg(8, FRIEND, "token: " + TOKEN)},
    })
    monkeypatch.setattr(app, "clients", {"main": cl})
    return cl


def _out(s):
    return json.loads(s)


async def test_forward_from_service_denied(fake):
    r = _out(await messages.forward_message(from_chat_id=SERVICE, message_id=5, to_chat_id=FRIEND))
    assert r["error"]["code"] == "sensitive_message"
    r = _out(await messages.forward_messages(from_chat_id=SERVICE, message_ids=[5], to_chat_id=FRIEND))
    assert r["error"]["code"] == "sensitive_message"
    assert not any(c[0] == "forward" for c in fake.calls)


async def test_forward_message_with_token_denied(fake):
    r = _out(await messages.forward_message(from_chat_id=FRIEND, message_id=8, to_chat_id=FRIEND))
    assert r["error"]["code"] == "sensitive_message"
    assert not fake.calls


async def test_press_button_in_service_denied(fake):
    r = _out(await messages.press_inline_button(chat_id=SERVICE, message_id=5, button_index=0))
    assert r["error"]["code"] == "sensitive_message"
    r = _out(await messages.list_inline_buttons(chat_id=SERVICE))
    assert r["error"]["code"] == "sensitive_message"
    assert not fake.calls


async def test_send_message_with_secret_blocked(fake):
    r = _out(await messages.send_message(chat_id=FRIEND, message="вот ключ " + TOKEN))
    assert r["error"]["code"] == "outgoing_secret"
    assert TOKEN not in json.dumps(r, ensure_ascii=False)
    r = _out(await messages.create_poll(chat_id=FRIEND, question="q", options=["a", TOKEN]))
    assert r["error"]["code"] == "outgoing_secret"
    assert fake.calls == []


async def test_send_message_ok(fake):
    r = _out(await messages.send_message(chat_id=FRIEND, message="привет"))
    assert r == {"ok": True, "chat_id": FRIEND, "id": 99}


async def test_get_messages_masks(fake):
    svc = _out(await messages.get_messages(chat_id=SERVICE))
    assert svc["items"][0]["text"].startswith("‹скрыто: служебное")
    assert "12345" not in json.dumps(svc, ensure_ascii=False)
    fr = _out(await messages.get_messages(chat_id=FRIEND))
    texts = {m["id"]: m["text"] for m in fr["items"]}
    assert texts[7] == "привет"
    assert texts[8].startswith("token: ‹скрыто") and TOKEN not in json.dumps(fr, ensure_ascii=False)


async def test_events_no_text_and_service_flag(monkeypatch):
    events._pending.clear()
    monkeypatch.setattr(app, "clients", {"main": object()})
    handler = events._make_handler("main")
    sender = types.User(id=SERVICE, first_name="Telegram")
    ev = pytypes.SimpleNamespace(is_private=True, chat_id=SERVICE,
                                 message=pytypes.SimpleNamespace(id=5, message="Код: 12345"))

    async def get_sender():
        return sender
    ev.get_sender = get_sender
    await handler(ev)
    r = _out(await events.wait_for_new_message(timeout=0))
    assert r["event"] and r["pending_chats"][0]["service"] is True
    assert "12345" not in json.dumps(r, ensure_ascii=False)
    s = _out(await events.wait_for_settled_message(settle_ms=0, max_wait_ms=0))
    assert s["chat_id"] == SERVICE and s["service"] is True and s["message_count"] == 1
