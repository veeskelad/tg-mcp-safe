"""Папки чатов (dialog filters)."""

from __future__ import annotations

from typing import Any

from telethon import functions, types, utils

from ..app import client, tool
from ..guard.errors import GuardError
from ..guard.messages import check_outgoing
from ..serialize import entity, resolve

_FLAGS = ("contacts", "non_contacts", "groups", "broadcasts", "bots", "exclude_muted", "exclude_read", "exclude_archived")
_FOLDER_TYPES = (types.DialogFilter, types.DialogFilterChatlist)


def _title(f: Any) -> str:
    t = f.title
    return getattr(t, "text", t) or ""


async def _filters(cl: Any) -> list:
    res = await cl(functions.messages.GetDialogFiltersRequest())
    return [f for f in getattr(res, "filters", res) if isinstance(f, _FOLDER_TYPES)]


async def _folder(cl: Any, folder_id: int) -> Any:
    f = next((f for f in await _filters(cl) if f.id == folder_id), None)
    if f is None:
        raise GuardError("not_found", f"папка {folder_id} не найдена, см. list_folders")
    return f


def _summary(f: Any) -> dict:
    d: dict[str, Any] = {"id": f.id, "title": _title(f), "emoticon": getattr(f, "emoticon", None)}
    if isinstance(f, types.DialogFilterChatlist):
        d["type"] = "shared"
    else:
        d.update({k: bool(getattr(f, k, False)) for k in _FLAGS})
        d["excluded_count"] = len(f.exclude_peers or [])
    d["included_count"] = len(f.include_peers or [])
    d["pinned_count"] = len(f.pinned_peers or [])
    return d


async def _peers(cl: Any, peers: list) -> list[dict]:
    out = []
    for p in peers or []:
        try:
            out.append(entity(await cl.get_entity(p)))
        except (ValueError, TypeError):
            out.append({"id": utils.get_peer_id(p), "unresolved": True})
    return out


def _rebuild(f: Any, include: list, pinned: list) -> Any:
    common = dict(id=f.id, title=f.title, emoticon=getattr(f, "emoticon", None), pinned_peers=pinned, include_peers=include,
                  title_noanimate=getattr(f, "title_noanimate", None), color=getattr(f, "color", None))
    if isinstance(f, types.DialogFilterChatlist):
        return types.DialogFilterChatlist(has_my_invites=getattr(f, "has_my_invites", None), **common)
    return types.DialogFilter(exclude_peers=list(f.exclude_peers or []), **{k: getattr(f, k, None) for k in _FLAGS}, **common)


@tool(readonly=True)
async def list_folders(account: str = None) -> str:
    """List chat folders with ids, titles, emoji, filter flags and chat counts. Returns JSON items."""
    cl = client(account)
    return {"items": [_summary(f) for f in await _filters(cl)], "next": None}


@tool(readonly=True)
async def get_folder(folder_id: int, account: str = None) -> str:
    """Get a folder with its included, excluded and pinned chats. Returns JSON."""
    cl = client(account)
    f = await _folder(cl, folder_id)
    d = _summary(f)
    d["included"] = await _peers(cl, f.include_peers)
    d["pinned"] = await _peers(cl, f.pinned_peers)
    if not isinstance(f, types.DialogFilterChatlist):
        d["excluded"] = await _peers(cl, f.exclude_peers)
    return d


