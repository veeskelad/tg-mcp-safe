"""Запуск: конфиг → клиенты → инструменты → HTTP или stdio."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

from . import app
from .config import Account, Config, load
from .guard.files import FileGuard

log = logging.getLogger("tg_mcp_safe")


def _proxy_kwargs(a: Account) -> dict:
    p = a.proxy
    if not p:
        return {}
    ptype = (p["TYPE"] or "").lower()
    port = int(p["PORT"])
    if ptype == "mtproxy":
        from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate
        return {"proxy": (p["HOST"], port, p["SECRET"]), "connection": ConnectionTcpMTProxyRandomizedIntermediate}
    if ptype not in {"socks5", "socks4", "http"}:
        raise SystemExit(f"TELEGRAM_PROXY_TYPE: неизвестный тип {ptype}")
    proxy = {"proxy_type": ptype, "addr": p["HOST"], "port": port, "rdns": (p.get("RDNS") or "true").lower() in {"1", "true", "yes", "on"}}
    if p.get("USERNAME"):
        proxy["username"] = p["USERNAME"]
    if p.get("PASSWORD"):
        proxy["password"] = p["PASSWORD"]
    return {"proxy": proxy}


def build_clients(cfg: Config) -> dict[str, TelegramClient]:
    out = {}
    for a in cfg.accounts:
        session = StringSession(a.session_string.reveal()) if a.session_string else a.session_file
        cl = TelegramClient(session, cfg.api_id, cfg.api_hash.reveal(), flood_sleep_threshold=60,
                            receive_updates=True, **cfg.device, **_proxy_kwargs(a))
        out[a.label] = cl
    return out


async def _connect(label: str, cl: TelegramClient) -> bool:
    try:
        await cl.connect()
        if await cl.is_user_authorized():
            return True
        log.warning("аккаунт %s не авторизован, пропущен", label)
    except Exception as exc:  # noqa: BLE001
        log.warning("аккаунт %s: подключение не удалось (%s)", label, type(exc).__name__)
    await cl.disconnect()
    return False


async def _warm(label: str, cl: TelegramClient) -> None:
    try:
        await asyncio.wait_for(cl.get_dialogs(limit=None), timeout=60)
    except Exception as exc:  # noqa: BLE001
        log.warning("аккаунт %s: прогрев диалогов не удался (%s)", label, type(exc).__name__)


async def _main(args: argparse.Namespace) -> None:
    cfg = load()
    app.configure(cfg.secrets, FileGuard(args.roots, args.download_dir))
    built = build_clients(cfg)
    ok = await asyncio.gather(*(_connect(l, c) for l, c in built.items()))
    app.clients.clear()
    app.clients.update({l: c for (l, c), good in zip(built.items(), ok) if good})
    if not app.clients:
        raise SystemExit("ни один аккаунт Telegram не авторизован")
    from . import tools  # noqa: F401  регистрирует инструменты
    from .tools import events
    events.register()
    removed = app.apply_exposed_mode(cfg.exposed)
    log.info("аккаунты: %s; инструментов: %d (скрыто %d)", ", ".join(app.clients), len(app.mcp._tool_manager.list_tools()), len(removed))
    for l, c in app.clients.items():
        asyncio.create_task(_warm(l, c))
    try:
        if args.transport == "http":
            app.mcp.settings.host = args.host
            app.mcp.settings.port = args.port
            # без сессий: перезапуск сервера не ломает клиентов со старым Mcp-Session-Id
            app.mcp.settings.stateless_http = True
            await app.mcp.run_streamable_http_async()
        else:
            await app.mcp.run_stdio_async()
    finally:
        await asyncio.gather(*(c.disconnect() for c in app.clients.values()), return_exceptions=True)


def main() -> None:
    ap = argparse.ArgumentParser(prog="tg-mcp-safe")
    ap.add_argument("roots", nargs="*", help="папки, из которых можно отправлять файлы")
    ap.add_argument("--download-dir", default=os.environ.get("TG_MCP_DOWNLOAD_DIR"))
    ap.add_argument("--transport", default=os.environ.get("MCP_TRANSPORT", "stdio"), choices=["stdio", "http"])
    ap.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8767")))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("telethon", "mcp", "uvicorn.access", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
