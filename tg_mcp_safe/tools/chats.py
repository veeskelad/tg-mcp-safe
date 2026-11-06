"""Чаты: списки, карточки, форумные темы, уведомления, архив, ссылки на сообщения."""

from __future__ import annotations

import secrets
import time
from datetime import datetime
from typing import Any

from telethon import errors as tg_errors
from telethon.tl import functions, types

from ..app import client, tool
from ..guard import redact
from ..guard.errors import GuardError
from ..guard.messages import check_outgoing, deny_if_sensitive
from ..serialize import dialog, entity, message, resolve

_MUTE_FOREVER = 2**31 - 1


def _filter_type(e: Any) -> str | None:
    if isinstance(e, types.User):
        return "user"
    if isinstance(e, (types.Chat, types.ChatForbidden)):
        return "group"
    if isinstance(e, (types.Channel, types.ChannelForbidden)):
        return "group" if getattr(e, "megagroup", False) else "channel"
    return None


def _is_muted(dlg: Any) -> bool:
    ns = getattr(getattr(dlg, "dialog", None), "notify_settings", None)
    mu = getattr(ns, "mute_until", None)
    if mu is None:
        return False
    if isinstance(mu, datetime):
        return mu.timestamp() > time.time()
    return mu > time.time()


def _supergroup(e: Any) -> None:
    if not isinstance(e, types.Channel) or not getattr(e, "megagroup", False):
        raise GuardError("bad_request", "чат не супергруппа")


async def _about(cl: Any, e: Any) -> str:
    if isinstance(e, types.Channel):
        full = await cl(functions.channels.GetFullChannelRequest(channel=e))
        return getattr(full.full_chat, "about", "") or ""
    if isinstance(e, types.Chat):
        full = await cl(functions.messages.GetFullChatRequest(chat_id=e.id))
        return getattr(full.full_chat, "about", "") or ""
    if isinstance(e, types.User):
        full = await cl(functions.users.GetFullUserRequest(id=e))
        return getattr(full.full_user, "about", "") or ""
    return ""


@tool(readonly=True)
async def get_chats(account: str = None, page: int = 1, page_size: int = 20) -> dict:
    """List dialogs page by page (1-indexed). Returns {items:[{id,type,name,unread,last}], next: page or null}."""
    cl = client(account)
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    want = page * page_size
    dialogs = await cl.get_dialogs(limit=want + 1)
    chunk = dialogs[(page - 1) * page_size: want]
    return {"items": [dialog(d) for d in chunk], "next": page + 1 if len(dialogs) > want else None}


@tool(readonly=True)
async def list_chats(
    chat_type: str = None,
    limit: int = 20,
    unread_only: bool = False,
    unmuted_only: bool = False,
    archived: bool = None,
    with_about: bool = False,
    account: str = None,
) -> dict:
    """List chats with filters: chat_type user|group|channel, unread_only, unmuted_only, archived.
    limit applies before filtering. with_about adds the description (one extra call per chat)."""
    cl = client(account)
    dialogs = await cl.get_dialogs(limit=limit, archived=archived)
    want_type = chat_type.lower() if chat_type else None
    items = []
    for dlg in dialogs:
        e = dlg.entity
        if want_type and _filter_type(e) != want_type:
            continue
        if archived is not None and bool(getattr(dlg, "archived", False)) != archived:
            continue
        muted = _is_muted(dlg)
        if unmuted_only and muted:
            continue
        unread_mark = bool(getattr(getattr(dlg, "dialog", None), "unread_mark", False))
        if unread_only and not dlg.unread_count and not unread_mark:
            continue
        d = dialog(dlg)
        if unread_mark:
            d["unread_mark"] = True
        if muted:
            d["muted"] = True
        mentions = getattr(dlg, "unread_mentions_count", 0) or 0
        if mentions:
            d["unread_mentions"] = mentions
        if with_about:
            try:
                d["about"] = (redact.redact(await _about(cl, e)) or "")[:200] or None
            except tg_errors.RPCError as exc:
                d["about_error"] = type(exc).__name__
        items.append(d)
    return {"items": items, "next": None}


