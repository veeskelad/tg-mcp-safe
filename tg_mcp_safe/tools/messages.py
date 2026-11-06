"""Сообщения: чтение, отправка, правка, пересылка, кнопки, реакции, черновики.

Текст наружу — только через serialize.message или redact.redact; текст внутрь —
через check_outgoing; действия над чужими сообщениями — через deny_if_sensitive.
"""

import random
from datetime import datetime, timedelta, timezone

from telethon import utils
from telethon.tl import functions, types

from ..app import tool, client
from ..guard import redact
from ..guard.errors import GuardError
from ..guard.messages import check_outgoing, deny_if_sensitive, is_service_sender
from ..serialize import media, resolve
from ..serialize import message as ser_message

MAX_LIMIT = 500
_PARSE_MODES = {None, "html", "md", "markdown"}


# ---------------------------------------------------------------- помощники

def _peer_id(e) -> int | None:
    try:
        return utils.get_peer_id(e)
    except Exception:  # noqa: BLE001  фейки и неполные сущности
        return getattr(e, "id", None)


def _is_service_chat(e) -> bool:
    return _peer_id(e) in redact.SERVICE_SENDER_IDS


def _deny_service_chat(e, action: str) -> None:
    if _is_service_chat(e):
        raise GuardError("sensitive_message", f"{action}: служебный чат Telegram (коды входа) — запрещено")


def _limit(n: int, lo: int = 1, hi: int = MAX_LIMIT) -> int:
    return max(lo, min(int(n), hi))


def _parse_mode(pm: str | None) -> str | None:
    pm = pm.lower() if isinstance(pm, str) else pm
    if pm not in _PARSE_MODES:
        raise ValueError("parse_mode: html, md, markdown или пусто")
    return pm


def _int_id(v, name: str) -> int | None:
    if v is None or isinstance(v, int):
        return v
    s = str(v).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    raise ValueError(f"{name} должен быть целым числом")


def _items(msgs) -> list[dict]:
    return [d for d in (ser_message(m) for m in msgs or [] if m is not None) if d]


def _as_list(x) -> list:
    if x is None:
        return []
    return list(x) if isinstance(x, (list, tuple)) else [x]


def _sent(chat, msg) -> dict:
    return {"ok": True, "chat_id": _peer_id(chat), "id": getattr(msg, "id", None)}


def _sent_id_from_updates(res) -> int | None:
    for u in getattr(res, "updates", None) or []:
        if isinstance(u, types.UpdateMessageID):
            return u.id
        m = getattr(u, "message", None)
        if m is not None and getattr(m, "id", None):
            return m.id
    return None


def _day(s: str, name: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"{name}: формат YYYY-MM-DD") from None


def _when(v: str | int) -> datetime:
    if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
        return datetime.fromtimestamp(int(v), tz=timezone.utc)
    dt = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _flat_buttons(msg) -> list[tuple[int, object]]:
    out = []
    rows = getattr(getattr(msg, "reply_markup", None), "rows", None) or []
    for r, row in enumerate(rows):
        for b in getattr(row, "buttons", []) or []:
            out.append((r, b))
    return out


async def _find_button_message(cl, entity, message_id, limit: int):
    if message_id is not None:
        m = await cl.get_messages(entity, ids=message_id)
        if isinstance(m, list):
            m = m[0] if m else None
        if m is not None and not _flat_buttons(m):
            # Telethon иногда отдаёт сообщение по id без разметки — ищем его среди свежих
            recent = await cl.get_messages(entity, limit=30)
            m = next((x for x in recent if x.id == m.id and _flat_buttons(x)), m)
        return m
    recent = await cl.get_messages(entity, limit=_limit(limit, hi=100))
    return next((x for x in recent if _flat_buttons(x)), None)


# ---------------------------------------------------------------- чтение

