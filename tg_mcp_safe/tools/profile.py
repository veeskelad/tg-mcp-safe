"""Свой профиль, приватность, сведения о пользователях и ботах."""

from __future__ import annotations

from typing import Any

from telethon import functions, types

from .. import app
from ..app import client, tool
from ..guard import redact
from ..guard.errors import GuardError
from ..guard.messages import check_outgoing
from ..serialize import entity, resolve

_PRIVACY_KEYS = {
    "status": "InputPrivacyKeyStatusTimestamp",
    "phone": "InputPrivacyKeyPhoneNumber",
    "profile_photo": "InputPrivacyKeyProfilePhoto",
    "forwards": "InputPrivacyKeyForwards",
    "calls": "InputPrivacyKeyPhoneCall",
    "chat_invite": "InputPrivacyKeyChatInvite",
    "about": "InputPrivacyKeyAbout",
    "birthday": "InputPrivacyKeyBirthday",
    "voice_messages": "InputPrivacyKeyVoiceMessages",
}


def _privacy_key(key: str) -> Any:
    cls = getattr(types, _PRIVACY_KEYS.get(key, ""), None)
    if cls is None:
        raise GuardError("bad_request", "неизвестный key, доступны: " + ", ".join(k for k, v in _PRIVACY_KEYS.items() if hasattr(types, v)))
    return cls()


def _rule(r: Any) -> dict:
    name = type(r).__name__.replace("PrivacyValue", "")
    d: dict[str, Any] = {"type": name}
    if getattr(r, "users", None):
        d["users"] = list(r.users)
    if getattr(r, "chats", None):
        d["chats"] = list(r.chats)
    return d


def _status(st: Any) -> dict:
    if st is None:
        return {"status": "unknown"}
    name = type(st).__name__.replace("UserStatus", "").lower()
    d: dict[str, Any] = {"status": name}
    if isinstance(st, types.UserStatusOffline):
        d["was_online"] = st.was_online
    elif isinstance(st, types.UserStatusOnline):
        d["expires"] = st.expires
    return d


@tool(readonly=True)
async def get_me(account: str = None) -> str:
    """Get your own account: id, name, username, phone. Returns JSON."""
    cl = client(account)
    me = await cl.get_me()
    d = entity(me) or {}
    if getattr(me, "premium", False):
        d["premium"] = True
    return d


@tool(idempotent=True)
async def update_profile(account: str = None, first_name: str = None, last_name: str = None, about: str = None) -> str:
    """Update your name and/or bio; omitted fields stay unchanged. Returns JSON: ok."""
    check_outgoing(first_name, last_name, about, field="профиль")
    if first_name is None and last_name is None and about is None:
        raise GuardError("bad_request", "нечего менять")
    cl = client(account)
    await cl(functions.account.UpdateProfileRequest(first_name=first_name, last_name=last_name, about=about))
    return {"ok": True, "updated": [k for k, v in (("first_name", first_name), ("last_name", last_name), ("about", about)) if v is not None]}


@tool(idempotent=True)
async def set_profile_photo(file_path: str, account: str = None) -> str:
    """Set a new profile photo from an image in the server's allowed folders. Returns JSON: ok, photo_id."""
    path = app.files.resolve_upload(file_path)
    cl = client(account)
    up = await cl.upload_file(str(path))
    res = await cl(functions.photos.UploadProfilePhotoRequest(file=up))
    return {"ok": True, "photo_id": getattr(getattr(res, "photo", None), "id", None)}


@tool(destructive=True)
async def delete_profile_photo(account: str = None) -> str:
    """Delete your current profile photo. Returns JSON: ok, deleted photo_id or null."""
    cl = client(account)
    photos = await cl(functions.photos.GetUserPhotosRequest(user_id=types.InputUserSelf(), offset=0, max_id=0, limit=1))
    if not photos.photos:
        return {"ok": True, "photo_id": None, "note": "фото профиля нет"}
    p = photos.photos[0]
    await cl(functions.photos.DeletePhotosRequest(id=[types.InputPhoto(id=p.id, access_hash=p.access_hash, file_reference=p.file_reference)]))
    return {"ok": True, "photo_id": p.id}


@tool(readonly=True)
async def get_privacy_settings(account: str = None) -> str:
    """Get privacy rules for "last seen". Returns JSON: key, rules [{type, users?, chats?}]."""
    cl = client(account)
    res = await cl(functions.account.GetPrivacyRequest(key=types.InputPrivacyKeyStatusTimestamp()))
    return {"key": "status", "rules": [_rule(r) for r in res.rules]}


async def _input_users(cl: Any, ids: list | None) -> list:
    out = []
    for uid in ids or []:
        e = await resolve(cl, uid)
        if not isinstance(e, types.User):
            raise GuardError("bad_request", f"{uid} — не пользователь")
        out.append(await cl.get_input_entity(e))
    return out


