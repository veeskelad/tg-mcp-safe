"""Ядро: экземпляр MCP, клиенты, обёртка инструментов.

Каждый ответ инструмента проходит одну точку выхода `finalize`: маскирование
кредов во всех строках, проверка, что секреты сервера не попали в ответ,
единый формат ошибок без сырых исключений.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import traceback
from datetime import date, datetime
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import ContentBlock, TextContent, ToolAnnotations
from telethon import TelegramClient, errors as tg_errors

from .guard import redact
from .guard.errors import GuardError
from .guard.files import FileGuard

log = logging.getLogger("tg_mcp_safe")

mcp = FastMCP("telegram")

clients: dict[str, TelegramClient] = {}
_secrets: list[str] = []
files: FileGuard = FileGuard([], None)
READONLY_TOOLS: set[str] = set()


def configure(secrets: list[str], file_guard: FileGuard) -> None:
    global files
    _secrets[:] = secrets
    files = file_guard


def client(account: str | None = None) -> TelegramClient:
    if not clients:
        raise GuardError("no_accounts", "нет подключённых аккаунтов")
    if account is None:
        if len(clients) == 1:
            return next(iter(clients.values()))
        raise GuardError("account_required", "укажите account: " + ", ".join(clients))
    lab = account.lower()
    if lab not in clients:
        raise GuardError("unknown_account", "неизвестный account, доступны: " + ", ".join(clients))
    return clients[lab]


# ---------------------------------------------------------------- вывод

def _default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, bytes):
        return f"<bytes len:{len(o)}>"
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def _deep_redact(o: Any) -> Any:
    if isinstance(o, str):
        return redact.redact(o)
    if isinstance(o, dict):
        return {str(k): _deep_redact(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_deep_redact(v) for v in o]
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return o


def finalize(result: Any) -> str:
    if isinstance(result, str):
        text = redact.redact(result) or ""
    else:
        text = json.dumps(_deep_redact(result), ensure_ascii=False, default=_default, separators=(",", ":"))
    for s in _secrets:
        if s and s in text:
            log.error("canary: секрет сервера в ответе инструмента, ответ заменён")
            return json.dumps({"error": {"code": "blocked", "reason": "ответ содержал секрет сервера и заблокирован"}}, ensure_ascii=False)
    return text


class Rich:
    """Ответ с бинарными блоками (картинка, base64 файла) рядом с JSON-метаданными.

    Метаданные проходят обычный `finalize`. Блоки не маскируются (маска испортит
    байты), поэтому инструмент обязан проверить содержимое до того, как вернуть Rich;
    обёртка сверяет блоки только с секретами сервера.
    """

    def __init__(self, meta: dict, blocks: list[ContentBlock], raw: bytes = b""):
        self.meta = meta
        self.blocks = blocks
        self.raw = raw


def _blocks_leak(r: Rich) -> bool:
    for s in _secrets:
        if not s:
            continue
        if r.raw and s.encode() in r.raw:
            return True
        for b in r.blocks:
            if isinstance(b, TextContent) and s in b.text:
                return True
    return False


def finalize_rich(result: Any) -> list[ContentBlock]:
    if not isinstance(result, Rich):
        return [TextContent(type="text", text=finalize(result))]
    if _blocks_leak(result):
        log.error("canary: секрет сервера в файле, ответ заменён")
        return [TextContent(type="text", text=finalize(error("blocked", "файл содержал секрет сервера и заблокирован")))]
    return [TextContent(type="text", text=finalize(result.meta)), *result.blocks]


def error(code: str, reason: str, **extra: Any) -> dict:
    return {"error": {"code": code, "reason": reason, **extra}}


def _where(exc: BaseException) -> str:
    tb = traceback.extract_tb(exc.__traceback__)
    if not tb:
        return "?"
    fr = tb[-1]
    return f"{fr.filename.rsplit('/', 1)[-1]}:{fr.lineno}"


def map_exception(name: str, exc: BaseException) -> dict:
    if isinstance(exc, GuardError):
        return error(exc.code, exc.reason)
    if isinstance(exc, tg_errors.FloodWaitError):
        return error("flood_wait", f"Telegram просит подождать {exc.seconds} с", retry_after=exc.seconds)
    if isinstance(exc, tg_errors.RPCError):
        # message у RPCError — константа сервера Telegram (CHAT_ADMIN_REQUIRED и т.п.)
        return error("telegram", str(getattr(exc, "message", "") or type(exc).__name__)[:120], type=type(exc).__name__)
    if isinstance(exc, (ValueError, TypeError, KeyError, IndexError)):
        msg = redact.redact(str(exc)) or ""
        return error("bad_request", msg[:200], type=type(exc).__name__)
    if isinstance(exc, asyncio.TimeoutError):
        return error("timeout", "Telegram не ответил вовремя")
    log.error("tool %s: %s at %s", name, type(exc).__name__, _where(exc))
    return error("internal", type(exc).__name__)


# ---------------------------------------------------------------- регистрация

def tool(*, readonly: bool = False, destructive: bool = False, idempotent: bool | None = None,
         title: str | None = None, open_world: bool = True, fan_out: bool = True,
         rich: bool = False) -> Callable:
    """Регистрирует инструмент. Функция возвращает dict/list/str; строки маскируются.

    rich=True: функция может вернуть `Rich` (JSON + блоки картинки/файла); инструмент
    отвечает списком блоков MCP без структурированной схемы и без раздачи по аккаунтам.
    """

    def deco(fn: Callable) -> Callable:
        sig = inspect.signature(fn)
        has_account = "account" in sig.parameters

        async def run_one(args: tuple, kwargs: dict) -> Any:
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                return map_exception(fn.__name__, exc)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            bound = sig.bind_partial(*args, **kwargs)
            account = bound.arguments.get("account") if has_account else None
            if rich:
                if has_account and account is None and len(clients) > 1:
                    return finalize_rich(error("account_required", "укажите account: " + ", ".join(clients)))
                return finalize_rich(await run_one(args, kwargs))
            if has_account and account is None and len(clients) > 1:
                if readonly and fan_out:
                    labels = list(clients)
                    res = await asyncio.gather(*(run_one(args, {**kwargs, "account": lab}) for lab in labels))
                    return finalize({lab: r for lab, r in zip(labels, res)})
                return finalize(error("account_required", "укажите account: " + ", ".join(clients)))
            return finalize(await run_one(args, kwargs))

        ann = ToolAnnotations(
            title=title,
            readOnlyHint=readonly,
            destructiveHint=(destructive if not readonly else None),
            idempotentHint=idempotent,
            openWorldHint=open_world,
        )
        doc = inspect.getdoc(fn) or ""
        mcp.tool(name=fn.__name__, description=doc, annotations=ann,
                 structured_output=False if rich else None)(wrapper)
        if readonly:
            READONLY_TOOLS.add(fn.__name__)
        return wrapper

    return deco


def apply_exposed_mode(mode: str) -> list[str]:
    if mode != "read-only":
        return []
    removed = []
    for t in list(mcp._tool_manager.list_tools()):
        if t.name not in READONLY_TOOLS:
            mcp._tool_manager.remove_tool(t.name)
            removed.append(t.name)
    return removed
