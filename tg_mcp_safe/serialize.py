"""Компактные dict-представления объектов Telegram для агента.

Служебные сообщения Telegram (777000/42777) отдаются без текста, кнопок и медиа.
Остальные строки маскируются здесь и ещё раз в app.finalize.
"""

from __future__ import annotations

from typing import Any

from telethon import utils
from telethon.tl import types

from .guard import messages as mguard
from .guard import redact


def entity_type(e: Any) -> str:
    if isinstance(e, types.User):
        return "bot" if e.bot else ("self" if e.is_self else "user")
    if isinstance(e, (types.Chat, types.ChatForbidden)):
        return "group"
    if isinstance(e, (types.Channel, types.ChannelForbidden)):
        return "supergroup" if getattr(e, "megagroup", False) else "channel"
    return type(e).__name__.lower()


def entity(e: Any) -> dict | None:
    if e is None:
        return None
    d: dict[str, Any] = {
        "id": utils.get_peer_id(e) if not isinstance(e, types.User) else e.id,
        "type": entity_type(e),
        "name": utils.get_display_name(e) or None,
    }
    un = getattr(e, "username", None)
    if un:
        d["username"] = un
    if isinstance(e, types.User):
        if e.phone:
            d["phone"] = e.phone
        if e.verified:
            d["verified"] = True
        if e.deleted:
            d["deleted"] = True
        if e.contact:
            d["contact"] = True
    if getattr(e, "participants_count", None):
        d["members"] = e.participants_count
    if getattr(e, "forum", False):
        d["forum"] = True
    if getattr(e, "creator", False):
        d["creator"] = True
    if e is not None and getattr(e, "id", None) in redact.SERVICE_SENDER_IDS:
        d["service"] = True
    return d


def media(msg: Any) -> dict | None:
    m = getattr(msg, "media", None)
    if m is None:
        return None
    if isinstance(m, types.MessageMediaWebPage):
        page = m.webpage
        if isinstance(page, types.WebPage):
            return {"type": "webpage", "url": page.url, "title": page.title, "description": (page.description or "")[:300] or None}
        return {"type": "webpage"}
    if isinstance(m, types.MessageMediaPhoto):
        return {"type": "photo"}
    if isinstance(m, types.MessageMediaDocument):
        doc = m.document
        d: dict[str, Any] = {"type": "document"}
        if isinstance(doc, types.Document):
            d["mime"] = doc.mime_type
            d["size"] = doc.size
            for a in doc.attributes:
                if isinstance(a, types.DocumentAttributeFilename):
                    d["file_name"] = a.file_name
                elif isinstance(a, types.DocumentAttributeVideo):
                    d["type"] = "video_note" if a.round_message else "video"
                    d["duration"] = a.duration
                elif isinstance(a, types.DocumentAttributeAudio):
                    d["type"] = "voice" if a.voice else "audio"
                    d["duration"] = a.duration
                    if a.title:
                        d["title"] = a.title
                elif isinstance(a, types.DocumentAttributeSticker):
                    d["type"] = "sticker"
                    d["emoji"] = a.alt
                elif isinstance(a, types.DocumentAttributeAnimated):
                    d["type"] = "gif"
        return d
    if isinstance(m, types.MessageMediaPoll):
        p = m.poll
        q = getattr(p.question, "text", p.question)
        return {"type": "poll", "question": q, "options": [getattr(a.text, "text", a.text) for a in p.answers], "closed": bool(p.closed)}
    if isinstance(m, types.MessageMediaContact):
        return {"type": "contact", "name": f"{m.first_name} {m.last_name}".strip(), "phone": m.phone_number, "user_id": m.user_id or None}
    if isinstance(m, (types.MessageMediaGeo, types.MessageMediaGeoLive, types.MessageMediaVenue)):
        g = m.geo
        return {"type": "geo", "lat": getattr(g, "lat", None), "long": getattr(g, "long", None), "title": getattr(m, "title", None)}
    return {"type": type(m).__name__.replace("MessageMedia", "").lower()}


def buttons(msg: Any) -> list[list[dict]] | None:
    markup = getattr(msg, "reply_markup", None)
    rows = getattr(markup, "rows", None)
    if not rows:
        return None
    out = []
    for row in rows:
        r = []
        for b in row.buttons:
            item: dict[str, Any] = {"text": getattr(b, "text", "")}
            if getattr(b, "url", None):
                item["url"] = b.url
            if isinstance(b, types.KeyboardButtonCallback):
                item["kind"] = "callback"
            r.append(item)
        out.append(r)
    return out


