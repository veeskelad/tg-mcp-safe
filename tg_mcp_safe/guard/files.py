"""Файловый страж: какие файлы можно отправить в Telegram и куда скачивать.

Разрешённые папки задаёт только сервер (аргументы запуска). Папки клиента MCP
(roots) не используются никогда.

Исключение для файлов с кредами (kubeconfig, конфиг доступа): такой файл уходит, только если
он лежит в папке secret_outbox и адресат входит в secret_recipients. Оба значения задаёт
сервер (site.conf), не клиент. Остальные проверки (папки, имена, размер) действуют.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from . import redact
from .errors import GuardError

MAX_UPLOAD_BYTES = 2000 * 1024 * 1024
TEXT_SCAN_BYTES = 4 * 1024 * 1024

_DENY_NAMES = [
    ".env", ".env.*", "*.env", "env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", "*.session", "*.session-journal",
    "*kubeconfig*", "*.kubeconfig", "config.json", "*secret*", "*credential*", "*.tfvars",
    "*.tfstate*", "*.ovpn", "*.wg", "wg*.conf", "*.gpg", "*.asc", "*.kdbx", ".netrc", ".pgpass",
    "*.htpasswd", "*token*", "*password*", "*passwd*", "shadow", "*.sqlite", "*.db",
    "authorized_keys", "known_hosts", "*vault*",
]
_TEXT_EXT = {
    ".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv",
    ".log", ".xml", ".html", ".htm", ".js", ".ts", ".py", ".sh", ".sql", ".srt", ".vtt", "",
}


class FileGuard:
    def __init__(self, roots: list[str], download_dir: str | None,
                 secret_outbox: str | None = None, secret_recipients: "list[int] | tuple[int, ...]" = ()):
        self.roots = [Path(r).expanduser().resolve(strict=True) for r in roots if r]
        self.download_dir = Path(download_dir).expanduser().resolve() if download_dir else None
        self.secret_outbox = Path(secret_outbox).expanduser().resolve() if secret_outbox else None
        self.secret_recipients = frozenset(int(x) for x in secret_recipients)

    def in_secret_outbox(self, file_path: str) -> bool:
        if self.secret_outbox is None or not self.secret_recipients or not isinstance(file_path, str):
            return False
        real = Path(os.path.realpath(Path(file_path).expanduser()))
        return real != self.secret_outbox and self.secret_outbox in real.parents

    def secret_allowed(self, real: Path, peer_id: int | None) -> bool:
        """Файл из папки секретов адресату из списка: проверку содержимого пропускаем."""
        return (self.secret_outbox is not None and peer_id is not None
                and int(peer_id) in self.secret_recipients
                and real != self.secret_outbox and self.secret_outbox in real.parents)

    def _within(self, p: Path, root: Path) -> bool:
        return p == root or root in p.parents

    def _check_name_chain(self, p: Path, root: Path) -> None:
        rel = p.relative_to(root)
        for part in rel.parts:
            if part.startswith("."):
                raise GuardError("file_denied", "скрытые файлы и папки отправлять нельзя")
        name = p.name.lower()
        for pat in _DENY_NAMES:
            if fnmatch.fnmatch(name, pat):
                raise GuardError("file_denied", "имя файла похоже на файл с секретами")

    def resolve_upload(self, file_path: str, secret_peer: int | None = None) -> Path:
        if not self.roots:
            raise GuardError("file_tools_disabled", "отправка файлов выключена: нет разрешённых папок")
        if not isinstance(file_path, str) or not file_path.strip():
            raise GuardError("bad_path", "пустой путь")
        if "\x00" in file_path:
            raise GuardError("bad_path", "недопустимый путь")
        raw = Path(file_path).expanduser()
        candidates = [raw] if raw.is_absolute() else [r / raw for r in self.roots]
        for cand in candidates:
            try:
                real = Path(os.path.realpath(cand))
            except OSError:
                continue
            root = next((r for r in self.roots if self._within(real, r)), None)
            if root is None:
                continue
            if not real.is_file():
                raise GuardError("not_found", "файл не найден")
            self._check_name_chain(real, root)
            size = real.stat().st_size
            if size > MAX_UPLOAD_BYTES:
                raise GuardError("too_large", "файл больше 2 ГБ")
            if not self.secret_allowed(real, secret_peer):
                self._scan_content(real)
            return real
        raise GuardError("outside_roots", "файл вне разрешённых папок: " + ", ".join(str(r) for r in self.roots))

    def _scan_content(self, p: Path) -> None:
        if p.suffix.lower() not in _TEXT_EXT:
            return
        with open(p, "rb") as fh:
            chunk = fh.read(TEXT_SCAN_BYTES)
        if b"\x00" in chunk[:8192]:
            return
        text = chunk.decode("utf-8", errors="ignore")
        kinds = redact.detect(text)
        if kinds:
            raise GuardError("file_contains_secret", "в файле найдены креды: " + ", ".join(kinds))

    def download_target(self, requested: str | None, default_name: str) -> Path:
        if self.download_dir is None:
            raise GuardError("download_disabled", "скачивание выключено: нет папки загрузок")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        name = Path(requested).name if requested else default_name
        name = re.sub(r"[^\w.\- ]", "_", name).lstrip(".") or "file"
        if requested and Path(requested).is_absolute():
            real = Path(os.path.realpath(Path(requested).parent))
            if not self._within(real, self.download_dir):
                raise GuardError("outside_download_dir", f"скачивать можно только в {self.download_dir}")
        target = self.download_dir / name
        stem, suffix, i = target.stem, target.suffix, 1
        while target.exists():
            target = self.download_dir / f"{stem}-{i}{suffix}"
            i += 1
        return target
