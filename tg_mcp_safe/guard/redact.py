"""Поиск и маскирование кредов в тексте.

Маскирование нельзя отключить из инструмента: только сменой кода на хосте.
Значение секрета никогда не возвращается — только тип и длина.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

SERVICE_SENDER_IDS = frozenset({777000, 42777})
SERVICE_MASK = "‹скрыто: служебное сообщение Telegram›"

_INVISIBLE = re.compile("[­͏؜ᅟᅠ឴឵᠎​-‏‪-‮⁠-⁯ㅤ︀-️﻿ﾠ]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Кириллица, похожая на латиницу: чтобы «glраt-» с русскими р и а не прошёл мимо.
_HOMOGLYPHS = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p",
    "с": "c", "т": "t", "у": "y", "х": "x", "А": "A", "В": "B", "Е": "E", "К": "K",
    "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X", "і": "i",
    "І": "I", "ј": "j", "ѕ": "s", "ԁ": "d", "ԛ": "q", "ԝ": "w",
})


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int


def _mask(kind: str, value: str) -> str:
    return f"‹скрыто: {kind} len:{len(value)}›"


# (тип, шаблон, номер группы со значением; 0 = всё совпадение)
_PATTERNS: list[tuple[str, re.Pattern, int]] = [
    ("private_key", re.compile(r"-----BEGIN[ A-Z0-9]*PRIVATE KEY-----[\s\S]*?(?:-----END[ A-Z0-9]*PRIVATE KEY-----|\Z)"), 0),
    ("pem", re.compile(r"-----BEGIN [A-Z0-9 ]+-----[\s\S]*?(?:-----END [A-Z0-9 ]+-----|\Z)"), 0),
    ("ssh_key", re.compile(r"\bssh-(?:rsa|ed25519|dss)\s+AAAA[0-9A-Za-z+/=]{40,}"), 0),
    ("telegram_bot_token", re.compile(r"\b\d{6,12}:AA[0-9A-Za-z_-]{30,}"), 0),
    ("anthropic_key", re.compile(r"\bsk-ant-[0-9A-Za-z_-]{20,}"), 0),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|or-v1-)?[0-9A-Za-z_-]{20,}"), 0),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{40,})"), 0),
    ("gitlab_token", re.compile(r"\bgl(?:pat|dt|rt|cbt|ptt|ft|imt|agent|oas|soat)-[0-9A-Za-z_-]{16,}"), 0),
    ("slack_token", re.compile(r"\bxox[abposr]-[0-9A-Za-z-]{10,}"), 0),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), 0),
    ("google_oauth", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"), 0),
    ("yandex_token", re.compile(r"\b(?:y[0-3]_[0-9A-Za-z_-]{30,}|t1\.[0-9A-Za-z_.-]{40,}|AQVN[0-9A-Za-z_-]{30,})"), 0),
    ("vault_token", re.compile(r"\bhv[sbr]\.[0-9A-Za-z_-]{20,}"), 0),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{16,}"), 0),
    ("jwt", re.compile(r"\beyJ[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}"), 0),
    ("url_password", re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:([^\s/@]+)@"), 1),
    ("url_secret", re.compile(r"(?i)[?&#](?:access_token|refresh_token|id_token|token|api_?key|key|secret|sig|signature|password|passwd|auth|code|otp|start|startapp|hash)=([^&#\s]{4,})"), 1),
    # «пароль: x», «token = y», «API key — z»
    ("labeled_secret", re.compile(
        r"(?i)(?<![\w])(?:пароль|парол[ья]|password|passwd|pwd|pass|secret|секрет|token|токен|api[ _-]?key|apikey|access[ _-]?key|secret[ _-]?key|private[ _-]?key|client[ _-]?secret|ключ(?: api)?|pin|пин|cvv|cvc|seed|мнемоник\w*|session[ _-]?string)"
        r"\s*(?:[:=]|—|–|-\s|is\s|это\s)\s*[\"'`]?([^\s\"'`]{3,})"), 1),
    # Коды входа и OTP: число рядом со словом «код».
    ("login_code", re.compile(
        r"(?i)(?<![\w])(?:код\w*|code|otp|2fa|pin|пин|verification|подтверждени\w*|login|вход\w*)[^\d\n]{0,40}?\b(\d(?:[ -]?\d){3,7})\b"), 1),
    ("login_code", re.compile(
        r"(?i)\b(\d(?:[ -]?\d){3,7})\b[^\d\n]{0,25}?(?:—|-|–|:)?\s*(?:ваш|your|это|is)?\s*(?:код\w*|code|otp)"), 1),
    ("card_number", re.compile(r"\b(?:\d[ -]?){15,18}\d\b"), 0),
]

_TOKEN_CANDIDATE = re.compile(r"[0-9A-Za-z_\-+/=.~]{24,}")
# Префиксы, по которым ищем секрет, разбитый пробелами: «g l p a t - …»
_SPLIT_PREFIXES = re.compile(r"(?i)(glpat-|ghp_|github_pat_|sk-ant-|sk-proj-|xox[abp]-|akia|aiza|hvs\.|eyj)")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text)
    return text


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _entropy(s: str) -> float:
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _looks_random(tok: str) -> bool:
    t = tok.strip("=.~-_/+")
    if len(t) < 24:
        return False
    if t.startswith(("http", "www.")) or "/" in t and t.count("/") > 2:
        return False
    classes = sum(bool(re.search(p, t)) for p in (r"[a-z]", r"[A-Z]", r"\d"))
    if classes < 3:
        return False
    return _entropy(t) >= 3.6


# Пароль без подписи: подпись на своей строке, пара «почта + пароль», строка из одного «парольного» слова.
_PW_LABEL_LINE = re.compile(
    r"(?im)^[ \t]*(?:мой |твой |ваш |новый |временный )?(?:пароль|парол[ьяи]|password|passwd|pass|pwd|пасс|креды|доступы?|"
    r"логин[ \t]*(?:/|и)[ \t]*пароль|login[ \t]*/[ \t]*pass(?:word)?|credentials?|creds)[ \t]*[:\-—–]?[ \t]*\n[ \t]*(\S+)")
_EMAIL_PAIR = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+[ \t]*(?:[:/|;,]|[ \t])[ \t]*(\S{6,64})")
_NOT_PASSWORD = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://\S+|www\.\S+|t\.me/\S+|[\w.+-]+@[\w-]+\.\S+|[@#/~.]\S*|\S*[/\\]\S*|"
    r"[A-Z][A-Z0-9]{1,9}-\d+|v?\d+(?:\.\d+)+\S*|[\d\s.,:;+\-()₽$€%№]+|\S+\.[A-Za-z]{2,4})$")


def _password_like(tok: str, standalone_message: bool) -> bool:
    if not 8 <= len(tok) <= 64 or _NOT_PASSWORD.match(tok):
        return False
    core = tok.strip(" *`_~[](){}<>\"'«»“”:;.,!?…")
    if not core or _NOT_PASSWORD.match(core):
        return False
    if not standalone_message:
        # строка кода или разметки внутри длинного текста: скобки, кавычки, присваивание, имена через точку
        if re.search(r"[\[\]{}()=<>`'\"|]", core) or re.fullmatch(r"\w+(?:\.\w+)+", core):
            return False
    # слово, слова через дефис или идентификатор_с_подчёркиваниями — не пароль
    if re.fullmatch(r"[^\W\d_]+(?:[-_][^\W\d_]+)*", core) or re.fullmatch(r"[a-z]+(?:_[a-z0-9]+)+", core):
        return False
    lower, upper = bool(re.search(r"[a-zа-яё]", tok)), bool(re.search(r"[A-ZА-ЯЁ]", tok))
    digit, symbol = bool(re.search(r"\d", tok)), bool(re.search(r"[^\w\s-]", core))
    if not (lower or upper) or not (digit or symbol):
        return False
    classes = lower + upper + digit + symbol
    if standalone_message:
        return classes >= 3 or (symbol and classes >= 2)
    return symbol and classes >= 3


def _password_findings(text: str) -> list[Finding]:
    out: list[Finding] = []
    for m in _PW_LABEL_LINE.finditer(text):
        out.append(Finding("labeled_secret", *m.span(1)))
    for m in _EMAIL_PAIR.finditer(text):
        if _password_like(m.group(1), True) and not _NOT_PASSWORD.match(m.group(1)):
            out.append(Finding("email_password", *m.span(1)))
    lines = text.split("\n")
    standalone = len([l for l in lines if l.strip()]) == 1
    pos = 0
    for line in lines:
        tok = line.strip()
        if tok and " " not in tok and "\t" not in tok and _password_like(tok, standalone):
            start = pos + line.index(tok)
            out.append(Finding("possible_password", start, start + len(tok)))
        pos += len(line) + 1
    return out


def find(text: str) -> list[Finding]:
    """Найти креды в уже нормализованном тексте. Отрезки не пересекаются."""
    found: list[Finding] = []
    probe = text.translate(_HOMOGLYPHS)
    probes = [text] if probe == text else [text, probe]
    for kind, pat, grp in _PATTERNS:
        for m in (m for pr in probes for m in pat.finditer(pr)):
            s, e = m.span(grp)
            if s < 0 or e <= s:
                continue
            if kind == "card_number":
                digits = re.sub(r"\D", "", m.group(0))
                if not (16 <= len(digits) <= 19 and _luhn_ok(digits)):
                    continue
            found.append(Finding(kind, s, e))
    found.extend(_password_findings(text))
    for m in _TOKEN_CANDIDATE.finditer(probe):
        if _looks_random(m.group(0)):
            found.append(Finding("random_token", m.start(), m.end()))
    found.sort(key=lambda f: (f.start, -(f.end - f.start)))
    merged: list[Finding] = []
    for f in found:
        if merged and f.start < merged[-1].end:
            last = merged[-1]
            if f.end > last.end:
                merged[-1] = Finding(last.kind, last.start, f.end)
            continue
        merged.append(f)
    return merged


def _split_secret(text: str) -> bool:
    compact = re.sub(r"[\s ]+", "", text.translate(_HOMOGLYPHS))
    if compact == text:
        return False
    if not _SPLIT_PREFIXES.search(compact):
        return False
    spaced = text.translate(_HOMOGLYPHS)
    return bool(find(compact)) and not _SPLIT_PREFIXES.search(spaced)


def detect(text: str | None) -> list[str]:
    """Типы найденных кредов (без значений)."""
    if not text:
        return []
    t = normalize(text)
    kinds = [f.kind for f in find(t)]
    if _split_secret(t):
        kinds.append("split_secret")
    return sorted(set(kinds))


def redact(text: str | None) -> str | None:
    """Вернуть текст с замаскированными кредами и без невидимых символов."""
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    t = _CONTROL.sub("", normalize(text))
    if _split_secret(t):
        return f"‹скрыто: разбитый секрет len:{len(t)}›"
    findings = find(t)
    if not findings:
        return t
    out, pos = [], 0
    for f in findings:
        out.append(t[pos:f.start])
        out.append(_mask(f.kind, t[f.start:f.end]))
        pos = f.end
    out.append(t[pos:])
    return "".join(out)
