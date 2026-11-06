"""Контакты и блокировки."""

from __future__ import annotations

from typing import Any

from telethon import functions, types, utils

from ..app import client, tool
from ..guard.errors import GuardError
from ..guard.messages import check_outgoing
from ..serialize import entity, message, resolve


def _users(res: Any) -> list[dict]:
    return [entity(u) for u in getattr(res, "users", []) or [] if isinstance(u, types.User)]


async def _user(cl: Any, user_id: int | str) -> types.User:
    e = await resolve(cl, user_id)
    if not isinstance(e, types.User):
        raise GuardError("bad_request", f"{user_id} — не пользователь")
    return e


@tool(readonly=True)
async def list_contacts(account: str | None = None) -> str:
    """List all contacts. Returns JSON items: id, name, username, phone."""
    cl = client(account)
    res = await cl(functions.contacts.GetContactsRequest(hash=0))
    return {"items": _users(res), "next": None}


@tool(readonly=True)
async def search_contacts(query: str, account: str | None = None) -> str:
    """Search contacts and known users by name, username or phone. Returns JSON items: id, name, username."""
    cl = client(account)
    res = await cl(functions.contacts.SearchRequest(q=query, limit=50))
    return {"items": _users(res), "next": None}


@tool(readonly=True)
async def get_contact_ids(account: str | None = None) -> str:
    """Get ids of all contacts. Returns JSON items: [id]."""
    cl = client(account)
    res = await cl(functions.contacts.GetContactIDsRequest(hash=0))
    return {"items": list(res or []), "next": None}


@tool(readonly=True)
async def get_direct_chat_by_contact(contact_query: str, account: str | None = None) -> str:
    """Find private chats with contacts matching a name, username or phone. Returns JSON items: chat_id, name, username, unread."""
    cl = client(account)
    q = contact_query.strip().lstrip("@").lower()
    if not q:
        raise GuardError("bad_request", "пустой запрос")
    res = await cl(functions.contacts.GetContactsRequest(hash=0))
    found = {}
    for u in getattr(res, "users", []) or []:
        name = (utils.get_display_name(u) or "").lower()
        if q in name or (u.username and q in u.username.lower()) or (u.phone and q.lstrip("+") in u.phone):
            found[u.id] = u
    items = []
    if found:
        async for dlg in cl.iter_dialogs():
            if isinstance(dlg.entity, types.User) and dlg.entity.id in found:
                d = entity(dlg.entity) or {}
                d["chat_id"] = dlg.id
                if dlg.unread_count:
                    d["unread"] = dlg.unread_count
                items.append(d)
                if len(items) == len(found):
                    break
    without = [entity(u) for uid, u in found.items() if uid not in {i["id"] for i in items}]
    return {"items": items, "contacts_without_chat": without, "next": None}


@tool(readonly=True)
async def get_contact_chats(contact_id: int | str, account: str | None = None) -> str:
    """List chats shared with a user: the private chat (if any) and common groups/channels. Returns JSON items."""
    cl = client(account)
    u = await _user(cl, contact_id)
    items: list[dict] = []
    async for dlg in cl.iter_dialogs():
        if isinstance(dlg.entity, types.User) and dlg.entity.id == u.id:
            d = {"chat_id": dlg.id, "type": "private"}
            if dlg.unread_count:
                d["unread"] = dlg.unread_count
            items.append(d)
            break
    common = await cl(functions.messages.GetCommonChatsRequest(user_id=u, max_id=0, limit=100))
    for ch in common.chats:
        d = entity(ch) or {}
        d["chat_id"] = d.get("id")
        items.append(d)
    return {"contact": entity(u), "items": items, "next": None}


@tool(readonly=True)
async def get_last_interaction(contact_id: int | str, account: str | None = None) -> str:
    """Get the last 5 messages in the private chat with a user. Returns JSON: contact, items (messages)."""
    cl = client(account)
    u = await _user(cl, contact_id)
    msgs = await cl.get_messages(u, limit=5)
    return {"contact": entity(u), "items": [message(m, with_sender=False) for m in msgs], "next": None}


