import pytest
from tg_mcp_safe.guard import redact

@pytest.mark.parametrize("text", [
    "Kx7#pL9qZ2",                       # голое сообщение
    "Qwerty2024!",
    "Qwerty2024",
    "Пароль:\nKx7pL9qZ2a",              # подпись на своей строке
    "Доступы\nadmin\nhunter2!",         # подпись + значение со следующей строки
    "логин/пароль\nivan Qx9mPa2k",
    "ivan@mail.ru Kx7#pL9q",            # пара почта + пароль
    "ivan@mail.ru / Qwerty2024",
    "держи вход:\nadmin\nZx!9vB#2qL",   # строка из одного парольного слова
])
def test_password_without_label_masked(text):
    out = redact.redact(text)
    assert "‹скрыто" in out, out

@pytest.mark.parametrize("text", [
    "DEV-5300",
    "Привет, как дела?",
    "Встреча в 19:30",
    "https://t.me/ninjiu",
    "@veskelam",
    "#новости",
    "report-2026.pdf",
    "v1.44.0",
    "+7 999 123-45-67",
    "ок",
    "Спасибо!",
    "завтра\nв 10:00\nу офиса",
    "ivan@mail.ru",
    "напиши на ivan@mail.ru завтра",
    "Смотри src/app.py",
    "2026-11-07",
    "Модели:\nWan2.1-14B\nQwen2.5-VL\nFlux.1-dev\nи ещё строка",
    "Новости\n👉Подписаться\n🔥Обсуждение",
    "Видео https://www.youtube.com/watch?v=dQw4w9WgXcQ_Ab3dEf6hIj9kLm2n и ещё",
    "Вышел Claude Code 2025 года",
])
def test_normal_text_not_masked(text):
    assert redact.redact(text) == text, redact.redact(text)
