"""Медиа: отправка файлов из серверных папок, скачивание в папку загрузок, стикеры, GIF."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

from telethon import functions, types

from .. import app
from ..app import client, tool
from ..guard import redact
from ..guard.errors import GuardError
from ..guard.messages import check_outgoing, deny_if_sensitive, is_service_sender
from ..serialize import media as media_info, resolve

# Документы GIF из последнего поиска: send_gif берёт отсюда access_hash.
_gif_cache: dict[int, Any] = {}
_GIF_CACHE_MAX = 500


def _ids(sent: Any) -> dict:
    if isinstance(sent, (list, tuple)):
        return {"message_ids": [getattr(m, "id", None) for m in sent]}
    return {"message_id": getattr(sent, "id", None)}


def _media_ext(msg: Any) -> str:
    m = getattr(msg, "media", None)
    if isinstance(m, types.MessageMediaPhoto):
        return ".jpg"
    doc = getattr(m, "document", None)
    for a in getattr(doc, "attributes", []) or []:
        fn = getattr(a, "file_name", None)
        if fn and Path(fn).suffix:
            return Path(fn).suffix.lower()
    mime = getattr(doc, "mime_type", None)
    if mime:
        ext = mimetypes.guess_extension(mime) or ""
        if mime == "audio/ogg":
            ext = ".ogg"
        return ext
    return ""


def _media_name(msg: Any) -> str | None:
    doc = getattr(getattr(msg, "media", None), "document", None)
    for a in getattr(doc, "attributes", []) or []:
        fn = getattr(a, "file_name", None)
        if fn:
            return fn
    return None


async def _get_message(cl: Any, chat_id: int | str, message_id: int) -> tuple[Any, Any]:
    ent = await resolve(cl, chat_id)
    msg = await cl.get_messages(ent, ids=message_id)
    if msg is None:
        raise GuardError("not_found", "сообщение не найдено")
    return ent, msg


async def _send_paths(chat_id: int | str, paths: list[str], caption: str | None, account: str | None) -> dict:
    if not 2 <= len(paths) <= 10:
        raise GuardError("bad_request", "в альбоме должно быть от 2 до 10 файлов")
    check_outgoing(caption, field="подпись")
    resolved = [str(app.files.resolve_upload(p)) for p in paths]
    cl = client(account)
    ent = await resolve(cl, chat_id)
    sent = await cl.send_file(ent, resolved, caption=caption)
    return {"ok": True, "chat_id": chat_id, "files": len(resolved), **_ids(sent)}


@tool(destructive=True)
async def send_file(chat_id: int | str, file_path: str | list[str], caption: str = None, account: str = None) -> str:
    """Send a file from the server's allowed folders to a chat; a list of 2-10 paths is sent as an album.
    Returns JSON: ok, message_id(s)."""
    if isinstance(file_path, list):
        return await _send_paths(chat_id, file_path, caption, account)
    check_outgoing(caption, field="подпись")
    path = app.files.resolve_upload(file_path)
    cl = client(account)
    ent = await resolve(cl, chat_id)
    sent = await cl.send_file(ent, str(path), caption=caption)
    return {"ok": True, "chat_id": chat_id, "file": path.name, **_ids(sent)}


@tool(destructive=True)
async def send_album(chat_id: int | str, file_paths: list[str], caption: str = None, account: str = None) -> str:
    """Send 2-10 photos/videos from the server's allowed folders as one album. Caption shows on the first item.
    Returns JSON: ok, message_ids."""
    if not isinstance(file_paths, list):
        raise GuardError("bad_request", "file_paths должен быть списком")
    return await _send_paths(chat_id, file_paths, caption, account)


@tool(destructive=True)
async def download_media(chat_id: int | str, message_id: int, file_path: str | None = None, account: str = None) -> str:
    """Download media of a message into the server's download folder (file_path is only a file name hint).
    Returns JSON: path, size, mime."""
    cl = client(account)
    _, msg = await _get_message(cl, chat_id, message_id)
    deny_if_sensitive(msg, "download_media")
    if not getattr(msg, "media", None):
        raise GuardError("no_media", "в сообщении нет медиа")
    ext = _media_ext(msg)
    default = _media_name(msg) or f"msg_{message_id}{ext}"
    if not Path(default).suffix and ext:
        default += ext
    requested = file_path
    if requested and ext and not Path(requested).suffix:
        requested = requested + ext
    target = app.files.download_target(requested, default)
    dl_dir = app.files.download_dir
    downloaded = await cl.download_media(msg, file=str(target))
    if not downloaded:
        raise GuardError("download_failed", "Telegram не отдал файл")
    final = Path(os.path.realpath(downloaded))
    if dl_dir is None or not (final.parent == dl_dir or dl_dir in final.parents):
        try:
            final.unlink()
        except OSError:
            pass
        raise GuardError("outside_download_dir", "файл оказался вне папки загрузок и удалён")
    mime = getattr(getattr(msg.media, "document", None), "mime_type", None) or mimetypes.guess_type(final.name)[0]
    return {"ok": True, "path": str(final), "size": final.stat().st_size, "mime": mime}


@tool(destructive=True)
async def send_voice(chat_id: int | str, file_path: str, account: str = None) -> str:
    """Send an OGG/OPUS file from the server's allowed folders as a voice message. Returns JSON: ok, message_id."""
    path = app.files.resolve_upload(file_path)
    if path.suffix.lower() not in (".ogg", ".opus", ".oga"):
        raise GuardError("bad_request", "голосовое должно быть .ogg или .opus")
    cl = client(account)
    ent = await resolve(cl, chat_id)
    sent = await cl.send_file(ent, str(path), voice_note=True)
    return {"ok": True, "chat_id": chat_id, **_ids(sent)}