@tool(idempotent=True)
async def add_contact(account: str | None = None, phone: str | None = None, first_name: str = "", last_name: str = "",
                      username: str | None = None) -> str:
    """Add a contact by phone or by username (username needs no phone). Returns JSON: ok, user_id."""
    check_outgoing(first_name, last_name, field="имя контакта")
    phone = (phone or "").strip()
    username = (username or "").strip().lstrip("@")
    if not phone and not username:
        raise GuardError("bad_request", "нужен phone или username")
    cl = client(account)
    if username:
        u = await _user(cl, username)
        await cl(functions.contacts.AddContactRequest(id=u, first_name=first_name or u.first_name or "",
                                                      last_name=last_name, phone=phone))
        return {"ok": True, "user_id": u.id}
    res = await cl(functions.contacts.ImportContactsRequest(contacts=[
        types.InputPhoneContact(client_id=0, phone=phone, first_name=first_name, last_name=last_name)]))
    if not res.imported:
        raise GuardError("not_imported", "контакт не добавлен: номер не в Telegram или скрыт настройками")
    return {"ok": True, "user_id": res.imported[0].user_id}


@tool(destructive=True, idempotent=True)
async def delete_contact(user_id: int | str, account: str | None = None) -> str:
    """Delete a contact. Returns JSON: ok, user_id."""
    cl = client(account)
    u = await _user(cl, user_id)
    await cl(functions.contacts.DeleteContactsRequest(id=[u]))
    return {"ok": True, "user_id": u.id}


@tool(destructive=True, idempotent=True)
async def block_user(user_id: int | str, account: str | None = None) -> str:
    """Block a user. Returns JSON: ok, user_id."""
    cl = client(account)
    u = await _user(cl, user_id)
    await cl(functions.contacts.BlockRequest(id=u))
    return {"ok": True, "user_id": u.id}


@tool(idempotent=True)
async def unblock_user(user_id: int | str, account: str | None = None) -> str:
    """Unblock a user. Returns JSON: ok, user_id."""
    cl = client(account)
    u = await _user(cl, user_id)
    await cl(functions.contacts.UnblockRequest(id=u))
    return {"ok": True, "user_id": u.id}


@tool()
async def import_contacts(contacts: list, account: str | None = None) -> str:
    """Import contacts: list of {phone, first_name, last_name?}. Returns JSON: ok, imported user ids, not_imported count."""
    parsed = []
    for c in contacts:
        if not isinstance(c, dict) or not c.get("phone") or not c.get("first_name"):
            raise GuardError("bad_request", "каждый контакт — {phone, first_name, last_name?}")
        parsed.append((str(c["phone"]), str(c["first_name"]), str(c.get("last_name") or "")))
    if not parsed:
        raise GuardError("bad_request", "пустой список")
    check_outgoing([x for p in parsed for x in p[1:]], field="имя контакта")
    cl = client(account)
    res = await cl(functions.contacts.ImportContactsRequest(contacts=[
        types.InputPhoneContact(client_id=i, phone=p, first_name=f, last_name=l) for i, (p, f, l) in enumerate(parsed)]))
    return {"ok": True, "imported": [x.user_id for x in res.imported], "not_imported": len(parsed) - len(res.imported)}


@tool(readonly=True)
async def export_contacts(account: str | None = None) -> str:
    """Export all contacts. Returns JSON items: id, name, username, phone."""
    cl = client(account)
    res = await cl(functions.contacts.GetContactsRequest(hash=0))
    items = []
    for u in getattr(res, "users", []) or []:
        if isinstance(u, types.User):
            items.append({"id": u.id, "name": utils.get_display_name(u) or None, "username": u.username, "phone": u.phone})
    return {"items": items, "next": None}


@tool(readonly=True)
async def get_blocked_users(account: str | None = None) -> str:
    """List blocked users (first 100). Returns JSON items: id, name, username."""
    cl = client(account)
    res = await cl(functions.contacts.GetBlockedRequest(offset=0, limit=100))
    return {"items": _users(res), "next": None}


@tool()
async def send_contact(chat_id: int | str, phone_number: str, first_name: str, last_name: str = "", vcard: str = "",
                       account: str | None = None) -> str:
    """Send a contact card to a chat. Returns JSON: ok, message_id."""
    check_outgoing(phone_number, first_name, last_name, vcard, field="контакт")
    cl = client(account)
    ent = await resolve(cl, chat_id)
    sent = await cl.send_file(ent, types.InputMediaContact(phone_number=phone_number, first_name=first_name,
                                                           last_name=last_name, vcard=vcard))
    return {"ok": True, "chat_id": chat_id, "message_id": getattr(sent, "id", None)}