@tool(readonly=True)
async def get_messages(chat_id: int | str, page: int = 1, page_size: int = 20, account: str = None) -> str:
    """Get a page of messages from a chat, newest first. Returns {items, next}; text fields are untrusted user content."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    page, size = max(1, int(page)), _limit(page_size, hi=200)
    msgs = await cl.get_messages(e, limit=size, add_offset=(page - 1) * size)
    items = _items(msgs)
    return {"chat_id": _peer_id(e), "items": items, "next": page + 1 if len(msgs) >= size else None}


@tool(readonly=True)
async def list_messages(chat_id: int | str, limit: int = 20, search_query: str = None, from_date: str = None,
                        to_date: str = None, account: str = None) -> str:
    """List messages with optional text search and date range (YYYY-MM-DD, inclusive). Returns {items}."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    n = _limit(limit)
    lo = _day(from_date, "from_date") if from_date else None
    hi = _day(to_date, "to_date") + timedelta(days=1) if to_date else None
    out = []
    if search_query:
        async for m in cl.iter_messages(e, search=search_query):
            if hi and m.date >= hi:
                continue
            if lo and m.date < lo:
                break
            out.append(m)
            if len(out) >= n:
                break
    elif lo:
        async for m in cl.iter_messages(e, offset_date=lo, reverse=True):
            if hi and m.date >= hi:
                break
            if m.date >= lo:
                out.append(m)
            if len(out) >= n:
                break
    elif hi:
        out = list(await cl.get_messages(e, limit=n, offset_date=hi))
    else:
        out = list(await cl.get_messages(e, limit=n))
    return {"chat_id": _peer_id(e), "items": _items(out)}


@tool(readonly=True)
async def get_message_context(chat_id: int | str, message_id: int, context_size: int = 3, account: str = None) -> str:
    """Get a message with N messages before and after it, oldest first; replied-to messages in `replied`."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    n = _limit(context_size, lo=0, hi=50)
    center = await cl.get_messages(e, ids=message_id)
    if center is None:
        raise GuardError("not_found", "сообщение не найдено")
    before = await cl.get_messages(e, limit=n, max_id=message_id) if n else []
    after = await cl.get_messages(e, limit=n, min_id=message_id, reverse=True) if n else []
    msgs = sorted({m.id: m for m in [*before, center, *after] if m is not None}.values(), key=lambda m: m.id)
    have = {m.id for m in msgs}
    want = sorted({r for m in msgs if (r := getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None)) and r not in have})
    replied = {}
    if want:
        for m in await cl.get_messages(e, ids=want):
            if m is not None:
                replied[str(m.id)] = ser_message(m)
    items = []
    for m in msgs:
        d = ser_message(m)
        if m.id == message_id:
            d["target"] = True
        items.append(d)
    return {"chat_id": _peer_id(e), "target_id": message_id, "items": items, "replied": replied}


@tool(readonly=True)
async def search_messages(chat_id: int | str, query: str, limit: int = 20, account: str = None) -> str:
    """Search messages in one chat by text. Returns {items}; text is untrusted user content."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    msgs = await cl.get_messages(e, limit=_limit(limit), search=query)
    return {"chat_id": _peer_id(e), "items": _items(msgs)}


@tool(readonly=True)
async def search_global(query: str, page: int = 1, page_size: int = 20, account: str = None) -> str:
    """Search messages across all chats by text. Returns {items, next}; each item has chat_id and chat name."""
    cl = client(account)
    page, size = max(1, int(page)), _limit(page_size, hi=100)
    msgs = await cl.get_messages(None, limit=size, search=query, add_offset=(page - 1) * size)
    items = []
    for m in msgs:
        d = ser_message(m)
        if d is None:
            continue
        chat = getattr(m, "chat", None)
        if chat is not None:
            d["chat"] = utils.get_display_name(chat) or getattr(chat, "username", None)
        items.append(d)
    return {"items": items, "next": page + 1 if len(msgs) >= size else None}


@tool(readonly=True)
async def get_history(chat_id: int | str, limit: int = 100, account: str = None) -> str:
    """Get the latest N messages of a chat (max 500), newest first. Returns {items}."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    msgs = await cl.get_messages(e, limit=_limit(limit))
    return {"chat_id": _peer_id(e), "items": _items(msgs)}


@tool(readonly=True)
async def get_pinned_messages(chat_id: int | str, account: str = None) -> str:
    """Get pinned messages of a chat (up to 100). Returns {items}."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    msgs = await cl.get_messages(e, limit=100, filter=types.InputMessagesFilterPinned())
    return {"chat_id": _peer_id(e), "items": _items(msgs)}


