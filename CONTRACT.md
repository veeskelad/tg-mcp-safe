# Контракт для модулей инструментов

Сервер `tg-mcp-safe` — своя замена `chigwell/telegram-mcp` с защитой от утечки кредов.

## Совместимость
- `tests/fixtures/tools.json` — эталон: для каждого инструмента имя, имена параметров, типы, значения по умолчанию, обязательность и `readOnlyHint` должны совпасть **точно** (тест `tests/test_compat.py` это проверяет).
- Набор инструментов модуля = инструменты одноимённого модуля `telegram_mcp/tools/` в upstream `chigwell/telegram-mcp`.
- Старый код — только справка по поведению. Пишем своё, не копируем блоки, не импортируем из него.

## Как писать инструмент
```python
from ..app import tool, client, error
from .. import app            # app.files — FileGuard (берите через app.files, он подменяется при старте)
from ..serialize import message, entity, dialog, resolve
from ..guard.messages import check_outgoing, deny_if_sensitive, is_service_sender
from ..guard.errors import GuardError

@tool(readonly=True)
async def get_chat(chat_id: int | str, account: str = None) -> str:
    """Get chat info by id or @username. Returns JSON: id, type, name, username, members."""
    cl = client(account)
    e = await resolve(cl, chat_id)
    return entity(e)
```
- `account: str = None` (не `str | None`: эталон без null в anyOf). Тип параметра и default — как в эталоне (`int | str` → смотрите `anyOf` в схеме; `Optional[X] = None` и т.п.). Сигнатура функции и есть схема.
- Возвращать dict/list/str. Обёртка `tool` сама сериализует в компактный JSON, маскирует строки, проверяет, что секреты сервера не утекли, и превращает исключения в `{"error":{code,reason}}`. Ловить исключения ради красивой ошибки не нужно; для отказа — `raise GuardError(code, reason)`.
- Docstring = описание для модели: 1–3 строки по-английски, что делает и что возвращает. Без простыней.
- Агентно-удобный вывод: компактные поля, id везде, пагинация через параметры из эталона, списки — `{"items":[...], "next": <курсор или null>}` где есть страницы. Действия возвращают `{"ok": true, ...}` с id созданного/изменённого.
- `destructive=True` для удаления, бана, выхода, очистки истории.

## Правила безопасности (обязательно)
1. Любой исходящий текст (сообщение, ответ, правка, черновик, подпись, опрос, профиль, описание чата, команды бота, имя контакта) — `check_outgoing(...)` до вызова Telegram.
2. Действие над существующим сообщением — переслать, нажать кнопку, скачать медиа, ссылка на сообщение — сначала получить сообщение(я) через `cl.get_messages(entity, ids=...)` и вызвать `deny_if_sensitive(msgs, "<действие>")`. Для служебного чата 777000/42777 эти действия запрещены всегда.
3. Файлы: путь на отправку — только `app.files.resolve_upload(path)`; путь на скачивание — только `app.files.download_target(requested, default_name)`. Никаких своих `open()` на пути от агента.
4. Сообщения отдавать только через `serialize.message` / `serialize.dialog`; сущности — через `serialize.entity` (можно дополнять полями). Никогда: `str(obj)`, `obj.to_dict()`, `obj.stringify()`, сырые Telethon-объекты, `client.session`, `StringSession.save()`.
5. Не логировать текст сообщений, аргументы и исключения целиком.
6. Никаких новых инструментов сверх эталона (особенно сессии аккаунта, 2FA, удаление аккаунта, экспорт сессии).

## Доступ к клиенту
- `client(account)` — TelegramClient; `app.clients` — все.
- `flood_sleep_threshold=60` уже стоит: короткие FloodWait Telethon переждёт сам.
- Сеть в тестах не трогать. Сервер не запускать.
