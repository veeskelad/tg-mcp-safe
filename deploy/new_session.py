"""Вход в Telegram и новая строка сессии для tg-mcp-safe.

Режимы: по номеру (телефон, код, 2FA) или `--qr` (QR-код сканируется в приложении:
Настройки → Устройства → Подключить устройство; затем 2FA, если включена).

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


def render_qr(url: str, dark_terminal: bool = True) -> str:
    import io

    import qrcode

    q = qrcode.QRCode(border=2)
    q.add_data(url)
    out = io.StringIO()
    q.print_ascii(out=out, invert=dark_terminal)
    return out.getvalue()


async def login_qr(api_id: int, api_hash: str, show: Callable[[str], None], ask_secret: Callable[[str], str],
                   client_factory=None, attempts: int = 6, dark_terminal: bool = True) -> tuple[str, int]:
    from telethon.errors import SessionPasswordNeededError

    if client_factory is None:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        def client_factory():
            return TelegramClient(StringSession(), api_id, api_hash, device_model="tg-mcp-safe")

    cl = client_factory()
    await cl.connect()
    try:
        qr = await cl.qr_login()
        for _ in range(attempts):
            show(render_qr(qr.url, dark_terminal))
            try:
                await qr.wait()
                break
            except asyncio.TimeoutError:
                await qr.recreate()
            except SessionPasswordNeededError:
                await cl.sign_in(password=ask_secret("Пароль 2FA: "))
                break
        else:
            raise SystemExit("QR-код не отсканирован, ничего не меняю")
        me = await cl.get_me()
        session = cl.session.save()
    finally:
        await cl.disconnect()
    return session, me.id


def main() -> int:
    use_qr = "--qr" in sys.argv[1:]
    dark = "--light" not in sys.argv[1:]
    lines, api_id, api_hash = parse_env(sys.stdin.read())
    tty_in = open("/dev/tty")
    tty = open("/dev/tty", "w")

    def ask(prompt: str) -> str:
        tty.write(prompt)
        tty.flush()
        return tty_in.readline().strip()

    def ask_secret(prompt: str) -> str:
        return getpass.getpass(prompt, stream=tty)

    def show(qr_text: str) -> None:
        tty.write("\nОтсканируйте в Telegram: Настройки → Устройства → Подключить устройство."
                  " Код обновляется примерно раз в 30 секунд. Не сканируется на светлом фоне — запустите с --qr --light.\n" + qr_text)
        tty.flush()

    if use_qr:
        session, me_id = asyncio.run(login_qr(api_id, api_hash, show, ask_secret, dark_terminal=dark))
    else:
        session, me_id = asyncio.run(login(api_id, api_hash, ask, ask_secret))
    os.write(3, build_env(lines, session).encode())
    os.close(3)
    tty.write(f"вход выполнен: id {me_id}\n")
    tty.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