@tool(readonly=True)
async def get_chat(chat_id: int | str, account: str = None) -> dict:
    """Get chat or user info by id or @username: entity fields, unread, archived, last message."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    d = entity(e) or {}
    if isinstance(e, types.User):
        d["bot"] = bool(e.bot)
    elif "members" not in d:
        try:
            d["members"] = (await cl.get_participants(e, limit=0)).total
        except tg_errors.RPCError:
            pass
    try:
        peer = await cl.get_input_entity(e)
        res = await cl(functions.messages.GetPeerDialogsRequest(peers=[types.InputDialogPeer(peer=peer)]))
        if getattr(res, "dialogs", None):
            dg = res.dialogs[0]
            d["unread"] = getattr(dg, "unread_count", 0)
            d["archived"] = getattr(dg, "folder_id", 0) == 1
        last = await cl.get_messages(e, limit=1)
        if last:
            d["last"] = message(last[0])
    except tg_errors.RPCError:
        pass
    return d


@tool(readonly=True)
async def get_full_chat(chat_id: int | str, account: str = None) -> dict:
    """Get full info of a group or channel: id, name, username, about, members, linked_chat_id."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    if isinstance(e, types.Channel):
        full = await cl(functions.channels.GetFullChannelRequest(channel=e))
    elif isinstance(e, types.Chat):
        full = await cl(functions.messages.GetFullChatRequest(chat_id=e.id))
    else:
        raise GuardError("bad_request", "это не группа и не канал")
    fc = full.full_chat
    d = entity(full.chats[0] if full.chats else e) or {}
    d["about"] = redact.redact(getattr(fc, "about", "") or "")[:1024] or None
    pc = getattr(fc, "participants_count", None)
    if pc is not None:
        d["members"] = pc
    linked = getattr(fc, "linked_chat_id", None)
    if linked:
        d["linked_chat_id"] = linked
    return d


@tool(readonly=True)
async def search_public_chats(query: str, limit: int = 20, account: str = None) -> dict:
    """Search public chats, channels, users and bots by username or title."""
    cl = client(account)
    res = await cl(functions.contacts.SearchRequest(q=query, limit=limit))
    return {"items": [entity(x) for x in list(res.chats) + list(res.users)], "next": None}


@tool(readonly=True)
async def resolve_username(username: str, account: str = None) -> dict:
    """Resolve @username to a user or chat. Returns id, type, name, username."""
    cl = client(account)
    name = username.strip().lstrip("@")
    res = await cl(functions.contacts.ResolveUsernameRequest(username=name))
    found = list(res.users) + list(res.chats)
    pid = getattr(res.peer, "user_id", None) or getattr(res.peer, "channel_id", None) or getattr(res.peer, "chat_id", None)
    hit = next((x for x in found if x.id == pid), found[0] if found else None)
    if hit is None:
        raise GuardError("not_found", "имя не найдено")
    return entity(hit)


@tool()
async def subscribe_public_channel(channel: int | str, account: str = None) -> dict:
    """Join a public channel or supergroup by @username or id."""
    cl = client(account)
    e = await resolve(cl, channel)
    try:
        await cl(functions.channels.JoinChannelRequest(channel=e))
    except tg_errors.UserAlreadyParticipantError:
        return {"ok": True, "already": True, "chat": entity(e)}
    except tg_errors.ChannelPrivateError:
        raise GuardError("private", "канал закрытый: нужна ссылка-приглашение")
    return {"ok": True, "chat": entity(e)}


@tool(readonly=True)
async def list_topics(
    chat_id: int,
    limit: int = 200,
    offset_topic: int = 0,
    search_query: str = None,
    account: str = None,
) -> dict:
    """List forum topics of a supergroup. Topic id works as reply_to/message_id for sending into the topic.
    Returns {items:[{id,title,total_messages,unread,closed,hidden,last_activity}], next: offset_topic or null}."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    _supergroup(e)
    if not getattr(e, "forum", False):
        raise GuardError("bad_request", "в супергруппе не включены темы")
    res = await cl(functions.messages.GetForumTopicsRequest(
        peer=e, offset_date=None, offset_id=0, offset_topic=offset_topic, limit=limit, q=search_query or None,
    ))
    msgs = {m.id: m for m in (getattr(res, "messages", None) or [])}
    items = []
    for t in getattr(res, "topics", None) or []:
        if isinstance(t, types.ForumTopicDeleted):
            continue
        it: dict[str, Any] = {"id": t.id, "title": redact.redact(getattr(t, "title", "") or "")[:256]}
        if getattr(t, "unread_count", 0):
            it["unread"] = t.unread_count
        if getattr(t, "closed", False):
            it["closed"] = True
        if getattr(t, "hidden", False):
            it["hidden"] = True
        top = msgs.get(getattr(t, "top_message", None))
        if top is not None and getattr(top, "date", None):
            it["last_activity"] = top.date
        items.append(it)
    nxt = items[-1]["id"] if items and len(items) >= limit else None
    return {"items": items, "next": nxt}


@tool(idempotent=True)
async def enable_forum_topics(chat_id: int | str, tabs: bool = True, account: str = None) -> dict:
    """Enable forum topics in a supergroup (needs admin right to change info). tabs: show topics as tabs."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    _supergroup(e)
    if getattr(e, "forum", False):
        return {"ok": True, "already": True, "chat_id": chat_id}
    await cl(functions.channels.ToggleForumRequest(channel=e, enabled=True, tabs=tabs))
    return {"ok": True, "chat": entity(e)}


def _created_topic_id(res: Any) -> int | None:
    for u in getattr(res, "updates", None) or []:
        if isinstance(u, types.UpdateMessageID):
            return u.id
        m = getattr(u, "message", None)
        if isinstance(getattr(m, "id", None), int):
            return m.id
    return None


