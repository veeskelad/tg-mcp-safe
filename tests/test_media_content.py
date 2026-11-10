"""get_media_content без сети: файл в память, стражи до выдачи байтов."""
import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace

import pytest
from mcp.types import ImageContent, TextContent
from telethon.tl import types

from tg_mcp_safe import app
from tg_mcp_safe.guard.files import FileGuard
import tg_mcp_safe.tools  # noqa: F401
from tg_mcp_safe.tools import media

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes" * 10


def _pdf(text: str) -> bytes:
    """Минимальный PDF с одной строкой текста."""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offs:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _doc(name, mime, size):
    return types.MessageMediaDocument(document=types.Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type=mime, size=size, dc_id=1,
        attributes=[types.DocumentAttributeFilename(file_name=name)]))


def _msg(mid, sender, media, text=""):
    return SimpleNamespace(id=mid, sender_id=sender, chat_id=sender, peer_id=None, message=text,
                           media=media, reply_markup=None)


class FakeClient:
    def __init__(self, messages, payloads):
        self.messages, self.payloads, self.calls = messages, payloads, []

    async def get_entity(self, x):
        return SimpleNamespace(id=x)

    async def get_messages(self, ent, ids=None, **kw):
        return self.messages.get(ids)

    async def download_media(self, msg, file=None):
        self.calls.append(("download_media", file))
        assert file is bytes, "файл должен читаться в память"
        return self.payloads[msg.id]


SECRET_TXT = ("deploy " + "glpat-" + "Ab3dEf6hIj9kLm2nOp5q").encode()
PDF = _pdf("Wise transfer 1650 JPY akimitsu toyooka")


@pytest.fixture
def env(tmp_path, monkeypatch):
    msgs = {
        1: _msg(1, 777000, types.MessageMediaPhoto()),
        2: _msg(2, 555, types.MessageMediaPhoto()),
        3: _msg(3, 555, _doc("wise.pdf", "application/pdf", len(PDF))),
        4: _msg(4, 555, _doc("leak.txt", "text/plain", len(SECRET_TXT))),
        5: _msg(5, 555, _doc("big.pdf", "application/pdf", 6 * 1024 * 1024)),
        6: _msg(6, 555, _doc("a.zip", "application/zip", 10)),
        7: _msg(7, 555, _doc("big.txt", "text/plain", 60 * 1024)),
        8: _msg(8, 555, None),
    }
    payloads = {2: JPEG, 3: PDF, 4: SECRET_TXT, 6: b"PK\x03\x04zip", 7: b"word " * (12 * 1024)}
    fake = FakeClient(msgs, payloads)
    monkeypatch.setattr(app, "clients", {"main": fake})
    monkeypatch.setattr(app, "files", FileGuard([], None))
    return fake


def _meta(blocks):
    assert isinstance(blocks[0], TextContent)
    return json.loads(blocks[0].text)


def _err(blocks):
    return _meta(blocks).get("error", {}).get("code")


async def test_photo_auto_returns_image(env):
    blocks = await media.get_media_content(chat_id=555, message_id=2)
    meta = _meta(blocks)
    assert meta["mime"] == "image/jpeg" and meta["size"] == len(JPEG)
    assert meta["sha256"] == hashlib.sha256(JPEG).hexdigest()
    assert isinstance(blocks[1], ImageContent) and base64.b64decode(blocks[1].data) == JPEG


async def test_pdf_auto_extracts_text(env):
    blocks = await media.get_media_content(chat_id=555, message_id=3)
    meta = _meta(blocks)
    assert "1650 JPY akimitsu toyooka" in meta["text"]
    assert meta["pages"] == 1 and len(blocks) == 1


async def test_pdf_base64_roundtrip(env):
    blocks = await media.get_media_content(chat_id=555, message_id=3, mode="base64")
    assert len(blocks) == 2
    assert base64.b64decode(blocks[1].text) == PDF


async def test_text_secret_masked_and_base64_denied(env):
    blocks = await media.get_media_content(chat_id=555, message_id=4)
    out = json.dumps([b.model_dump() for b in blocks])
    assert "glpat" not in out
    assert _meta(blocks)["secrets_masked"]
    blocks = await media.get_media_content(chat_id=555, message_id=4, mode="base64")
    assert _err(blocks) == "file_contains_secret"
    assert len(blocks) == 1 and "glpat" not in blocks[0].text


async def test_service_chat_denied_without_download(env):
    blocks = await media.get_media_content(chat_id=777000, message_id=1)
    assert _err(blocks) == "sensitive_message"
    assert env.calls == []


async def test_too_large_checked_before_download(env):
    assert _err(await media.get_media_content(chat_id=555, message_id=5)) == "too_large"
    assert env.calls == []


async def test_base64_limits(env):
    assert _err(await media.get_media_content(chat_id=555, message_id=6, mode="base64")) == "unsupported_type"
    assert _err(await media.get_media_content(chat_id=555, message_id=7, mode="base64")) == "too_large_for_base64"
    assert _err(await media.get_media_content(chat_id=555, message_id=8)) == "no_media"
    meta = _meta(await media.get_media_content(chat_id=555, message_id=7))
    assert meta["text_truncated"] is True and len(meta["text"]) == media.MAX_TEXT_CHARS
    assert _err(await media.get_media_content(chat_id=555, message_id=2, mode="raw")) == "bad_request"


async def test_server_secret_in_file_blocked(env, monkeypatch):
    monkeypatch.setattr(app, "_secrets", ["jpeg-bytes"])
    blocks = await media.get_media_content(chat_id=555, message_id=2)
    assert _err(blocks) == "blocked" and len(blocks) == 1


async def test_multi_account_requires_account(env, monkeypatch):
    monkeypatch.setattr(app, "clients", {"a": env, "b": env})
    assert _err(await media.get_media_content(chat_id=555, message_id=2)) == "account_required"


def test_registered_unstructured_readonly():
    t = next(x for x in asyncio.run(app.mcp.list_tools()) if x.name == "get_media_content")
    assert t.outputSchema is None and t.annotations.readOnlyHint is True
    assert set(t.inputSchema["properties"]) == {"chat_id", "message_id", "mode", "account"}
