"""Файлы с кредами: только из папки секретов, только в личку людям из списка."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from telethon.tl import types

import tg_mcp_safe.tools  # noqa: F401
from tg_mcp_safe import app
from tg_mcp_safe.guard.errors import GuardError
from tg_mcp_safe.guard.files import FileGuard
from tg_mcp_safe.tools import media

KUBE = "users:\n- name: u\n  user:\n    token: " + "eyJhbGciOiJSUzI1NiIsImtpZCI6IjEifQ." + "eyJzdWIiOiJ1In0" + ".c2lnbmF0dXJlc2lnbmF0dXJl\n"
OK_USER, OTHER_USER = 111, 222


def user(uid, bot=False):
    return types.User(id=uid, access_hash=1, bot=bot, first_name="x")


class FakeClient:
    def __init__(self, ent):
        self.ent, self.sent = ent, []

    async def get_entity(self, x):
        return self.ent

    async def get_input_entity(self, x):
        return self.ent

    async def send_file(self, ent, file, **kw):
        self.sent.append(file)
        return SimpleNamespace(id=7)


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "media"; root.mkdir()
    box = root / "secret-outbox"; box.mkdir()
    (box / "u-x-vk.yaml").write_text(KUBE)
    (root / "plain.yaml").write_text(KUBE)
    g = FileGuard([str(root)], str(tmp_path / "dl"), str(box), [OK_USER])
    monkeypatch.setattr(app, "files", g)
    return g, root, box


def send(monkeypatch, ent, path, album=False):
    fc = FakeClient(ent)
    monkeypatch.setattr(app, "clients", {"default": fc})
    arg = [path, path] if album else path
    return fc, json.loads(asyncio.run(media.send_file("@x", arg)))


def test_guard_level(env):
    g, root, box = env
    assert g.resolve_upload(str(box / "u-x-vk.yaml"), secret_peer=OK_USER).name == "u-x-vk.yaml"
    for peer in (None, OTHER_USER):
        with pytest.raises(GuardError) as e:
            g.resolve_upload(str(box / "u-x-vk.yaml"), secret_peer=peer)
        assert e.value.code == "file_contains_secret"
    with pytest.raises(GuardError) as e:
        g.resolve_upload(str(root / "plain.yaml"), secret_peer=OK_USER)
    assert e.value.code == "file_contains_secret"


def test_disabled_without_config(tmp_path):
    root = tmp_path / "m"; root.mkdir(); (root / "a.yaml").write_text(KUBE)
    g = FileGuard([str(root)], None)
    with pytest.raises(GuardError):
        g.resolve_upload(str(root / "a.yaml"), secret_peer=OK_USER)


def test_send_to_listed_user(env, monkeypatch):
    _, _, box = env
    fc, out = send(monkeypatch, user(OK_USER), str(box / "u-x-vk.yaml"))
    assert out.get("ok") is True and len(fc.sent) == 1


@pytest.mark.parametrize("ent", [
    user(OTHER_USER),
    user(OK_USER, bot=True),
    types.Channel(id=OK_USER, title="c", photo=types.ChatPhotoEmpty(), date=None, access_hash=1, broadcast=True),
    types.Chat(id=OK_USER, title="g", photo=types.ChatPhotoEmpty(), participants_count=2, date=None, version=1),
])
def test_send_refused(env, monkeypatch, ent):
    _, _, box = env
    fc, out = send(monkeypatch, ent, str(box / "u-x-vk.yaml"))
    assert fc.sent == [] and "file_contains_secret" in json.dumps(out)


def test_album_stays_strict(env, monkeypatch):
    _, _, box = env
    fc, out = send(monkeypatch, user(OK_USER), str(box / "u-x-vk.yaml"), album=True)
    assert fc.sent == [] and "file_contains_secret" in json.dumps(out)