@tool()
async def create_forum_topic(
    chat_id: int | str,
    title: str,
    icon_color: int = None,
    icon_emoji_id: int = None,
    account: str = None,
) -> dict:
    """Create a topic in a forum supergroup. Returns {ok, chat_id, topic_id, title}."""
    check_outgoing(title, field="название темы")
    cl = client(account)
    e = await resolve(cl, chat_id)
    _supergroup(e)
    if not getattr(e, "forum", False):
        raise GuardError("bad_request", "в супергруппе не включены темы: сначала enable_forum_topics")
    res = await cl(functions.messages.CreateForumTopicRequest(
        peer=e, title=title[:128], icon_color=icon_color, icon_emoji_id=icon_emoji_id,
        random_id=secrets.randbits(63),
    ))
    out: dict[str, Any] = {"ok": True, "chat_id": (entity(e) or {}).get("id"), "title": title[:128]}
    tid = _created_topic_id(res)
    if tid is not None:
        out["topic_id"] = tid
    return out


async def _notify(cl: Any, chat_id: Any, mute_until: int) -> None:
    e = await resolve(cl, chat_id)
    peer = types.InputNotifyPeer(peer=await cl.get_input_entity(e))
    await cl(functions.account.UpdateNotifySettingsRequest(
        peer=peer, settings=types.InputPeerNotifySettings(mute_until=mute_until),
    ))


@tool(idempotent=True)
async def mute_chat(chat_id: int | str, account: str = None) -> dict:
    """Mute notifications for a chat forever."""
    await _notify(client(account), chat_id, _MUTE_FOREVER)
    return {"ok": True, "chat_id": chat_id, "muted": True}


@tool(idempotent=True)
async def unmute_chat(chat_id: int | str, account: str = None) -> dict:
    """Unmute notifications for a chat."""
    await _notify(client(account), chat_id, 0)
    return {"ok": True, "chat_id": chat_id, "muted": False}


async def _folder(cl: Any, chat_id: Any, folder_id: int) -> None:
    e = await resolve(cl, chat_id)
    peer = await cl.get_input_entity(e)
    await cl(functions.folders.EditPeerFoldersRequest(folder_peers=[types.InputFolderPeer(peer=peer, folder_id=folder_id)]))


@tool(idempotent=True)
async def archive_chat(chat_id: int | str, account: str = None) -> dict:
    """Move a chat to the archive."""
    await _folder(client(account), chat_id, 1)
    return {"ok": True, "chat_id": chat_id, "archived": True}


@tool(idempotent=True)
async def unarchive_chat(chat_id: int | str, account: str = None) -> dict:
    """Move a chat out of the archive."""
    await _folder(client(account), chat_id, 0)
    return {"ok": True, "chat_id": chat_id, "archived": False}


@tool(readonly=True)
async def get_common_chats(user_id: int | str, limit: int = 100, max_id: int = 0, account: str = None) -> dict:
    """List chats shared with a user. Pagination: pass next as max_id."""
    cl = client(account)
    limit = max(1, min(limit, 100))
    u = await resolve(cl, user_id)
    res = await cl(functions.messages.GetCommonChatsRequest(user_id=u, max_id=max_id, limit=limit))
    chats = list(getattr(res, "chats", None) or [])
    return {"items": [entity(c) for c in chats], "next": chats[-1].id if len(chats) >= limit else None}


@tool(readonly=True)
async def get_message_read_by(chat_id: int | str, message_id: int, account: str = None) -> dict:
    """Who read a message (small groups, messages younger than ~7 days). Returns {items:[{user_id,read_at}]}."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    try:
        res = await cl(functions.messages.GetMessageReadParticipantsRequest(peer=e, msg_id=message_id))
    except tg_errors.MsgTooOldError:
        raise GuardError("unavailable", "сообщение слишком старое или отметки о прочтении выключены")
    items = []
    for it in res or []:
        if hasattr(it, "user_id"):
            items.append({"user_id": it.user_id, "read_at": getattr(it, "date", None)})
        else:
            items.append({"user_id": it, "read_at": None})
    return {"chat_id": chat_id, "message_id": message_id, "items": items, "next": None}


@tool(readonly=True)
async def get_message_link(chat_id: int | str, message_id: int, thread: bool = False, account: str = None) -> dict:
    """Export a t.me link to a message (channels and supergroups only). thread: open inside the discussion thread."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    if not isinstance(e, types.Channel):
        raise GuardError("bad_request", "ссылки на сообщения есть только у каналов и супергрупп")
    msgs = await cl.get_messages(e, ids=[message_id])
    if not msgs or msgs[0] is None:
        raise GuardError("not_found", "сообщение не найдено")
    deny_if_sensitive(msgs, "ссылка на сообщение")
    res = await cl(functions.channels.ExportMessageLinkRequest(channel=e, id=message_id, grouped=False, thread=thread))
    if not getattr(res, "link", None):
        raise GuardError("not_found", "Telegram не вернул ссылку")
    return {"chat_id": chat_id, "message_id": message_id, "link": res.link}
