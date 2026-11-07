"""Инструменты для своих каналов сверх эталона chigwell: HTML-посты с кастомными эмодзи,
наборы эмодзи, бусты и реакции. Нужны скриптам, которые раньше держали свою копию сессии.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from telethon import functions, types
from telethon.extensions import html as tg_html

from .. import app
from ..app import client, tool
from ..guard.errors import GuardError
from ..guard.messages import check_outgoing
from ..serialize import resolve

FFMPEG = "/usr/bin/ffmpeg"


def _visible(text_html: str | None) -> str | None:
    if not text_html:
        return text_html
    try:
        return tg_html.parse(text_html)[0]
    except Exception:  # noqa: BLE001  кривой HTML: проверяем как есть
        return text_html


async def _webp_to_jpeg(src: Path, tmp: str) -> Path:
    out = Path(tmp) / (src.stem + ".jpg")
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-v", "error", "-i", str(src), "-frames:v", "1", "-q:v", "2", str(out),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        raise GuardError("convert_failed", "webp → jpeg: ffmpeg не уложился в 60 с")
    if rc != 0 or not out.is_file():
        raise GuardError("convert_failed", "webp → jpeg: ffmpeg завершился с ошибкой")
    return out


@tool(destructive=True)
async def send_post(chat_id: int | str, html: str = None, file_path: str = None, as_document: bool = False,
                    link_preview: bool = False, account: str = None) -> str:
    """Post HTML (Telegram HTML incl. <tg-emoji emoji-id="ID">ALT</tg-emoji>) as text, or as the caption of a file
    from the server's allowed folders. .webp is sent as a photo unless as_document. Returns JSON: ok, message_id."""
    if not html and not file_path:
        raise GuardError("bad_request", "нужен html, file_path или оба")
    check_outgoing(_visible(html), field="текст поста")
    cl = client(account)
    ent = await resolve(cl, chat_id)
    if not file_path:
        msg = await cl.send_message(ent, html, parse_mode="html", link_preview=link_preview)
        return {"ok": True, "chat_id": chat_id, "message_id": msg.id}
    path = app.files.resolve_upload(file_path)
    with tempfile.TemporaryDirectory(prefix="tgpost-") as tmp:
        send = path
        if path.suffix.lower() == ".webp" and not as_document:
            send = await _webp_to_jpeg(path, tmp)
        msg = await cl.send_file(ent, str(send), caption=html or None, parse_mode="html",
                                 force_document=as_document)
    return {"ok": True, "chat_id": chat_id, "message_id": msg.id, "file": path.name}


def _doc_set(doc: types.Document):
    for a in doc.attributes:
        if isinstance(a, (types.DocumentAttributeCustomEmoji, types.DocumentAttributeSticker)):
            return a.stickerset
    return None


@tool(readonly=True)
async def get_custom_emoji_set(chat_id: int | str = "me", limit: int = 20, account: str = None) -> str:
    """Find the newest message (default: Saved Messages) with a custom emoji or sticker and return its whole set:
    {set:{id,short_name,title,count}, emoji:[{id,alt}]}. Use ids as <tg-emoji emoji-id="ID">ALT</tg-emoji>."""
    cl = client(account)
    ent = await resolve(cl, chat_id)
    doc = None
    async for m in cl.iter_messages(ent, limit=max(1, min(int(limit), 200))):
        ids = [e.document_id for e in (m.entities or []) if isinstance(e, types.MessageEntityCustomEmoji)]
        if ids:
            docs = await cl(functions.messages.GetCustomEmojiDocumentsRequest(document_id=ids[:1]))
            doc = docs[0] if docs else None
        elif m.sticker:
            doc = m.sticker
        if doc:
            break
    if doc is None:
        raise GuardError("not_found", f"в последних {limit} сообщениях нет кастомных эмодзи и стикеров")
    ss = _doc_set(doc)
    if ss is None or isinstance(ss, types.InputStickerSetEmpty):
        raise GuardError("not_found", "у эмодзи нет набора")
    full = await cl(functions.messages.GetStickerSetRequest(stickerset=ss, hash=0))
    alts: dict[int, str] = {}
    for pack in full.packs:
        for did in pack.documents:
            alts.setdefault(did, pack.emoticon)
    return {
        "set": {"id": str(full.set.id), "short_name": full.set.short_name, "title": full.set.title,
                "emojis": bool(full.set.emojis), "count": full.set.count},
        "emoji": [{"id": str(d.id), "alt": alts.get(d.id, "")} for d in full.documents],
    }


def _reactions(r) -> dict:
    if r is None or isinstance(r, types.ChatReactionsNone):
        return {"mode": "none"}
    if isinstance(r, types.ChatReactionsAll):
        return {"mode": "all", "allow_custom": bool(r.allow_custom)}
    out: dict = {"mode": "some", "emoticons": [], "custom_emoji_ids": []}
    for x in getattr(r, "reactions", []) or []:
        if isinstance(x, types.ReactionEmoji):
            out["emoticons"].append(x.emoticon)
        elif isinstance(x, types.ReactionCustomEmoji):
            out["custom_emoji_ids"].append(str(x.document_id))
    return out


@tool(readonly=True)
async def get_boosts_status(chat_id: int | str, account: str = None) -> str:
    """Boost level of a channel and the reactions it currently allows. Returns JSON:
    level, boosts, next_level_boosts, boost_url, reactions{mode, emoticons, custom_emoji_ids}."""
    cl = client(account)
    ent = await resolve(cl, chat_id)
    if not isinstance(ent, types.Channel):
        raise GuardError("bad_request", "только для каналов и супергрупп")
    full = await cl(functions.channels.GetFullChannelRequest(ent))
    b = await cl(functions.premium.GetBoostsStatusRequest(peer=ent))
    return {"level": b.level, "boosts": b.boosts, "next_level_boosts": b.next_level_boosts,
            "boost_url": b.boost_url, "reactions": _reactions(full.full_chat.available_reactions)}


@tool(destructive=True)
async def set_chat_reactions(chat_id: int | str, custom_emoji_ids: list[str] = None, emoticons: list[str] = None,
                             allow_all: bool = False, account: str = None) -> str:
    """Set the reactions allowed in a chat/channel: allow_all, or exactly the given emoticons and custom emoji ids
    (custom ones need channel boosts). Empty lists and allow_all=false disable reactions. Returns JSON: ok, count."""
    cl = client(account)
    ent = await resolve(cl, chat_id)
    if allow_all:
        avail = types.ChatReactionsAll(allow_custom=True)
        count = None
    else:
        items = [types.ReactionEmoji(emoticon=e) for e in emoticons or []]
        try:
            items += [types.ReactionCustomEmoji(document_id=int(i)) for i in custom_emoji_ids or []]
        except ValueError as exc:
            raise GuardError("bad_request", "custom_emoji_ids: только числа") from exc
        avail = types.ChatReactionsSome(reactions=items) if items else types.ChatReactionsNone()
        count = len(items)
    await cl(functions.messages.SetChatAvailableReactionsRequest(peer=ent, available_reactions=avail))
    return {"ok": True, "chat_id": chat_id, "count": count, "mode": "all" if allow_all else ("some" if count else "none")}