def message(msg: Any, *, with_sender: bool = True) -> dict | None:
    if msg is None:
        return None
    if isinstance(msg, types.MessageEmpty):
        return {"id": msg.id, "empty": True}
    d: dict[str, Any] = {"id": msg.id, "date": msg.date}
    if getattr(msg, "chat_id", None) is not None:
        d["chat_id"] = msg.chat_id
    sid = mguard.sender_id(msg)
    if sid is not None:
        d["sender_id"] = sid
    if with_sender:
        s = getattr(msg, "sender", None)
        if s is not None:
            d["sender"] = utils.get_display_name(s) or getattr(s, "username", None)
    if getattr(msg, "out", False):
        d["out"] = True
    if isinstance(msg, types.MessageService):
        d["service_action"] = type(msg.action).__name__.replace("MessageAction", "")
        return d
    if mguard.is_service_sender(msg):
        d["text"] = redact.SERVICE_MASK
        d["sensitive"] = True
        return d
    text = msg.message or ""
    if text:
        d["text"] = redact.redact(text)
    r = getattr(msg, "reply_to", None)
    if r is not None and getattr(r, "reply_to_msg_id", None):
        d["reply_to"] = r.reply_to_msg_id
        qt = getattr(r, "quote_text", None)
        if qt:
            d["reply_quote"] = redact.redact(qt)
        if getattr(r, "reply_to_top_id", None):
            d["topic_id"] = r.reply_to_top_id
        elif getattr(r, "forum_topic", False):
            d["topic_id"] = r.reply_to_msg_id
    fwd = getattr(msg, "fwd_from", None)
    if fwd is not None:
        d["forwarded"] = {"from_id": utils.get_peer_id(fwd.from_id) if fwd.from_id else None, "from_name": fwd.from_name, "date": fwd.date}
    md = media(msg)
    if md:
        d["media"] = md
    bt = buttons(msg)
    if bt:
        d["buttons"] = bt
    if getattr(msg, "edit_date", None):
        d["edited"] = msg.edit_date
    if getattr(msg, "views", None):
        d["views"] = msg.views
    if getattr(msg, "forwards", None):
        d["forwards"] = msg.forwards
    links = [e.url for e in (getattr(msg, "entities", None) or []) if isinstance(e, types.MessageEntityTextUrl)]
    if links:
        d["links"] = sorted(set(links))[:20]
    if getattr(msg, "grouped_id", None):
        d["album_id"] = msg.grouped_id
    if getattr(msg, "pinned", False):
        d["pinned"] = True
    rx = getattr(msg, "reactions", None)
    if rx is not None and getattr(rx, "results", None):
        d["reactions"] = {getattr(x.reaction, "emoticon", None) or "custom": x.count for x in rx.results}
    if any(redact.detect(t) for t in mguard.message_texts(msg) if t):
        d["sensitive"] = True
    return d


def dialog(dlg: Any) -> dict:
    e = dlg.entity
    d = entity(e) or {}
    d["id"] = dlg.id
    if dlg.unread_count:
        d["unread"] = dlg.unread_count
    if getattr(dlg, "archived", False) or getattr(dlg, "folder_id", None) == 1:
        d["archived"] = True
    if dlg.pinned:
        d["pinned"] = True
    m = dlg.message
    if m is not None:
        if dlg.id in redact.SERVICE_SENDER_IDS or mguard.is_service_sender(m):
            d["last"] = {"id": m.id, "date": m.date, "text": redact.SERVICE_MASK}
        else:
            d["last"] = {"id": m.id, "date": m.date, "text": (redact.redact(m.message or "") or "")[:160] or None}
    return d


async def resolve(cl: Any, chat_id: Any) -> Any:
    """Найти сущность по id, @username, ссылке t.me или «me»."""
    if isinstance(chat_id, str):
        s = chat_id.strip()
        if s.lstrip("-").isdigit():
            chat_id = int(s)
        else:
            if s.startswith(("https://t.me/", "http://t.me/", "t.me/")):
                s = s.split("t.me/", 1)[1].split("/")[0].split("?")[0]
            if s == "self":
                s = "me"
            return await cl.get_entity(s)
    try:
        return await cl.get_entity(chat_id)
    except ValueError:
        async for dlg in cl.iter_dialogs():
            if dlg.id == chat_id or getattr(dlg.entity, "id", None) == chat_id:
                return dlg.entity
        raise ValueError(f"чат {chat_id} не найден")