@tool(readonly=True)
async def get_scheduled_messages(chat_id: int | str, account: str = None) -> str:
    """List scheduled (pending) messages in a chat: id, scheduled date, text, media type."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    res = await cl(functions.messages.GetScheduledHistoryRequest(peer=e, hash=0))
    items = []
    for m in getattr(res, "messages", None) or []:
        if isinstance(m, types.MessageEmpty):
            continue
        d = {"id": m.id, "date": getattr(m, "date", None)}
        if _is_service_chat(e) or is_service_sender(m):
            d["text"] = redact.SERVICE_MASK
        elif getattr(m, "message", None):
            d["text"] = redact.redact(m.message)
        md = media(m)
        if md:
            d["media"] = md
        items.append(d)
    return {"chat_id": _peer_id(e), "items": items}


@tool(readonly=True)
async def list_inline_buttons(chat_id: int | str, message_id: int | str | None = None, limit: int = 20,
                              account: str = None) -> str:
    """List inline buttons of a message (or of the latest message with buttons): index, row, text, kind, url."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    _deny_service_chat(e, "кнопки")
    m = await _find_button_message(cl, e, _int_id(message_id, "message_id"), limit)
    if m is None:
        raise GuardError("not_found", "нет сообщения с inline-кнопками")
    if is_service_sender(m):
        deny_if_sensitive(m, "кнопки")
    items = []
    for i, (r, b) in enumerate(_flat_buttons(m)):
        d = {"index": i, "row": r, "text": redact.redact(getattr(b, "text", "") or "")}
        if isinstance(b, types.KeyboardButtonCallback):
            d["kind"] = "callback"
            if getattr(b, "requires_password", False):
                d["requires_password"] = True
        elif getattr(b, "url", None):
            d["kind"] = "url"
            d["url"] = b.url
        else:
            d["kind"] = type(b).__name__.replace("KeyboardButton", "").lower() or "button"
        items.append(d)
    return {"chat_id": _peer_id(e), "message_id": m.id, "date": m.date, "items": items}


