"""Входящие личные сообщения как события: агент ждёт, а не опрашивает.

Обработчик NewMessage(incoming=True) копит по чату только счётчики и id —
текст не хранится и не отдаётся. Служебный чат Telegram помечается service.
"""

import asyncio
import logging
import time
from typing import Any

from telethon import events as tg_events
from telethon import utils

from .. import app
from ..app import tool
from ..guard import redact

log = logging.getLogger("tg_mcp_safe")

# (account, chat_id) -> счётчики пачки
_pending: dict[tuple[str, int], dict[str, Any]] = {}
_activity: asyncio.Event | None = None
_registered: set[int] = set()


def _event() -> asyncio.Event:
    global _activity
    if _activity is None:
        _activity = asyncio.Event()
    return _activity


def _make_handler(label: str):
    async def on_incoming(event) -> None:
        try:
            if not event.is_private:
                return
            sender = await event.get_sender()
            if sender is None or getattr(sender, "bot", False) or getattr(sender, "is_self", False):
                return
            key = (label, event.chat_id)
            now, mid = time.time(), event.message.id
            rec = _pending.get(key)
            if rec is None:
                _pending[key] = {
                    "first_ts": now, "last_ts": now, "count": 1, "first_id": mid, "last_id": mid,
                    "name": utils.get_display_name(sender) or None,
                    "username": getattr(sender, "username", None),
                    "service": getattr(sender, "id", None) in redact.SERVICE_SENDER_IDS
                    or event.chat_id in redact.SERVICE_SENDER_IDS,
                }
            else:
                rec["last_ts"], rec["last_id"] = now, mid
                rec["count"] += 1
            _event().set()
        except Exception as exc:  # noqa: BLE001
            log.warning("events: обработчик входящих упал (%s)", type(exc).__name__)

    return on_incoming


def register() -> None:
    """Повесить обработчик входящих на все app.clients. Повторный вызов не дублирует."""
    for label, cl in app.clients.items():
        if id(cl) in _registered:
            continue
        cl.add_event_handler(_make_handler(label), tg_events.NewMessage(incoming=True))
        _registered.add(id(cl))


def _chat(key: tuple[str, int], rec: dict) -> dict:
    d = {"chat_id": key[1], "name": rec["name"], "username": rec["username"]}
    if len(app.clients) > 1:
        d["account"] = key[0]
    if rec["service"]:
        d["service"] = True
    return d


@tool(readonly=True, fan_out=False)
async def wait_for_new_message(timeout: float = 50.0) -> str:
    """Block until an incoming private message from a non-bot user arrives (or timeout, max 600 s).
    Returns chats with pending messages (ids and counts, no text); does not consume them."""
    if not _pending:
        ev = _event()
        ev.clear()
        try:
            await asyncio.wait_for(ev.wait(), timeout=max(0.0, min(float(timeout), 600.0)))
        except asyncio.TimeoutError:
            return {"event": False, "reason": "timeout"}
    chats = [{**_chat(k, r), "count": r["count"], "last_message_id": r["last_id"]} for k, r in _pending.items()]
    return {"event": True, "pending_chats": chats}


@tool(readonly=True, fan_out=False)
async def wait_for_settled_message(settle_ms: int = 6000, max_wait_ms: int = 50000) -> str:
    """Debounced wait: returns one private chat whose burst of incoming messages went quiet for settle_ms,
    and removes it from the pending set. Ids and counts only, no text; timeout after max_wait_ms (max 600000)."""
    settle = max(0, int(settle_ms)) / 1000.0
    deadline = time.time() + max(0, min(int(max_wait_ms), 600_000)) / 1000.0
    ev = _event()
    while True:
        now = time.time()
        ready, soonest = None, None
        for k, r in list(_pending.items()):
            quiet = now - r["last_ts"]
            if quiet >= settle:
                ready = k
                break
            rem = settle - quiet
            soonest = rem if soonest is None else min(soonest, rem)
        if ready is not None:
            r = _pending.pop(ready)
            return {"event": True, **_chat(ready, r), "message_count": r["count"], "first_message_id": r["first_id"],
                    "last_message_id": r["last_id"], "burst_seconds": round(r["last_ts"] - r["first_ts"], 2)}
        left = deadline - now
        if left <= 0:
            return {"event": False, "reason": "timeout"}
        if soonest is not None:
            await asyncio.sleep(min(soonest, left))
            continue
        ev.clear()
        try:
            await asyncio.wait_for(ev.wait(), timeout=left)
        except asyncio.TimeoutError:
            return {"event": False, "reason": "timeout"}
