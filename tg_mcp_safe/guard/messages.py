"""Правила для сообщений: что считать чувствительным и что с таким нельзя делать."""

from __future__ import annotations

from typing import Any, Iterable

from . import redact
from .errors import GuardError


def _peer_user_id(peer: Any) -> int | None:
    return getattr(peer, "user_id", None)


def sender_id(msg: Any) -> int | None:
    sid = getattr(msg, "sender_id", None)
    if sid is None:
        sid = _peer_user_id(getattr(msg, "from_id", None))
    return sid


def is_service_sender(msg: Any) -> bool:
    if sender_id(msg) in redact.SERVICE_SENDER_IDS:
        return True
    chat = getattr(msg, "chat_id", None)
    if chat in redact.SERVICE_SENDER_IDS:
        return True
    return _peer_user_id(getattr(msg, "peer_id", None)) in redact.SERVICE_SENDER_IDS


def message_texts(msg: Any) -> Iterable[str]:
    """Весь текст сообщения, который мог бы увидеть агент."""
    for attr in ("message", "raw_text"):
        v = getattr(msg, attr, None)
        if isinstance(v, str) and v:
            yield v
    media = getattr(msg, "media", None)
    page = getattr(media, "webpage", None) if media is not None else None
    for attr in ("url", "title", "description", "site_name"):
        v = getattr(page, attr, None) if page is not None else None
        if isinstance(v, str):
            yield v
    poll = getattr(media, "poll", None) if media is not None else None
    if poll is not None:
        q = getattr(poll, "question", None)
        yield getattr(q, "text", q) if q is not None else ""
        for a in getattr(poll, "answers", []) or []:
            t = getattr(a, "text", None)
            yield getattr(t, "text", t) or ""
    markup = getattr(msg, "reply_markup", None)
    for row in getattr(markup, "rows", []) or []:
        for b in getattr(row, "buttons", []) or []:
            for attr in ("text", "url"):
                v = getattr(b, attr, None)
                if isinstance(v, str):
                    yield v
            data = getattr(b, "data", None)
            if isinstance(data, bytes):
                yield data.decode("utf-8", "ignore")
    doc = getattr(media, "document", None) if media is not None else None
    for a in getattr(doc, "attributes", []) or []:
        fn = getattr(a, "file_name", None)
        if fn:
            yield fn


def is_sensitive(msg: Any) -> bool:
    if is_service_sender(msg):
        return True
    return any(redact.detect(t) for t in message_texts(msg) if t)


def deny_if_sensitive(msgs: Any, action: str) -> None:
    if msgs is None:
        raise GuardError("not_found", "сообщение не найдено")
    if not isinstance(msgs, (list, tuple)):
        msgs = [msgs]
    for m in msgs:
        if m is None:
            continue
        if is_service_sender(m):
            raise GuardError("sensitive_message", f"{action}: служебное сообщение Telegram (коды входа) — запрещено")
        if is_sensitive(m):
            raise GuardError("sensitive_message", f"{action}: в сообщении найдены креды — запрещено")


def check_outgoing(*texts: Any, field: str = "текст") -> None:
    """Отказ, если агент пытается отправить креды."""
    kinds: set[str] = set()
    for t in texts:
        if t is None:
            continue
        if isinstance(t, (list, tuple)):
            for x in t:
                kinds.update(redact.detect(str(x)))
        else:
            kinds.update(redact.detect(str(t)))
    if kinds:
        raise GuardError("outgoing_secret", f"{field}: найдены креды ({', '.join(sorted(kinds))}) — отправка запрещена")