@tool(readonly=True)
async def get_message_reactions(chat_id: int | str, message_id: int, limit: int = 50, account: str = None) -> str:
    """List who reacted to a message: user_id, name, emoji, date. Returns {items, next}."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    res = await cl(functions.messages.GetMessageReactionsListRequest(peer=e, id=message_id, limit=_limit(limit, hi=100)))
    names = {u.id: utils.get_display_name(u) for u in getattr(res, "users", None) or []}
    items = []
    for r in getattr(res, "reactions", None) or []:
        rx = r.reaction
        if isinstance(rx, types.ReactionEmoji):
            emoji = rx.emoticon
        elif isinstance(rx, types.ReactionCustomEmoji):
            emoji = f"custom:{rx.document_id}"
        else:
            emoji = type(rx).__name__
        uid = _peer_id(r.peer_id)
        items.append({"user_id": uid, "name": names.get(getattr(r.peer_id, "user_id", None)), "emoji": emoji, "date": r.date})
    return {"chat_id": _peer_id(e), "message_id": message_id, "items": items, "next": getattr(res, "next_offset", None)}


@tool(readonly=True)
async def get_drafts(account: str = None) -> str:
    """List all saved drafts: chat_id, chat name, text, date, reply_to."""
    cl = client(account)
    res = await cl(functions.messages.GetAllDraftsRequest())
    names = {}
    for x in [*(getattr(res, "users", None) or []), *(getattr(res, "chats", None) or [])]:
        names[_peer_id(x)] = utils.get_display_name(x) or None
    items = []
    for u in getattr(res, "updates", None) or []:
        dr = getattr(u, "draft", None)
        if not isinstance(dr, types.DraftMessage):
            continue
        pid = _peer_id(u.peer)
        d = {"chat_id": pid, "chat": names.get(pid), "date": dr.date}
        if pid in redact.SERVICE_SENDER_IDS:
            d["text"] = redact.SERVICE_MASK
        else:
            d["text"] = redact.redact(dr.message or "")
        rt = getattr(getattr(dr, "reply_to", None), "reply_to_msg_id", None)
        if rt:
            d["reply_to"] = rt
        if dr.no_webpage:
            d["no_webpage"] = True
        items.append(d)
    return {"items": items}


# ---------------------------------------------------------------- отправка и правка

@tool()
async def send_message(chat_id: int | str, message: str, parse_mode: str | None = None, account: str = None) -> str:
    """Send a text message. parse_mode: 'html', 'md'/'markdown' or empty for plain text. Returns {ok, chat_id, id}."""
    check_outgoing(message)
    pm = _parse_mode(parse_mode)
    cl = client(account)
    e = await resolve(cl, chat_id)
    m = await cl.send_message(e, message, parse_mode=pm)
    return _sent(e, m)


@tool()
async def reply_to_message(chat_id: int | str, message_id: int, text: str, parse_mode: str | None = None,
                           account: str = None) -> str:
    """Reply to a message. parse_mode: 'html', 'md'/'markdown' or empty. Returns {ok, chat_id, id}."""
    check_outgoing(text)
    pm = _parse_mode(parse_mode)
    cl = client(account)
    e = await resolve(cl, chat_id)
    m = await cl.send_message(e, text, reply_to=message_id, parse_mode=pm)
    return _sent(e, m)


@tool()
async def send_scheduled_message(chat_id: int | str, message: str, schedule_date: str | int, account: str = None) -> str:
    """Schedule a message. schedule_date: ISO-8601 (naive = UTC) or Unix timestamp, must be in the future."""
    check_outgoing(message)
    dt = _when(schedule_date)
    if dt <= datetime.now(timezone.utc):
        raise ValueError("schedule_date должна быть в будущем")
    cl = client(account)
    e = await resolve(cl, chat_id)
    m = await cl.send_message(e, message, schedule=dt)
    return {**_sent(e, m), "scheduled": dt}


@tool(destructive=True)
async def delete_scheduled_message(chat_id: int | str, message_ids: list[int], account: str = None) -> str:
    """Delete scheduled (pending) messages by id. Returns {ok, deleted}."""
    if not message_ids:
        raise ValueError("message_ids пуст")
    cl = client(account)
    e = await resolve(cl, chat_id)
    await cl(functions.messages.DeleteScheduledMessagesRequest(peer=e, id=list(message_ids)))
    return {"ok": True, "chat_id": _peer_id(e), "deleted": list(message_ids)}


@tool(idempotent=True)
async def edit_message(chat_id: int | str, message_id: int, new_text: str, account: str = None) -> str:
    """Edit the text of your own message. Returns {ok, chat_id, id}."""
    check_outgoing(new_text)
    cl = client(account)
    e = await resolve(cl, chat_id)
    m = await cl.edit_message(e, message_id, new_text)
    return {"ok": True, "chat_id": _peer_id(e), "id": getattr(m, "id", message_id)}


@tool()
async def create_poll(chat_id: int, question: str, options: list, multiple_choice: bool = False,
                      quiz_mode: bool = False, public_votes: bool = True, close_date: str = None,
                      account: str = None) -> str:
    """Send a native poll (2-10 options). In quiz mode the first option is the correct one. close_date: ISO-8601."""
    opts = [str(o) for o in options or []]
    if not 2 <= len(opts) <= 10:
        raise ValueError("нужно от 2 до 10 вариантов")
    check_outgoing(question, opts, field="опрос")
    closes = _when(close_date) if close_date else None
    cl = client(account)
    e = await resolve(cl, chat_id)
    poll = types.Poll(
        id=random.getrandbits(63),
        question=types.TextWithEntities(text=question, entities=[]),
        answers=[types.PollAnswer(text=types.TextWithEntities(text=o, entities=[]), option=bytes([i])) for i, o in enumerate(opts)],
        multiple_choice=multiple_choice,
        quiz=quiz_mode,
        public_voters=public_votes,
        close_date=closes,
    )
    res = await cl(functions.messages.SendMediaRequest(
        peer=e,
        media=types.InputMediaPoll(poll=poll, correct_answers=[bytes([0])] if quiz_mode else None),
        message="",
        random_id=random.getrandbits(63),
    ))
    return {"ok": True, "chat_id": _peer_id(e), "id": _sent_id_from_updates(res)}


@tool(idempotent=True)
async def save_draft(chat_id: int | str, message: str, reply_to_msg_id: int | None = None, no_webpage: bool = False,
                     account: str = None) -> str:
    """Save a draft in a chat; it appears in the input field of Telegram apps. Returns {ok, chat_id}."""
    check_outgoing(message, field="черновик")
    cl = client(account)
    e = await resolve(cl, chat_id)
    reply = types.InputReplyToMessage(reply_to_msg_id=reply_to_msg_id) if reply_to_msg_id else None
    await cl(functions.messages.SaveDraftRequest(peer=e, message=message, no_webpage=no_webpage, reply_to=reply))
    return {"ok": True, "chat_id": _peer_id(e)}


@tool(idempotent=True)
async def clear_draft(chat_id: int | str, account: str = None) -> str:
    """Clear the draft in a chat. Returns {ok, chat_id}."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    await cl(functions.messages.SaveDraftRequest(peer=e, message=""))
    return {"ok": True, "chat_id": _peer_id(e)}


