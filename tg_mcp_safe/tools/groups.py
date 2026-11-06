"""Группы и каналы: создание, участники, права, оформление, приглашения, журнал админов."""

from __future__ import annotations

from typing import Any

from telethon import errors as tg_errors
from telethon.tl import functions, types

from .. import app
from ..app import client, tool
from ..guard import redact
from ..guard.errors import GuardError
from ..guard.messages import check_outgoing
from ..serialize import entity, message, resolve

_ADMIN_KEYS = (
    "change_info", "post_messages", "edit_messages", "delete_messages", "ban_users", "invite_users",
    "pin_messages", "add_admins", "anonymous", "manage_call", "manage_topics", "other",
)
_PROMOTE_DEFAULT = {k: k not in ("add_admins", "anonymous") for k in _ADMIN_KEYS}


def _chats_of(res: Any) -> list:
    chats = getattr(res, "chats", None)
    if chats:
        return list(chats)
    upd = getattr(res, "updates", None)
    return list(getattr(upd, "chats", None) or [])


def _group(e: Any) -> None:
    if not isinstance(e, (types.Chat, types.Channel)):
        raise GuardError("bad_request", "это не группа и не канал")


def _channel(e: Any, what: str) -> None:
    if not isinstance(e, types.Channel):
        raise GuardError("bad_request", f"{what}: только для супергрупп и каналов")


async def _users(cl: Any, ids: list) -> list:
    out = []
    for uid in ids:
        try:
            out.append(await resolve(cl, uid))
        except ValueError:
            raise GuardError("not_found", f"пользователь {uid} не найден")
    if not out:
        raise GuardError("bad_request", "пустой список пользователей")
    return out


@tool()
async def create_group(title: str, user_ids: list[int | str], account: str = None) -> dict:
    """Create a basic group with the given users. Returns {ok, chat:{id,...}}."""
    check_outgoing(title, field="название группы")
    cl = client(account)
    users = await _users(cl, user_ids)
    res = await cl(functions.messages.CreateChatRequest(users=users, title=title))
    chats = _chats_of(res)
    out: dict[str, Any] = {"ok": True, "chat": entity(chats[0]) if chats else None}
    missing = getattr(res, "missing_invitees", None)
    if missing:
        out["not_added"] = [m.user_id for m in missing]
    return out


@tool()
async def create_channel(title: str, about: str = "", megagroup: bool = False, account: str = None) -> dict:
    """Create a channel, or a supergroup with megagroup=true. Returns {ok, chat:{id,...}}."""
    check_outgoing(title, field="название канала")
    check_outgoing(about, field="описание канала")
    cl = client(account)
    res = await cl(functions.channels.CreateChannelRequest(title=title, about=about, megagroup=megagroup))
    chats = _chats_of(res)
    return {"ok": True, "chat": entity(chats[0]) if chats else None}


@tool()
async def invite_to_group(group_id: int | str, user_ids: list[int | str], account: str = None) -> dict:
    """Invite users to a group, supergroup or channel. Returns {ok, invited, not_added}."""
    cl = client(account)
    e = await resolve(cl, group_id)
    _group(e)
    users = await _users(cl, user_ids)
    not_added: list = []
    if isinstance(e, types.Channel):
        res = await cl(functions.channels.InviteToChannelRequest(channel=e, users=users))
        not_added = [m.user_id for m in (getattr(res, "missing_invitees", None) or [])]
    else:
        for u in users:
            try:
                res = await cl(functions.messages.AddChatUserRequest(chat_id=e.id, user_id=u, fwd_limit=0))
                not_added += [m.user_id for m in (getattr(res, "missing_invitees", None) or [])]
            except tg_errors.UserAlreadyParticipantError:
                continue
    return {"ok": True, "chat_id": group_id, "invited": len(users) - len(not_added), "not_added": not_added or None}