@tool()
async def create_folder(title: str, emoticon: str | None = None, chat_ids: list[int | str] | None = None, contacts: bool = False,
                        non_contacts: bool = False, groups: bool = False, broadcasts: bool = False, bots: bool = False,
                        exclude_muted: bool = False, exclude_read: bool = False, exclude_archived: bool = True,
                        account: str = None) -> str:
    """Create a chat folder with optional chats and type filters. Returns JSON: ok, folder_id."""
    check_outgoing(title, emoticon, field="название папки")
    if not title.strip():
        raise GuardError("bad_request", "пустое название")
    cl = client(account)
    existing = await _filters(cl)
    if len(existing) >= 10:
        raise GuardError("limit", "в Telegram не больше 10 папок, удалите одну")
    ids = {f.id for f in existing}
    new_id = 2
    while new_id in ids:
        new_id += 1
    include = [await cl.get_input_entity(await resolve(cl, c)) for c in chat_ids or []]
    flt = types.DialogFilter(
        id=new_id, title=types.TextWithEntities(text=title, entities=[]), emoticon=emoticon,
        pinned_peers=[], include_peers=include, exclude_peers=[], contacts=contacts, non_contacts=non_contacts,
        groups=groups, broadcasts=broadcasts, bots=bots, exclude_muted=exclude_muted, exclude_read=exclude_read,
        exclude_archived=exclude_archived)
    await cl(functions.messages.UpdateDialogFilterRequest(id=new_id, filter=flt))
    return {"ok": True, "folder_id": new_id, "included_count": len(include)}


@tool(idempotent=True)
async def add_chat_to_folder(folder_id: int, chat_id: int | str, pinned: bool = False, account: str = None) -> str:
    """Add a chat to a folder, optionally pinned there. Returns JSON: ok, changed."""
    cl = client(account)
    f = await _folder(cl, folder_id)
    peer = await cl.get_input_entity(await resolve(cl, chat_id))
    pid = utils.get_peer_id(peer)
    include, pins = list(f.include_peers or []), list(f.pinned_peers or [])
    in_inc = any(utils.get_peer_id(p) == pid for p in include)
    in_pin = any(utils.get_peer_id(p) == pid for p in pins)
    if (in_inc or in_pin) and (not pinned or in_pin):
        return {"ok": True, "changed": False, "folder_id": folder_id, "chat_id": pid}
    if pinned:
        pins.append(peer)
        include = [p for p in include if utils.get_peer_id(p) != pid]
    elif not in_inc:
        include.append(peer)
    await cl(functions.messages.UpdateDialogFilterRequest(id=folder_id, filter=_rebuild(f, include, pins)))
    return {"ok": True, "changed": True, "folder_id": folder_id, "chat_id": pid, "pinned": pinned}


@tool(idempotent=True)
async def remove_chat_from_folder(folder_id: int, chat_id: int | str, account: str = None) -> str:
    """Remove a chat from a folder (from included and pinned). Returns JSON: ok, changed."""
    cl = client(account)
    f = await _folder(cl, folder_id)
    pid = utils.get_peer_id(await cl.get_input_entity(await resolve(cl, chat_id)))
    include = [p for p in f.include_peers or [] if utils.get_peer_id(p) != pid]
    pins = [p for p in f.pinned_peers or [] if utils.get_peer_id(p) != pid]
    if len(include) == len(f.include_peers or []) and len(pins) == len(f.pinned_peers or []):
        return {"ok": True, "changed": False, "folder_id": folder_id, "chat_id": pid}
    await cl(functions.messages.UpdateDialogFilterRequest(id=folder_id, filter=_rebuild(f, include, pins)))
    return {"ok": True, "changed": True, "folder_id": folder_id, "chat_id": pid}


@tool(destructive=True, idempotent=True)
async def delete_folder(folder_id: int, account: str = None) -> str:
    """Delete a folder; its chats stay. Returns JSON: ok, folder_id."""
    if folder_id < 2:
        raise GuardError("bad_request", "системную папку удалить нельзя")
    cl = client(account)
    await _folder(cl, folder_id)
    await cl(functions.messages.UpdateDialogFilterRequest(id=folder_id, filter=None))
    return {"ok": True, "folder_id": folder_id}


@tool(idempotent=True)
async def reorder_folders(folder_ids: list[int], account: str = None) -> str:
    """Reorder folders; folder_ids must list every folder id once. Returns JSON: ok, order."""
    cl = client(account)
    existing = {f.id for f in await _filters(cl)}
    if len(folder_ids) != len(set(folder_ids)) or set(folder_ids) != existing:
        raise GuardError("bad_request", f"нужны все id папок ровно по разу: {sorted(existing)}")
    await cl(functions.messages.UpdateDialogFiltersOrderRequest(order=folder_ids))
    return {"ok": True, "order": folder_ids}
