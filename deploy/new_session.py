"""Вход в Telegram и новая строка сессии для tg-mcp-safe.

stdin: текущий telegram.env (расшифрованный systemd-creds); вопросы задаются через /dev/tty;
результат (новый telegram.env) пишется в fd 3 и дальше сразу в systemd-creds encrypt.
Строка сессии не печатается и не пишется на диск.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from typing import Callable


def parse_env(text: str) -> tuple[list[str], int, str]:
    lines = text.splitlines()
    env = dict(l.split("=", 1) for l in lines if "=" in l and not l.lstrip().startswith("#"))
    return lines, int(env["TELEGRAM_API_ID"]), env["TELEGRAM_API_HASH"].strip("'\"")


def build_env(lines: list[str], session: str) -> str:
    keep = [l for l in lines if not l.startswith("TELEGRAM_SESSION_STRING=")]
    return "\n".join(keep + ["TELEGRAM_SESSION_STRING=" + session]) + "\n"


async def login(api_id: int, api_hash: str, ask: Callable[[str], str], ask_secret: Callable[[str], str],
                client_factory=None) -> tuple[str, int]:
    if client_factory is None:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        def client_factory():
            return TelegramClient(StringSession(), api_id, api_hash, device_model="tg-mcp-safe")

    cl = client_factory()
    await cl.start(phone=lambda: ask("Телефон: "), code_callback=lambda: ask("Код из Telegram: "),
                   password=lambda: ask_secret("Пароль 2FA (если есть): "))
    me = await cl.get_me()
    session = cl.session.save()
    await cl.disconnect()
    return session, me.id


def main() -> int:
    lines, api_id, api_hash = parse_env(sys.stdin.read())
    tty_in = open("/dev/tty")
    tty = open("/dev/tty", "w")

    def ask(prompt: str) -> str:
        tty.write(prompt)
        tty.flush()
        return tty_in.readline().strip()

    def ask_secret(prompt: str) -> str:
        return getpass.getpass(prompt, stream=tty)

    session, me_id = asyncio.run(login(api_id, api_hash, ask, ask_secret))
    os.write(3, build_env(lines, session).encode())
    os.close(3)
    tty.write(f"вход выполнен: id {me_id}\n")
    tty.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