@tool(destructive=True)
async def upload_file(file_path: str, account: str = None) -> str:
    """Upload a file from the server's allowed folders to Telegram without sending. Returns JSON: name, size, md5."""
    path = app.files.resolve_upload(file_path)
    cl = client(account)
    up = await cl.upload_file(str(path))
    return {
        "ok": True,
        "path": str(path),
        "name": getattr(up, "name", path.name),
        "size": path.stat().st_size,
        "md5_checksum": getattr(up, "md5_checksum", None) or None,
    }


@tool(readonly=True)
async def get_media_info(chat_id: int | str, message_id: int, account: str = None) -> str:
    """Describe media of a message: type, mime, size, file name, duration, caption. Returns JSON."""
    cl = client(account)
    _, msg = await _get_message(cl, chat_id, message_id)
    if is_service_sender(msg):
        raise GuardError("sensitive_message", "get_media_info: служебное сообщение Telegram (коды входа) — запрещено")
    info = media_info(msg)
    if not info:
        raise GuardError("no_media", "в сообщении нет медиа")
    out: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "media": info}
    if msg.message:
        out["caption"] = redact.redact(msg.message)
    return out


@tool(readonly=True)
async def get_sticker_sets(account: str = None) -> str:
    """List installed sticker sets. Returns JSON items: id, title, short_name, count."""
    cl = client(account)
    res = await cl(functions.messages.GetAllStickersRequest(hash=0))
    items = [
        {"id": s.id, "title": s.title, "short_name": s.short_name, "count": s.count}
        for s in getattr(res, "sets", []) or []
    ]
    return {"items": items, "next": None}


@tool(destructive=True)
async def send_sticker(chat_id: int | str, file_path: str, account: str = None) -> str:
    """Send a .webp/.tgs/.webm sticker file from the server's allowed folders. Returns JSON: ok, message_id."""
    path = app.files.resolve_upload(file_path)
    cl = client(account)
    ent = await resolve(cl, chat_id)
    sent = await cl.send_file(ent, str(path), force_document=False)
    return {"ok": True, "chat_id": chat_id, **_ids(sent)}


@tool(readonly=True)
async def get_gif_search(query: str, limit: int = 10, account: str = None) -> str:
    """Search GIFs via the @gif inline bot. Returns JSON items: id (use with send_gif), title."""
    cl = client(account)
    limit = max(1, min(int(limit), 50))
    results = await cl.inline_query("gif", query)
    items = []
    for r in results:
        doc = getattr(r.result, "document", None)
        if doc is None:
            continue
        if len(_gif_cache) >= _GIF_CACHE_MAX:
            _gif_cache.pop(next(iter(_gif_cache)))
        _gif_cache[doc.id] = doc
        items.append({"id": doc.id, "title": getattr(r.result, "title", None)})
        if len(items) >= limit:
            break
    return {"items": items, "next": None}


@tool(destructive=True)
async def send_gif(chat_id: int | str, gif_id: int, account: str = None) -> str:
    """Send a GIF by document id from get_gif_search or your saved GIFs. Returns JSON: ok, message_id."""
    if not isinstance(gif_id, int):
        raise GuardError("bad_request", "gif_id — id документа из get_gif_search, не путь к файлу")
    cl = client(account)
    doc = _gif_cache.get(gif_id)
    if doc is None:
        saved = await cl(functions.messages.GetSavedGifsRequest(hash=0))
        doc = next((d for d in getattr(saved, "gifs", []) or [] if d.id == gif_id), None)
    if doc is None:
        raise GuardError("not_found", "GIF не найден: сначала get_gif_search")
    ent = await resolve(cl, chat_id)
    sent = await cl.send_file(ent, doc)
    return {"ok": True, "chat_id": chat_id, **_ids(sent)}