@tool(idempotent=True)
async def set_privacy_settings(key: str, allow_users: list[int | str] | None = None, disallow_users: list[int | str] | None = None,
                               account: str = None) -> str:
    """Set a privacy rule (key: status, phone, profile_photo, forwards, calls, chat_invite, about, birthday).
    No allow_users = allow everyone; with allow_users only they are allowed. Returns JSON: ok."""
    pkey = _privacy_key(key)
    cl = client(account)
    allow = await _input_users(cl, allow_users)
    deny = await _input_users(cl, disallow_users)
    rules: list[Any] = [types.InputPrivacyValueAllowUsers(users=allow)] if allow else [types.InputPrivacyValueAllowAll()]
    if deny:
        rules.append(types.InputPrivacyValueDisallowUsers(users=deny))
    res = await cl(functions.account.SetPrivacyRequest(key=pkey, rules=rules))
    return {"ok": True, "key": key, "rules": [_rule(r) for r in getattr(res, "rules", [])]}


def _birthday(b: Any) -> str | None:
    if b is None or not getattr(b, "day", None) or not getattr(b, "month", None):
        return None
    return f"{b.year:04d}-{b.month:02d}-{b.day:02d}" if getattr(b, "year", None) else f"--{b.month:02d}-{b.day:02d}"


@tool(readonly=True)
async def get_full_user(username: int | str, account: str = None) -> str:
    """Get a user's full profile: id, name, username, bio, personal channel, birthday, common chats. Returns JSON."""
    cl = client(account)
    e = await resolve(cl, username)
    if not isinstance(e, types.User):
        raise GuardError("bad_request", "это не пользователь")
    full = await cl(functions.users.GetFullUserRequest(id=e))
    fu = full.full_user
    user = next((u for u in full.users if u.id == e.id), e)
    d = entity(user) or {}
    if getattr(user, "premium", False):
        d["premium"] = True
    d["bio"] = redact.redact((fu.about or "")[:1024]) or None
    ch_id = getattr(fu, "personal_channel_id", None)
    if ch_id:
        ch = next((c for c in full.chats if c.id == ch_id), None)
        un = getattr(ch, "username", None)
        d["personal_channel"] = f"https://t.me/{un}" if un else ch_id
    d["birthday"] = _birthday(getattr(fu, "birthday", None))
    d["common_chats_count"] = getattr(fu, "common_chats_count", None)
    return d


@tool(readonly=True)
async def get_bot_info(bot_username: str, account: str = None) -> str:
    """Get a bot's profile: id, name, username, about, description, commands. Returns JSON."""
    cl = client(account)
    e = await resolve(cl, bot_username)
    if not isinstance(e, types.User) or not e.bot:
        raise GuardError("bad_request", "это не бот")
    full = await cl(functions.users.GetFullUserRequest(id=e))
    fu = full.full_user
    d = entity(e) or {}
    d["about"] = redact.redact((fu.about or "")[:1024]) or None
    bi = getattr(fu, "bot_info", None)
    if bi is not None:
        if getattr(bi, "description", None):
            d["description"] = redact.redact(bi.description[:1024])
        cmds = getattr(bi, "commands", None) or []
        if cmds:
            d["commands"] = [{"command": c.command, "description": redact.redact(c.description)} for c in cmds]
    return d


@tool(idempotent=True)
async def set_bot_commands(bot_username: str, commands: list, account: str = None) -> str:
    """Set the command list of the bot this account is logged in as (bot accounts only).
    commands: [{command, description}]. Returns JSON: ok, count."""
    parsed = []
    for c in commands:
        if not isinstance(c, dict) or not c.get("command") or "description" not in c:
            raise GuardError("bad_request", "каждая команда — {command, description}")
        parsed.append((str(c["command"]).lstrip("/"), str(c["description"])))
    check_outgoing([p for pair in parsed for p in pair], field="команды бота")
    cl = client(account)
    me = await cl.get_me()
    if not getattr(me, "bot", False):
        raise GuardError("not_a_bot", "команды бота задаёт только аккаунт-бот")
    if (me.username or "").lower() != bot_username.lstrip("@").lower():
        raise GuardError("bad_request", "бот аккаунта другой: команды можно задать только себе")
    await cl(functions.bots.SetBotCommandsRequest(
        scope=types.BotCommandScopeDefault(), lang_code="",
        commands=[types.BotCommand(command=a, description=b) for a, b in parsed]))
    return {"ok": True, "count": len(parsed)}


@tool(readonly=True)
async def get_user_photos(user_id: int | str, limit: int = 10, account: str = None) -> str:
    """List a user's profile photos. Returns JSON items: id, date."""
    cl = client(account)
    e = await resolve(cl, user_id)
    res = await cl(functions.photos.GetUserPhotosRequest(user_id=e, offset=0, max_id=0, limit=max(1, min(int(limit), 100))))
    return {"items": [{"id": p.id, "date": getattr(p, "date", None)} for p in res.photos], "next": None}


@tool(readonly=True)
async def get_user_status(user_id: int | str, account: str = None) -> str:
    """Get a user's online status. Returns JSON: user_id, status (online/offline/recently/...), was_online."""
    cl = client(account)
    e = await resolve(cl, user_id)
    if not isinstance(e, types.User):
        raise GuardError("bad_request", "это не пользователь")
    return {"user_id": e.id, **_status(e.status)}
