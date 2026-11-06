"""Файловые стражи медиа-инструментов без сети: фейковый клиент вместо Telethon."""
import importlib
import json
import os
import sys
import types as pytypes
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl import types

from tg_mcp_safe import app
from tg_mcp_safe.guard.files import FileGuard


def _load_media():
    # Пакет tools импортирует все модули; пока соседние пишутся, грузим media напрямую.
    try:
        return importlib.import_module("tg_mcp_safe.tools.media")
    except ImportError:
        pkg_dir = Path(app.__file__).parent / "tools"
        pkg = pytypes.ModuleType("tg_mcp_safe.tools")
        pkg.__path__ = [str(pkg_dir)]
        sys.modules["tg_mcp_safe.tools"] = pkg
        return importlib.import_module("tg_mcp_safe.tools.media")


media = _load_media()


class FakeClient:
    def __init__(self, messages=None):
        self.calls = []
        self.messages = messages or {}

    async def get_entity(self, x):
        self.calls.append(("get_entity", x))
        return SimpleNamespace(id=x)

    async def send_file(self, ent, file, **kw):
        self.calls.append(("send_file", file, kw))
        return SimpleNamespace(id=101)

    async def get_messages(self, ent, ids=None, **kw):
        self.calls.append(("get_messages", ids))
        return self.messages.get(ids)

    async def download_media(self, msg, file=None):
        self.calls.append(("download_media", file))
        Path(file).write_bytes(b"\xff\xd8jpeg")
        return file


def _msg(mid, sender):
    return SimpleNamespace(id=mid, sender_id=sender, chat_id=sender, peer_id=None, message="",
                           media=types.MessageMediaPhoto(), reply_markup=None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "media"; root.mkdir()
    (root / "pic.jpg").write_bytes(b"\xff\xd8jpeg")
    (root / "leak.txt").write_text("deploy " + "glpat-" + "Ab3dEf6hIj9kLm2nOp5q")
    dl = tmp_path / "dl"
    fake = FakeClient({1: _msg(1, 777000), 2: _msg(2, 555)})
    monkeypatch.setattr(app, "clients", {"main": fake})
    monkeypatch.setattr(app, "files", FileGuard([str(root)], str(dl)))
    return fake, root, dl


def _err(res):
    return json.loads(res).get("error", {}).get("code")


async def test_send_outside_roots(env):
    fake, _, _ = env
    res = await media.send_file(chat_id=1, file_path=os.path.expanduser("~/.claude/.env"))
    assert _err(res) == "outside_roots"
    assert fake.calls == []


async def test_send_file_with_secret(env):
    fake, root, _ = env
    res = await media.send_file(chat_id=1, file_path=str(root / "leak.txt"))
    assert _err(res) == "file_contains_secret"
    assert fake.calls == []
    assert "glpat" not in res


async def test_send_ok(env):
    fake, root, _ = env
    res = json.loads(await media.send_file(chat_id=5, file_path="pic.jpg", caption="hi"))
    assert res["ok"] and res["message_id"] == 101
    sends = [c for c in fake.calls if c[0] == "send_file"]
    assert sends == [("send_file", str((root / "pic.jpg").resolve()), {"caption": "hi"})]


async def test_send_caption_secret(env):
    fake, _, _ = env
    res = await media.send_file(chat_id=5, file_path="pic.jpg", caption="token: " + "glpat-" + "Ab3dEf6hIj9kLm2nOp5q")
    assert _err(res) == "outgoing_secret"
    assert fake.calls == []


async def test_download_service_denied(env):
    fake, _, dl = env
    res = await media.download_media(chat_id=777000, message_id=1)
    assert _err(res) == "sensitive_message"
    assert not any(c[0] == "download_media" for c in fake.calls)
    assert not dl.exists() or not any(dl.iterdir())


async def test_download_traversal_stays_in_dir(env):
    fake, _, dl = env
    res = json.loads(await media.download_media(chat_id=555, message_id=2, file_path="../../x"))
    p = Path(res["path"])
    assert p.parent == dl.resolve() and p.exists()
    assert p.name == "x.jpg"
    assert res["size"] == 6 and res["mime"] == "image/jpeg"
