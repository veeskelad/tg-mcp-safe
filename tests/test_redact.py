import pytest
from tg_mcp_safe.guard import redact

# фейковые значения, собраны из частей, чтобы не светиться в git-сканерах
GL = "glpat-" + "Ab3dEf6hIj9kLm2nOp5q"
BOT = "1234567890:" + "AAH" + "x9Kq2LmN8pQr4StUv6WxYz0AbCdEfGhIjK"
JWT = "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"

@pytest.mark.parametrize("text", [
    f"мой токен {GL} держи",
    f"бот: {BOT}",
    f"Authorization: Bearer {JWT}",
    "Код для входа в Telegram: 52814. Не давайте его никому",
    "Login code: 12345. Do not give this code to anyone",
    "48213 — ваш код подтверждения",
    "пароль: Qwerty!2024",
    "password = hunter22",
    "postgres://app:S3cr3tPass@db:5432/x",
    "https://example.com/cb?access_token=abcd1234efgh",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "sk-ant-" + "api03-abcdefghijklmnopqrstuvwxyz",
    "карта 4111 1111 1111 1111",
    "1BVtsOK8Bu" + "zWm9fQ3pL7xR2nT5vY8cA1dE4gH6jK9mN0qS3uW5zB7",
])
def test_masked(text):
    out = redact.redact(text)
    assert "‹скрыто" in out
    assert redact.detect(text)

@pytest.mark.parametrize("text", [
    "Привет! Встречаемся в 19:30 у метро",
    "В 2024 году выручка 15000 рублей",
    "Смотри https://github.com/chigwell/telegram-mcp/pull/157",
    "commit 48a6237 Merge pull request",
    "номер заказа 5521, позвони +7 999 123-45-67",
    "это не пароль, а пасспорт",
    "sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
])
def test_not_masked(text):
    assert redact.redact(text) == text, redact.redact(text)

def test_split_secret():
    spaced = " ".join(GL)
    assert redact.detect(spaced)
    assert "‹скрыто" in redact.redact(spaced)

def test_homoglyph():
    # кириллические р и а внутри glpat-
    t = "glраt-" + "Ab3dEf6hIj9kLm2nOp5q"
    assert redact.detect(t)

def test_invisible_chars():
    t = GL[:5] + "​" + GL[5:]
    assert redact.detect(t)
