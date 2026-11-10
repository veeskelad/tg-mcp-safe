"""Медиа: отправка файлов из серверных папок, скачивание в папку загрузок, стикеры, GIF."""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any

from mcp.types import ImageContent, TextContent
from telethon import functions, types

from .. import app
from ..app import Rich, client, tool
from ..guard import redact
from ..guard.files import _TEXT_EXT
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


# get_media_content: файл читается в память, на диск сервера не пишется.
MAX_CONTENT_BYTES = 5 * 1024 * 1024
MAX_BASE64_BYTES = 48 * 1024  # base64 модель переписывает в другой вызов: больше — дорого и ненадёжно
MAX_TEXT_CHARS = 20000
MAX_PDF_PAGES = 50
_INLINE_IMAGES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_TEXT_MIMES = {"application/json", "application/xml", "application/x-yaml", "application/yaml"}

logging.getLogger("pypdf").setLevel(logging.ERROR)


def _content_kind(mime: str, name: str) -> str:
    if mime == "application/pdf" or name.lower().endswith(".pdf"):
        return "pdf"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("text/") or mime in _TEXT_MIMES or (Path(name).suffix.lower() in _TEXT_EXT and Path(name).suffix):
        return "text"
    return "other"


def _pdf_text(data: bytes) -> tuple[str | None, int | None, str | None]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return None, None, "PDF зашифрован, текст не извлечь"
        pages = reader.pages
        parts = [(pg.extract_text() or "") for pg in list(pages)[:MAX_PDF_PAGES]]
        return "\n".join(parts).strip(), len(pages), None
    except Exception as exc:  # noqa: BLE001 — битый PDF не должен ронять ответ
        return None, None, f"текст PDF не извлечь: {type(exc).__name__}"


@tool(readonly=True, rich=True, fan_out=False)
async def get_media_content(chat_id: int | str, message_id: int, mode: str = "auto", account: str = None) -> str:
    """Read a message's file without saving it: images return as an image, PDF/text files as extracted text.
    mode="base64" also returns the raw file (up to 48 KB) as base64 in the next block, e.g. for a Gmail attachment's content.
    Returns JSON: file_name, mime, size, sha256, text, pages, base64."""
    if mode not in ("auto", "base64"):
        raise GuardError("bad_request", "mode: auto или base64")
    cl = client(account)
    _, msg = await _get_message(cl, chat_id, message_id)
    deny_if_sensitive(msg, "get_media_content")
    m = getattr(msg, "media", None)
    if not m:
        raise GuardError("no_media", "в сообщении нет медиа")
    if not isinstance(m, (types.MessageMediaPhoto, types.MessageMediaDocument)):
        raise GuardError("no_file", "в сообщении нет файла (ссылка, опрос, контакт или геоточка)")
    doc = getattr(m, "document", None)
    if getattr(doc, "size", 0) and doc.size > MAX_CONTENT_BYTES:
        raise GuardError("too_large", "файл больше 5 МБ: используйте download_media")
    data = await cl.download_media(msg, file=bytes)
    if not data:
        raise GuardError("download_failed", "Telegram не отдал файл")
    if len(data) > MAX_CONTENT_BYTES:
        raise GuardError("too_large", "файл больше 5 МБ: используйте download_media")

    name = _media_name(msg) or f"msg_{message_id}{_media_ext(msg)}"
    mime = getattr(doc, "mime_type", None) if doc is not None else "image/jpeg"
    if not mime or mime == "application/octet-stream":
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    kind = _content_kind(mime, name)
    meta: dict[str, Any] = {
        "chat_id": chat_id, "message_id": message_id, "file_name": name, "mime": mime,
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    }
    if mode == "base64":
        if kind == "other":
            raise GuardError("unsupported_type", "base64 отдаётся только для картинок, PDF и текстовых файлов")
        if len(data) > MAX_BASE64_BYTES:
            raise GuardError("too_large_for_base64", "base64 отдаётся для файлов до 48 КБ")
    blocks: list = []
    text: str | None = None
    if kind == "pdf":
        text, pages, note = _pdf_text(data)
        meta["pages"] = pages
        if note:
            meta["note"] = note
    elif kind == "text" and b"\x00" not in data[:8192]:
        text = data.decode("utf-8", errors="replace")
    if text is not None:
        truncated = len(text) > MAX_TEXT_CHARS
        if mode == "auto":
            text = text[:MAX_TEXT_CHARS]  # маскировка квадратична на длинных словах: проверяем только то, что отдаём
        meta["text"] = text[:MAX_TEXT_CHARS]  # finalize замаскирует креды
        meta["text_truncated"] = truncated
    findings = redact.detect(text) if text else []
    if findings:
        meta["secrets_masked"] = findings
    if kind == "image" and mode == "auto" and mime in _INLINE_IMAGES:
        blocks.append(ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime))

    if mode == "base64":
        if kind == "pdf" and text is None:
            raise GuardError("unscannable", "PDF не проверить на креды (зашифрован или битый): base64 не отдаётся")
        if findings:
            raise GuardError("file_contains_secret", "в файле найдены креды: " + ", ".join(findings))
        meta["base64"] = "следующий блок: содержимое файла в base64 целиком, без пробелов и переносов"
        blocks.append(TextContent(type="text", text=base64.b64encode(data).decode()))
    return Rich(meta, blocks, raw=data)


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
