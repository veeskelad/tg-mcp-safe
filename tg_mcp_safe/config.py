"""Конфигурация. Секреты читаются один раз и убираются из окружения процесса.

Источник по приоритету:
1. $CREDENTIALS_DIRECTORY/telegram.env (systemd LoadCredential=)
2. файл из TG_MCP_ENV_FILE
3. переменные окружения (для тестов)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_SECRET_PREFIXES = ("TELEGRAM_SESSION_STRING", "TELEGRAM_API_HASH", "TELEGRAM_PROXY_PASSWORD", "TELEGRAM_PROXY_SECRET")


class _Secret:
    __slots__ = ("_v",)

    def __init__(self, v: str):
        self._v = v

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "‹secret›"

    __str__ = __repr__


@dataclass
class Account:
    label: str
    session_string: _Secret | None = None
    session_file: str | None = None
    proxy: dict = field(default_factory=dict)


@dataclass
class Config:
    api_id: int
    api_hash: _Secret
    accounts: list[Account]
    device: dict
    exposed: str
    secrets: list[str]  # для проверки «секрет не ушёл в ответ»

    def __repr__(self) -> str:
        return f"Config(accounts={[a.label for a in self.accounts]})"


def _parse_dotenv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _source() -> dict[str, str]:
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir and (Path(cred_dir) / "telegram.env").is_file():
        return _parse_dotenv((Path(cred_dir) / "telegram.env").read_text())
    env_file = os.environ.get("TG_MCP_ENV_FILE")
    if env_file:
        return _parse_dotenv(Path(env_file).read_text())
    return dict(os.environ)


def _proxy(src: dict[str, str], label: str) -> dict:
    def g(name: str) -> str | None:
        return src.get(f"TELEGRAM_PROXY_{name}_{label.upper()}") or src.get(f"TELEGRAM_PROXY_{name}") or None

    ptype = g("TYPE")
    if not ptype:
        return {}
    return {k: g(k) for k in ("TYPE", "HOST", "PORT", "USERNAME", "PASSWORD", "SECRET", "RDNS")}


def load() -> Config:
    src = {**_source()}
    # Несекретное можно переопределить окружением юнита.
    for k in ("TELEGRAM_EXPOSED_TOOLS", "TELEGRAM_DEVICE_MODEL", "TELEGRAM_SYSTEM_VERSION", "TELEGRAM_APP_VERSION"):
        if os.environ.get(k):
            src[k] = os.environ[k]
    api_id = int(src["TELEGRAM_API_ID"])
    api_hash = src["TELEGRAM_API_HASH"]
    accounts: dict[str, Account] = {}
    for k, v in src.items():
        if not v:
            continue
        if k.startswith("TELEGRAM_SESSION_STRING_"):
            lab = k[len("TELEGRAM_SESSION_STRING_"):].lower()
            accounts[lab] = Account(lab, session_string=_Secret(v))
        elif k.startswith("TELEGRAM_SESSION_NAME_"):
            lab = k[len("TELEGRAM_SESSION_NAME_"):].lower()
            accounts[lab] = Account(lab, session_file=v)
    if "default" not in accounts:
        if src.get("TELEGRAM_SESSION_STRING"):
            accounts["default"] = Account("default", session_string=_Secret(src["TELEGRAM_SESSION_STRING"]))
        elif src.get("TELEGRAM_SESSION_NAME"):
            accounts["default"] = Account("default", session_file=src["TELEGRAM_SESSION_NAME"])
    if not accounts:
        raise SystemExit("Нет сессии Telegram: задайте TELEGRAM_SESSION_STRING")
    for a in accounts.values():
        a.proxy = _proxy(src, a.label)
    secrets = [api_hash] + [a.session_string.reveal() for a in accounts.values() if a.session_string]
    secrets += [a.proxy[k] for a in accounts.values() for k in ("PASSWORD", "SECRET") if a.proxy.get(k)]
    exposed = (src.get("TELEGRAM_EXPOSED_TOOLS") or "all").strip().lower()
    if exposed not in {"all", "read-only"}:
        raise SystemExit("TELEGRAM_EXPOSED_TOOLS: all или read-only")
    device = {kw: src[env] for kw, env in (("device_model", "TELEGRAM_DEVICE_MODEL"), ("system_version", "TELEGRAM_SYSTEM_VERSION"), ("app_version", "TELEGRAM_APP_VERSION")) if src.get(env)}
    for k in list(os.environ):
        if k.startswith(_SECRET_PREFIXES):
            del os.environ[k]
    src.clear()
    return Config(api_id, _Secret(api_hash), list(accounts.values()), device, exposed, [s for s in secrets if len(s) >= 8])