@tool(destructive=True)
async def leave_chat(chat_id: int | str, account: str = None) -> dict:
    """Leave a group, supergroup or channel."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    if isinstance(e, types.Channel):
        await cl(functions.channels.LeaveChannelRequest(channel=e))
    elif isinstance(e, types.Chat):
        await cl(functions.messages.DeleteChatUserRequest(chat_id=e.id, user_id=types.InputUserSelf()))
    else:
        raise GuardError("bad_request", "выйти можно только из группы или канала")
    return {"ok": True, "left": entity(e)}


@tool(readonly=True)
async def get_participants(chat_id: int | str, page: int = 1, page_size: int = 200, account: str = None) -> dict:
    """List members of a group or channel page by page (page_size up to 1000). Returns {items, next: page or null}."""
    if page_size > 1000:
        raise GuardError("bad_request", "page_size не больше 1000")
    page, page_size = max(1, page), max(1, page_size)
    cl = client(account)
    e = await resolve(cl, chat_id)
    want = page * page_size
    got = [p async for p in cl.iter_participants(e, limit=want + 1)]
    chunk = got[(page - 1) * page_size: want]
    return {"items": [entity(p) for p in chunk], "next": page + 1 if len(got) > want else None}


@tool(readonly=True)
async def get_admins(chat_id: int | str, account: str = None) -> dict:
    """List admins of a group or channel."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    ps = await cl.get_participants(e, filter=types.ChannelParticipantsAdmins())
    return {"items": [entity(p) for p in ps], "next": None}


@tool(readonly=True)
async def get_banned_users(chat_id: int | str, account: str = None) -> dict:
    """List banned users of a supergroup or channel."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    _channel(e, "список забаненных")
    ps = await cl.get_participants(e, filter=types.ChannelParticipantsKicked(q=""))
    return {"items": [entity(p) for p in ps], "next": None}


@tool(idempotent=True)
async def edit_chat_title(chat_id: int | str, title: str, account: str = None) -> dict:
    """Rename a group, supergroup or channel."""
    check_outgoing(title, field="название чата")
    cl = client(account)
    e = await resolve(cl, chat_id)
    if isinstance(e, types.Channel):
        await cl(functions.channels.EditTitleRequest(channel=e, title=title))
    elif isinstance(e, types.Chat):
        await cl(functions.messages.EditChatTitleRequest(chat_id=e.id, title=title))
    else:
        raise GuardError("bad_request", "название меняется только у группы или канала")
    return {"ok": True, "chat_id": chat_id, "title": title}


@tool(idempotent=True)
async def edit_chat_about(chat_id: int | str, about: str, account: str = None) -> dict:
    """Set the description of a group or channel (Telegram limit 255 chars)."""
    check_outgoing(about, field="описание чата")
    cl = client(account)
    e = await resolve(cl, chat_id)
    try:
        await cl(functions.messages.EditChatAboutRequest(peer=e, about=about))
    except tg_errors.ChatAboutNotModifiedError:
        return {"ok": True, "chat_id": chat_id, "unchanged": True}
    return {"ok": True, "chat_id": chat_id}


@tool()
async def edit_chat_photo(chat_id: int | str, file_path: str, account: str = None) -> dict:
    """Set the photo of a group or channel from an image file inside the server's allowed folders."""
    path = app.files.resolve_upload(file_path)
    cl = client(account)
    e = await resolve(cl, chat_id)
    _group(e)
    up = await cl.upload_file(str(path))
    photo = types.InputChatUploadedPhoto(file=up)
    if isinstance(e, types.Channel):
        await cl(functions.channels.EditPhotoRequest(channel=e, photo=photo))
    else:
        await cl(functions.messages.EditChatPhotoRequest(chat_id=e.id, photo=photo))
    return {"ok": True, "chat_id": chat_id, "file": path.name}


