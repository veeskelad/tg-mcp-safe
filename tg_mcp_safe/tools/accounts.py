"""Подключённые аккаунты: только метки и краткий профиль, без сессий."""

from __future__ import annotations

import asyncio
from typing import Any

from telethon import utils

from .. import app
from ..app import tool


async def _one(label: str, cl: Any) -> dict:
    try:
        me = await asyncio.wait_for(cl.get_me(), timeout=15)
    except Exception:  # noqa: BLE001 — причину не раскрываем, только факт
        return {"account": label, "error": "unavailable"}
    if me is None:
        return {"account": label, "error": "not_authorized"}
    d = {"account": label, "id": me.id, "name": utils.get_display_name(me) or None}
    if me.username:
        d["username"] = me.username
    if me.phone:
        d["phone"] = me.phone
    if me.bot:
        d["bot"] = True
    return d


@tool(readonly=True)
async def list_accounts() -> str:
    """List connected Telegram accounts: label (use as `account`), id, name, username. Returns JSON items."""
    labels = list(app.clients)
    items = await asyncio.gather(*(_one(lab, app.clients[lab]) for lab in labels))
    return {"items": list(items), "next": None}
