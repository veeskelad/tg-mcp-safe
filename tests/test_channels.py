"""Свои инструменты каналов без сети: HTML-посты, наборы эмодзи, бусты, реакции."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl import types

import tg_mcp_safe.tools  # noqa: F401
from tg_mcp_safe import app, serialize
from tg_mcp_safe.call import ToolError, parse_result
from tg_mcp_safe.guard.files import FileGuard
from tg_mcp_safe.tools import channels

SECRET = "glpat-" + "Ab3dEf6hIj9kLm2nOp5q"


class FakeClient:
    def __init__(self, history=()):
        self.calls = []
        self.history = list(history)

    async def get_entity(self, x):
        return types.Channel(id=5, title="ninjiu", photo=types.ChatPhotoEmpty(), date=None, access_hash=1,
                             broadcast=True, creator=True)

    async def send_message(self, ent, text, **kw):
        self.calls.append(("send_message", text, kw))
        return SimpleNamespace(id=11)

    async def send_file(self, ent, file, **kw):
        self.calls.append(("send_file", file, kw))
        return SimpleNamespace(id=12)

    async def iter_messages(self, ent, limit=None):
        for m in self.history:
            yield m

    async def __call__(self, req):
        self.calls.append((type(req).__name__, req))
        name = type(req).__name__
        if name == "GetCustomEmojiDocumentsRequest":
            return [types.Document(id=77, access_hash=1, file_reference=b"", date=None, mime_type="x", size=1, dc_id=1,
                                   attributes=[types.DocumentAttributeCustomEmoji(
                                       alt="🔥", stickerset=types.InputStickerSetID(id=9, access_hash=1))])]
        if name == "GetStickerSetRequest":
            return SimpleNamespace(
                set=SimpleNamespace(id=9, short_name="ninjiu", title="Ninjiu", emojis=True, count=2),
                packs=[SimpleNamespace(emoticon="🔥", documents=[77]), SimpleNamespace(emoticon="⚡", documents=[78])],
                documents=[SimpleNamespace(id=77), SimpleNamespace(id=78)])
        if name == "GetFullChannelRequest":
            return SimpleNamespace(full_chat=SimpleNamespace(available_reactions=types.ChatReactionsSome(
                reactions=[types.ReactionEmoji(emoticon="👍"), types.ReactionCustomEmoji(document_id=77)])))
        if name == "GetBoostsStatusRequest":
            return SimpleNamespace(level=2, boosts=9, next_level_boosts=15, boost_url="https://t.me/boost/ninjiu")
        return SimpleNamespace()


@pytest.fixture
def fc(monkeypatch, tmp_path):
    c = FakeClient()
    monkeypatch.setattr(app, "clients", {"default": c})
    root = tmp_path / "drafts"; root.mkdir()
    (root / "image.webp").write_bytes(b"RIFF....WEBP")
    (root / "photo.jpg").write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(app, "files", FileGuard([str(root)], str(tmp_path / "dl")))
    return c, root


def run(coro):
    import asyncio
    return json.loads(asyncio.run(coro))


def test_send_post_html_text(fc):
    c, _ = fc
    html = 'Пост <tg-emoji emoji-id="5368324170671202286">🔥</tg-emoji> <a href="https://x.y">тут</a>'
    r = run(channels.send_post(chat_id=-1005, html=html))
    assert r["ok"] and r["message_id"] == 11
    name, text, kw = c.calls[0]
    assert name == "send_message" and text == html and kw == {"parse_mode": "html", "link_preview": False}


def test_send_post_blocks_secret_in_visible_text(fc):
    c, _ = fc
    r = run(channels.send_post(chat_id=-1005, html=f"<b>ключ</b> {SECRET}"))
    assert r["error"]["code"] == "outgoing_secret" and not c.calls


def test_send_post_webp_converted(fc, monkeypatch):
    c, root = fc
    seen = {}

    async def fake_convert(src, tmp):
        seen["src"] = src
        out = Path(tmp) / "image.jpg"; out.write_bytes(b"jpg")
        return out
    monkeypatch.setattr(channels, "_webp_to_jpeg", fake_convert)
    r = run(channels.send_post(chat_id=-1005, html="подпись", file_path=str(root / "image.webp")))
    assert r["ok"] and seen["src"].name == "image.webp"
    name, file, kw = c.calls[0]
    assert file.endswith("image.jpg") and kw["parse_mode"] == "html" and kw["caption"] == "подпись"


def test_send_post_webp_as_document_not_converted(fc, monkeypatch):
    c, root = fc
    monkeypatch.setattr(channels, "_webp_to_jpeg", lambda *a: (_ for _ in ()).throw(AssertionError("no convert")))
    r = run(channels.send_post(chat_id=-1005, file_path=str(root / "image.webp"), as_document=True))
    assert r["ok"] and c.calls[0][1].endswith("image.webp") and c.calls[0][2]["force_document"] is True


def test_send_post_outside_roots(fc):
    c, _ = fc
    r = run(channels.send_post(chat_id=-1005, html="x", file_path="/etc/passwd"))
    assert r["error"]["code"] == "outside_roots" and not c.calls


def test_custom_emoji_set(fc):
    c, _ = fc
    c.history = [SimpleNamespace(entities=None, sticker=None),
                 SimpleNamespace(entities=[types.MessageEntityCustomEmoji(offset=0, length=2, document_id=77)], sticker=None)]
    r = run(channels.get_custom_emoji_set())
    assert r["set"]["short_name"] == "ninjiu" and r["emoji"] == [{"id": "77", "alt": "🔥"}, {"id": "78", "alt": "⚡"}]


def test_boosts_status(fc):
    r = run(channels.get_boosts_status(chat_id=-1005))
    assert r["level"] == 2 and r["reactions"] == {"mode": "some", "emoticons": ["👍"], "custom_emoji_ids": ["77"]}


def test_set_reactions(fc):
    c, _ = fc
    r = run(channels.set_chat_reactions(chat_id=-1005, custom_emoji_ids=["77", "78"], emoticons=["👍"]))
    assert r == {"ok": True, "chat_id": -1005, "count": 3, "mode": "some"}
    req = [x for n, x in c.calls if n == "SetChatAvailableReactionsRequest"][0]
    assert [type(x).__name__ for x in req.available_reactions.reactions] == ["ReactionEmoji", "ReactionCustomEmoji", "ReactionCustomEmoji"]
    bad = run(channels.set_chat_reactions(chat_id=-1005, custom_emoji_ids=["x"]))
    assert bad["error"]["code"] == "bad_request"


def test_serialize_forwards_links_creator():
    m = types.Message(id=1, peer_id=types.PeerChannel(5), date=None, message="читай тут", views=10, forwards=3,
                      entities=[types.MessageEntityTextUrl(offset=6, length=3, url="https://example.com/a")])
    d = serialize.message(m, with_sender=False)
    assert d["forwards"] == 3 and d["links"] == ["https://example.com/a"]
    e = types.Channel(id=5, title="x", photo=types.ChatPhotoEmpty(), date=None, access_hash=1, broadcast=True, creator=True)
    assert serialize.entity(e)["creator"] is True


def test_call_parse_result():
    assert parse_result("t", '{"ok":true}') == {"ok": True}
    with pytest.raises(ToolError) as e:
        parse_result("t", '{"error":{"code":"outside_roots","reason":"нет"}}')
    assert e.value.code == "outside_roots"