@tool(destructive=True)
async def delete_chat_photo(chat_id: int | str, account: str = None) -> dict:
    """Remove the photo of a group or channel."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    if isinstance(e, types.Channel):
        await cl(functions.channels.EditPhotoRequest(channel=e, photo=types.InputChatPhotoEmpty()))
    elif isinstance(e, types.Chat):
        await cl(functions.messages.EditChatPhotoRequest(chat_id=e.id, photo=types.InputChatPhotoEmpty()))
    else:
        raise GuardError("bad_request", "фото удаляется только у группы или канала")
    return {"ok": True, "chat_id": chat_id}


async def _edit_admin(cl: Any, chat_id: Any, user_id: Any, rights: dict, rank: str) -> tuple[Any, Any]:
    e = await resolve(cl, chat_id)
    _channel(e, "права админа")
    u = await resolve(cl, user_id)
    ar = types.ChatAdminRights(**{k: bool(rights.get(k, False)) for k in _ADMIN_KEYS})
    await cl(functions.channels.EditAdminRequest(channel=e, user_id=u, admin_rights=ar, rank=rank))
    return e, u


@tool(idempotent=True)
async def promote_admin(group_id: int | str, user_id: int | str, rights: dict = None, account: str = None) -> dict:
    """Make a user admin of a supergroup or channel. rights: {change_info, post_messages, edit_messages,
    delete_messages, ban_users, invite_users, pin_messages, add_admins, anonymous, manage_call, manage_topics, other}."""
    cl = client(account)
    r = dict(_PROMOTE_DEFAULT)
    if rights:
        unknown = set(rights) - set(_ADMIN_KEYS)
        if unknown:
            raise GuardError("bad_request", "неизвестные права: " + ", ".join(sorted(unknown)))
        r.update({k: bool(v) for k, v in rights.items()})
    _, u = await _edit_admin(cl, group_id, user_id, r, "Admin")
    return {"ok": True, "chat_id": group_id, "user_id": u.id, "rights": sorted(k for k, v in r.items() if v)}


@tool(destructive=True, idempotent=True)
async def demote_admin(group_id: int | str, user_id: int | str, account: str = None) -> dict:
    """Remove admin rights from a user in a supergroup or channel."""
    cl = client(account)
    _, u = await _edit_admin(cl, group_id, user_id, {}, "")
    return {"ok": True, "chat_id": group_id, "user_id": u.id}


@tool(idempotent=True)
async def edit_admin_rights(
    chat_id: int | str,
    user_id: int | str,
    rank: str = "",
    change_info: bool = False,
    post_messages: bool = False,
    edit_messages: bool = False,
    delete_messages: bool = False,
    ban_users: bool = False,
    invite_users: bool = False,
    pin_messages: bool = False,
    add_admins: bool = False,
    anonymous: bool = False,
    manage_call: bool = False,
    manage_topics: bool = False,
    other: bool = False,
    account: str = None,
) -> dict:
    """Set each admin right of a user explicitly (all false = demote). rank: custom title up to 16 chars."""
    check_outgoing(rank, field="звание админа")
    r = {
        "change_info": change_info, "post_messages": post_messages, "edit_messages": edit_messages,
        "delete_messages": delete_messages, "ban_users": ban_users, "invite_users": invite_users,
        "pin_messages": pin_messages, "add_admins": add_admins, "anonymous": anonymous,
        "manage_call": manage_call, "manage_topics": manage_topics, "other": other,
    }
    cl = client(account)
    _, u = await _edit_admin(cl, chat_id, user_id, r, rank)
    return {"ok": True, "chat_id": chat_id, "user_id": u.id, "rights": sorted(k for k, v in r.items() if v)}


@tool(destructive=True)
async def ban_user(chat_id: int | str, user_id: int | str, account: str = None) -> dict:
    """Ban a user from a supergroup or channel forever; in a basic group the user is removed."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    u = await resolve(cl, user_id)
    if isinstance(e, types.Channel):
        br = types.ChatBannedRights(
            until_date=None, view_messages=True, send_messages=True, send_media=True, send_stickers=True,
            send_gifs=True, send_games=True, send_inline=True, embed_links=True, send_polls=True,
            change_info=True, invite_users=True, pin_messages=True,
        )
        await cl(functions.channels.EditBannedRequest(channel=e, participant=u, banned_rights=br))
    elif isinstance(e, types.Chat):
        await cl(functions.messages.DeleteChatUserRequest(chat_id=e.id, user_id=u))
    else:
        raise GuardError("bad_request", "банить можно только в группе или канале")
    return {"ok": True, "chat_id": chat_id, "user_id": u.id}


