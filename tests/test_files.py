import os
import pytest
from tg_mcp_safe.guard.files import FileGuard
from tg_mcp_safe.guard.errors import GuardError

@pytest.fixture
def env(tmp_path):
    root = tmp_path / "media"; root.mkdir()
    (root / "ok.jpg").write_bytes(b"\xff\xd8jpeg")
    (root / "notes.txt").write_text("просто заметка")
    (root / "leak.txt").write_text("token: " + "glpat-" + "Ab3dEf6hIj9kLm2nOp5q")
    (root / ".env").write_text("A=1")
    (root / ".hidden").mkdir(); (root / ".hidden" / "a.jpg").write_bytes(b"x")
    (root / "id_rsa").write_text("x")
    outside = tmp_path / "secret.env"; outside.write_text("X=1")
    (root / "link.jpg").symlink_to(outside)
    dl = tmp_path / "dl"
    return FileGuard([str(root)], str(dl)), root, tmp_path

def test_ok(env):
    g, root, _ = env
    assert g.resolve_upload(str(root / "ok.jpg")).name == "ok.jpg"
    assert g.resolve_upload("notes.txt").name == "notes.txt"

@pytest.mark.parametrize("p,code", [
    ("../secret.env", "outside_roots"),
    ("link.jpg", "outside_roots"),
    (".env", "file_denied"),
    (".hidden/a.jpg", "file_denied"),
    ("id_rsa", "file_denied"),
    ("leak.txt", "file_contains_secret"),
    ("/etc/passwd", "outside_roots"),
    (os.path.expanduser("~/.claude/.env"), "outside_roots"),
])
def test_denied(env, p, code):
    g, _, _ = env
    with pytest.raises(GuardError) as e:
        g.resolve_upload(p)
    assert e.value.code == code

def test_download(env):
    g, _, tmp = env
    t = g.download_target(None, "photo.jpg")
    assert t.parent == tmp / "dl"
    t.write_bytes(b"1")
    assert g.download_target("photo.jpg", "x").name == "photo-1.jpg"
    assert g.download_target("../../evil.sh", "x").parent == tmp / "dl"
    with pytest.raises(GuardError):
        g.download_target(str(tmp / "elsewhere" / "a.jpg"), "x")

def test_no_roots():
    with pytest.raises(GuardError) as e:
        FileGuard([], None).resolve_upload("/tmp/x")
    assert e.value.code == "file_tools_disabled"