# ---------------------------------------------------------------- действия над сообщениями

async def _forward(cl, src, dst, ids: list[int], action: str):
    _deny_service_chat(src, action)
    msgs = await cl.get_messages(src, ids=ids)
    msgs = _as_list(msgs)
    if not any(m is not None for m in msgs):
        raise GuardError("not_found", "сообщения не найдены")
    deny_if_sensitive(msgs, action)
    res = await cl.forward_messages(dst, ids, src)
    return [getattr(m, "id", None) for m in _as_list(res) if m is not None]


@tool()
async def forward_message(from_chat_id: int | str, message_id: int | list[int], to_chat_id: int | str,
                          account: str = None, expand_album: bool = True) -> str:
    """Forward a message (a single id expands to its whole album unless expand_album=false) or a list of ids."""
    cl = client(account)
    src = await resolve(cl, from_chat_id)
    _deny_service_chat(src, "пересылка")
    dst = await resolve(cl, to_chat_id)
    ids = list(message_id) if isinstance(message_id, list) else [int(message_id)]
    album = False
    if expand_album and not isinstance(message_id, list):
        anchor = await cl.get_messages(src, ids=ids[0])
        gid = getattr(anchor, "grouped_id", None) if anchor is not None else None
        if gid is not None:
            near = await cl.get_messages(src, ids=list(range(ids[0] - 9, ids[0] + 10)))
            sib = sorted({m.id for m in near if m is not None and getattr(m, "grouped_id", None) == gid})
            if len(sib) > 1:
                ids, album = sib, True
    new_ids = await _forward(cl, src, dst, ids, "пересылка")
    return {"ok": True, "from_chat_id": _peer_id(src), "to_chat_id": _peer_id(dst), "source_ids": ids,
            "ids": new_ids, "album": album}


@tool()
async def forward_messages(from_chat_id: int | str, message_ids: list[int], to_chat_id: int | str,
                           account: str = None) -> str:
    """Forward a batch of messages in one call (keeps albums grouped). Returns {ok, ids}."""
    if not message_ids:
        raise ValueError("message_ids пуст")
    cl = client(account)
    src = await resolve(cl, from_chat_id)
    _deny_service_chat(src, "пересылка")
    dst = await resolve(cl, to_chat_id)
    ids = list(message_ids)
    new_ids = await _forward(cl, src, dst, ids, "пересылка")
    return {"ok": True, "from_chat_id": _peer_id(src), "to_chat_id": _peer_id(dst), "source_ids": ids, "ids": new_ids}


@tool()
async def press_inline_button(chat_id: int | str, message_id: int | str | None = None, button_text: str | None = None,
                              button_index: int | None = None, account: str = None) -> str:
    """Press an inline callback button by text (case-insensitive) or index. Returns the bot's answer."""
    if button_text is None and button_index is None:
        raise ValueError("укажите button_text или button_index")
    cl = client(account)
    e = await resolve(cl, chat_id)
    _deny_service_chat(e, "нажатие кнопки")
    m = await _find_button_message(cl, e, _int_id(message_id, "message_id"), 20)
    if m is None:
        raise GuardError("not_found", "нет сообщения с inline-кнопками")
    deny_if_sensitive(m, "нажатие кнопки")
    btns = [b for _, b in _flat_buttons(m)]
    target = None
    if button_text:
        want = button_text.strip().lower()
        target = next((b for b in btns if (getattr(b, "text", "") or "").strip().lower() == want), None)
    if target is None and button_index is not None:
        idx = int(button_index)
        if not 0 <= idx < len(btns):
            raise ValueError(f"button_index вне диапазона 0..{len(btns) - 1}")
        target = btns[idx]
    if target is None:
        raise GuardError("not_found", "кнопка не найдена, см. list_inline_buttons")
    if getattr(target, "requires_password", False):
        raise GuardError("password_button", "кнопка требует пароль 2FA — запрещено")
    data = getattr(target, "data", None)
    if not data:
        url = getattr(target, "url", None)
        return {"ok": False, "message_id": m.id, "url": url, "reason": "кнопка открывает ссылку" if url else "у кнопки нет callback"}
    ans = await cl(functions.messages.GetBotCallbackAnswerRequest(peer=e, msg_id=m.id, data=data))
    out = {"ok": True, "message_id": m.id}
    if getattr(ans, "message", None):
        out["response"] = redact.redact(ans.message)
    if getattr(ans, "alert", False):
        out["alert"] = True
    if getattr(ans, "url", None):
        out["url"] = ans.url
    return out