@tool(idempotent=True)
async def unban_user(chat_id: int | str, user_id: int | str, account: str = None) -> dict:
    """Lift all restrictions from a user in a supergroup or channel."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    _channel(e, "разбан")
    u = await resolve(cl, user_id)
    await cl(functions.channels.EditBannedRequest(
        channel=e, participant=u, banned_rights=types.ChatBannedRights(until_date=None),
    ))
    return {"ok": True, "chat_id": chat_id, "user_id": u.id}


@tool(idempotent=True)
async def set_default_chat_permissions(
    chat_id: int | str,
    send_messages: bool = True,
    send_media: bool = True,
    send_stickers: bool = True,
    send_gifs: bool = True,
    send_games: bool = True,
    send_inline: bool = True,
    embed_links: bool = True,
    send_polls: bool = True,
    change_info: bool = False,
    invite_users: bool = True,
    pin_messages: bool = False,
    until_date: int = 0,
    account: str = None,
) -> dict:
    """Set default member permissions (true = allowed). until_date: unix time, 0 = permanent."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    _group(e)
    allowed = {
        "send_messages": send_messages, "send_media": send_media, "send_stickers": send_stickers,
        "send_gifs": send_gifs, "send_games": send_games, "send_inline": send_inline,
        "embed_links": embed_links, "send_polls": send_polls, "change_info": change_info,
        "invite_users": invite_users, "pin_messages": pin_messages,
    }
    br = types.ChatBannedRights(until_date=until_date or None, **{k: not v for k, v in allowed.items()})
    try:
        await cl(functions.messages.EditChatDefaultBannedRightsRequest(peer=e, banned_rights=br))
    except tg_errors.ChatNotModifiedError:
        return {"ok": True, "chat_id": chat_id, "unchanged": True}
    return {"ok": True, "chat_id": chat_id, "allowed": sorted(k for k, v in allowed.items() if v)}


@tool(idempotent=True)
async def toggle_slow_mode(chat_id: int | str, seconds: int = 0, account: str = None) -> dict:
    """Set slow mode in a supergroup: seconds in {0,10,30,60,300,900,3600}, 0 disables."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    if not isinstance(e, types.Channel) or not getattr(e, "megagroup", False):
        raise GuardError("bad_request", "медленный режим есть только в супергруппах")
    await cl(functions.channels.ToggleSlowModeRequest(channel=e, seconds=seconds))
    return {"ok": True, "chat_id": chat_id, "seconds": seconds}


@tool(readonly=True)
async def get_invite_link(chat_id: int | str, account: str = None) -> dict:
    """Get the chat's primary invite link; creates one only if the chat has none."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    _group(e)
    if isinstance(e, types.Channel):
        full = await cl(functions.channels.GetFullChannelRequest(channel=e))
    else:
        full = await cl(functions.messages.GetFullChatRequest(chat_id=e.id))
    inv = getattr(full.full_chat, "exported_invite", None)
    link = getattr(inv, "link", None)
    if not link:
        link = (await cl(functions.messages.ExportChatInviteRequest(peer=e))).link
    return {"chat_id": chat_id, "link": link}


