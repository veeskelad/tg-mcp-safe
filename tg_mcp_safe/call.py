"""Клиент для скриптов: вызвать инструмент tg-mcp-safe по HTTP и получить dict.

    from tg_mcp_safe.call import call, ToolError
    me = call("get_me")
    msgs = call("list_messages", chat_id=-100123, limit=50)["items"]

Сессия Telegram скрипту не нужна и недоступна: всё идёт через сервер со стражами.
Командная строка: tg-mcp-call <tool> '<json-аргументы>'.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8767/mcp"


class ToolError(Exception):
    def __init__(self, tool: str, code: str, reason: str):
        super().__init__(f"{tool}: {code}: {reason}")
        self.tool, self.code, self.reason = tool, code, reason


def parse_result(tool: str, text: str) -> Any:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if text.startswith("Error executing tool"):
            raise ToolError(tool, "bad_request", text[:300]) from None
        return text
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        e = data["error"]
        raise ToolError(tool, str(e.get("code", "error")), str(e.get("reason", "")))
    return data


async def acall(tool: str, url: str | None = None, timeout: float = 300, **args: Any) -> Any:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    args = {k: v for k, v in args.items() if v is not None}
    async with streamablehttp_client(url or os.environ.get("TG_MCP_URL", DEFAULT_URL), timeout=30,
                                     sse_read_timeout=timeout) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
    text = "".join(getattr(c, "text", "") for c in res.content)
    return parse_result(tool, text)


def call(tool: str, **args: Any) -> Any:
    return asyncio.run(acall(tool, **args))


def main() -> int:
    if len(sys.argv) < 2:
        print("использование: tg-mcp-call <tool> '<json>'", file=sys.stderr)
        return 2
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    try:
        print(json.dumps(call(sys.argv[1], **args), ensure_ascii=False))
        return 0
    except ToolError as e:
        print(json.dumps({"error": {"code": e.code, "reason": e.reason}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