@tool(destructive=True, idempotent=True)
async def delete_message(chat_id: int | str, message_id: int, account: str = None) -> str:
    """Delete one message (for everyone where allowed). Returns {ok, deleted}."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    res = await cl.delete_messages(e, [message_id])
    return {"ok": True, "chat_id": _peer_id(e), "deleted": sum(getattr(r, "pts_count", 0) for r in res or [])}


@tool(destructive=True, idempotent=True)
async def delete_messages_bulk(chat_id: int | str, message_ids: list[int], revoke: bool = True, account: str = None) -> str:
    """Delete several messages in one call; revoke=false deletes only for you (ignored in channels)."""
    if not message_ids:
        raise ValueError("message_ids пуст")
    cl = client(account)
    e = await resolve(cl, chat_id)
    res = await cl.delete_messages(e, list(message_ids), revoke=revoke)
    return {"ok": True, "chat_id": _peer_id(e), "requested": len(message_ids),
            "deleted": sum(getattr(r, "pts_count", 0) for r in res or [])}


@tool(destructive=True)
async def delete_chat_history(chat_id: int | str, max_id: int = 0, revoke: bool = False, account: str = None) -> str:
    """Clear chat history up to max_id (0 = all); revoke=true deletes for both sides where allowed."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    if isinstance(e, types.Channel):
        await cl(functions.channels.DeleteHistoryRequest(channel=e, max_id=max_id, for_everyone=revoke))
        return {"ok": True, "chat_id": _peer_id(e)}
    total = 0
    for _ in range(100):
        res = await cl(functions.messages.DeleteHistoryRequest(peer=e, max_id=max_id, revoke=revoke))
        total += getattr(res, "pts_count", 0)
        if not getattr(res, "offset", 0):
            break
    return {"ok": True, "chat_id": _peer_id(e), "deleted": total}


@tool(idempotent=True)
async def pin_message(chat_id: int | str, message_id: int, account: str = None) -> str:
    """Pin a message in a chat (silently). Returns {ok}."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    await cl.pin_message(e, message_id)
    return {"ok": True, "chat_id": _peer_id(e), "id": message_id}


@tool(idempotent=True)
async def unpin_message(chat_id: int | str, message_id: int, account: str = None) -> str:
    """Unpin a message in a chat. Returns {ok}."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    await cl.unpin_message(e, message_id)
    return {"ok": True, "chat_id": _peer_id(e), "id": message_id}


@tool(idempotent=True)
async def unpin_all_messages(chat_id: int | str, account: str = None) -> str:
    """Unpin all pinned messages in a chat. Returns {ok}."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    await cl(functions.messages.UnpinAllMessagesRequest(peer=e))
    return {"ok": True, "chat_id": _peer_id(e)}


@tool(idempotent=True)
async def mark_as_read(chat_id: int | str, account: str = None) -> str:
    """Mark all messages in a chat as read. Returns {ok}."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    await cl.send_read_acknowledge(e)
    return {"ok": True, "chat_id": _peer_id(e)}


@tool(idempotent=True)
async def send_reaction(chat_id: int | str, message_id: int, emoji: str, big: bool = False, account: str = None) -> str:
    """Set your emoji reaction on a message (e.g. 👍 ❤️ 🔥). Returns {ok}."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    await cl(functions.messages.SendReactionRequest(peer=e, msg_id=message_id, big=big,
                                                    reaction=[types.ReactionEmoji(emoticon=emoji)]))
    return {"ok": True, "chat_id": _peer_id(e), "id": message_id, "emoji": emoji}


@tool(idempotent=True)
async def remove_reaction(chat_id: int | str, message_id: int, account: str = None) -> str:
    """Remove your reaction from a message. Returns {ok}."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    await cl(functions.messages.SendReactionRequest(peer=e, msg_id=message_id, reaction=[]))
    return {"ok": True, "chat_id": _peer_id(e), "id": message_id}