@tool(readonly=True)
async def export_chat_invite(chat_id: int | str, account: str = None) -> dict:
    """Create a new invite link for a group or channel (needs invite rights)."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    _group(e)
    res = await cl(functions.messages.ExportChatInviteRequest(peer=e))
    return {"chat_id": chat_id, "link": getattr(res, "link", None)}


def _invite_hash(link: str) -> str | None:
    s = link.strip()
    for p in ("https://", "http://"):
        if s.startswith(p):
            s = s[len(p):]
    if s.startswith(("t.me/", "telegram.me/")):
        s = s.split("/", 1)[1]
        if s.startswith("joinchat/"):
            return s.split("/", 1)[1].split("?")[0]
        if s.startswith("+"):
            return s[1:].split("?")[0]
        return None
    return s.lstrip("+") or None


async def _join_by_hash(cl: Any, h: str) -> dict:
    try:
        info = await cl(functions.messages.CheckChatInviteRequest(hash=h))
        if isinstance(info, types.ChatInviteAlready):
            return {"ok": True, "already": True, "chat": entity(info.chat)}
    except (tg_errors.InviteHashExpiredError, tg_errors.InviteHashInvalidError):
        raise GuardError("invite_invalid", "приглашение недействительно или истекло")
    try:
        res = await cl(functions.messages.ImportChatInviteRequest(hash=h))
    except tg_errors.UserAlreadyParticipantError:
        return {"ok": True, "already": True}
    except tg_errors.InviteRequestSentError:
        return {"ok": True, "pending_approval": True}
    chats = _chats_of(res)
    return {"ok": True, "chat": entity(chats[0]) if chats else None}


@tool()
async def join_chat_by_link(link: str, account: str = None) -> dict:
    """Join a chat by invite link (t.me/+hash, t.me/joinchat/hash) or a public t.me/username link."""
    cl = client(account)
    h = _invite_hash(link)
    if h is None:
        e = await resolve(cl, link)
        try:
            await cl(functions.channels.JoinChannelRequest(channel=e))
        except tg_errors.UserAlreadyParticipantError:
            return {"ok": True, "already": True, "chat": entity(e)}
        return {"ok": True, "chat": entity(e)}
    return await _join_by_hash(cl, h)


@tool()
async def import_chat_invite(hash: str, account: str = None) -> dict:
    """Join a chat by invite hash (the part after t.me/+)."""
    cl = client(account)
    h = hash.strip().lstrip("+")
    if not h:
        raise GuardError("bad_request", "пустой hash")
    return await _join_by_hash(cl, h)


def _log_text(v: Any) -> str | None:
    return (redact.redact(v) or None) if isinstance(v, str) else None


def _log_event(ev: Any) -> dict:
    a = ev.action
    d: dict[str, Any] = {
        "id": ev.id, "date": ev.date, "user_id": ev.user_id,
        "action": type(a).__name__.replace("ChannelAdminLogEventAction", ""),
    }
    if isinstance(a, types.ChannelAdminLogEventActionEditMessage):
        d["prev_message"] = message(a.prev_message, with_sender=False)
        d["new_message"] = message(a.new_message, with_sender=False)
    elif isinstance(a, (types.ChannelAdminLogEventActionDeleteMessage, types.ChannelAdminLogEventActionUpdatePinned,
                        types.ChannelAdminLogEventActionSendMessage, types.ChannelAdminLogEventActionStopPoll)):
        d["message"] = message(a.message, with_sender=False)
    elif isinstance(a, (types.ChannelAdminLogEventActionChangeTitle, types.ChannelAdminLogEventActionChangeAbout,
                        types.ChannelAdminLogEventActionChangeUsername)):
        d["prev"] = _log_text(a.prev_value)
        d["new"] = _log_text(a.new_value)
    elif isinstance(a, types.ChannelAdminLogEventActionToggleSlowMode):
        d["prev"], d["new"] = a.prev_value, a.new_value
    else:
        for attr in ("participant", "prev_participant", "new_participant"):
            p = getattr(a, attr, None)
            uid = getattr(p, "user_id", None)
            if uid is not None:
                d["participant_id"] = uid
                break
        for attr in ("new_value", "invite"):
            v = getattr(a, attr, None)
            if isinstance(v, bool):
                d["value"] = v
            link = getattr(v, "link", None)
            if isinstance(link, str):
                d["invite_link"] = link
    return d


@tool(readonly=True)
async def get_recent_actions(chat_id: int | str, account: str = None) -> dict:
    """Last 20 admin-log events of a supergroup or channel; message texts are masked like everywhere."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    _channel(e, "журнал админов")
    res = await cl(functions.channels.GetAdminLogRequest(channel=e, q="", max_id=0, min_id=0, limit=20, admins=None))
    return {"items": [_log_event(ev) for ev in (res.events or [])], "next": None}
