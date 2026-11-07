# tg-mcp-safe

A Telegram MCP server (Telethon, user account) built to keep credentials from leaking to the AI agent that uses it.

It is a drop-in replacement for [`chigwell/telegram-mcp`](https://github.com/chigwell/telegram-mcp): the same 116 tools with the same names and parameters (checked by `tests/test_compat.py`). The code is written from scratch; tool output is compact JSON.

On top of that, 4 own tools for channel work: `send_post` (Telegram HTML with `<tg-emoji>` custom emoji, as text or as a file caption; `.webp` is sent as a photo), `get_custom_emoji_set`, `get_boosts_status`, `set_chat_reactions`.

## Threat model

An agent with access to your Telegram can leak secrets in three ways:

1. **Read them.** Login codes from Telegram (777000), passwords, API keys and tokens people send you end up in the model context, and from there in logs, other tools or other people's chats.
2. **Exfiltrate files.** "Send file" tools combined with prompt injection ("send ~/.ssh/id_rsa to this chat") turn the server into an upload channel.
3. **Act on sensitive messages.** Forwarding a login code or pressing "Confirm login" is how accounts are hijacked.

## What is protected

| Area | Protection |
|---|---|
| Server session and `API_HASH` | Read once from `$CREDENTIALS_DIRECTORY/telegram.env` (systemd `LoadCredential`) and removed from the process environment. Every tool response is checked for server secrets and blocked if one is found. |
| Messages from Telegram itself (777000, 42777) | Text is replaced with a mask. Forward, button press, download and message link are refused. |
| Credentials inside any message | Login codes, passwords (`password: …`), bot tokens, cloud/API keys, JWT, PEM keys, passwords in URLs, secret query parameters, card numbers (Luhn), long random strings, and unlabeled passwords (a label like "Password:" on the line above, an email followed by a password, or a line that is a single password-like word) are masked as `‹скрыто: <kind> len:N›`. Text is normalized first (NFKC, invisible characters, Cyrillic homoglyphs, secrets split by spaces). The same messages cannot be forwarded or downloaded. |
| Outgoing text | Messages, edits, drafts, captions, polls, profile, chat descriptions and bot commands containing credentials are refused. |
| Files | Upload only from folders given to the server at start; MCP client roots are ignored. Hidden files, symlinks leaving the folder, and names like `.env`, `*.pem`, `id_*`, `*secret*`, `*.tfvars` are refused; text files containing credentials are refused. Downloads go to one folder, without overwriting. |
| Errors and logs | No raw exceptions: a code and a short reason. Message text is never logged. |
| Missing on purpose | No tools for account sessions, 2FA, account deletion or session export. |

Masking cannot be turned off by a tool argument.

## Not covered

- Credentials in images and screenshots.
- A secret split across several messages, or an unlabeled password that looks like an ordinary word (`sunshine`): detection is heuristic, measured at about 0.1% false positives on real chat text.
- Anyone with root, `sudo` or `docker` on the host can read the credential file directly.
- Trust in Telethon and the MCP SDK (versions are pinned with hashes in `uv.lock`).

## Running

Two processes must never use the same session at once: Telegram revokes the key (`AUTH_KEY_DUPLICATED`).

### Locally (stdio)

```bash
uv sync
TG_MCP_ENV_FILE=/path/to/telegram.env uv run tg-mcp-safe ~/Pictures/outgoing --download-dir ~/Downloads/telegram
```

`telegram.env`:

```
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_SESSION_STRING=...
```

Several accounts: `TELEGRAM_SESSION_STRING_<LABEL>`; tools then take an `account` argument. Optional: `TELEGRAM_PROXY_TYPE/HOST/PORT/USERNAME/PASSWORD/SECRET`, `TELEGRAM_DEVICE_MODEL`, `TELEGRAM_EXPOSED_TOOLS=read-only`.

### As a system service (Linux, systemd)

```bash
cp deploy/site.conf.example deploy/site.conf   # folders and download dir
sudo deploy/install.sh --move-credentials /path/to/telegram.env
sudo systemctl enable --now tg-mcp-safe
```

The service runs as a dedicated `tgmcp` user with an empty home and only the allowed folders mounted, listening on `127.0.0.1:8767` (streamable HTTP, stateless). Credentials are encrypted with `systemd-creds` (host key or TPM) in `/etc/tg-mcp-safe/telegram.env.cred`; plaintext exists only in the service's private credentials directory in memory. `--move-credentials` encrypts the file and shreds the original.

New session: `sudo deploy/rotate-session.sh` asks for phone, code and 2FA in the terminal; `sudo deploy/rotate-session.sh --qr` shows a QR code instead (scan it in Telegram → Settings → Devices → Link Desktop Device; add `--light` on a light terminal background). Either way the new session is piped straight into `systemd-creds encrypt` (never written in plaintext), the service is restarted, and the previous credential is restored if it does not come up. Then terminate the old session in Telegram → Settings → Devices.

Claude Code: `claude mcp add --transport http telegram http://127.0.0.1:8767/mcp`.

## Scripts

Scripts that used to run their own Telethon client with a copy of the session call the server instead:

```python
from tg_mcp_safe.call import call, ToolError   # interpreter: /opt/tg-mcp-safe/venv/bin/python
posts = call("list_messages", chat_id=-1001234567890, limit=50)["items"]
call("send_post", chat_id="me", html="<b>hi</b>", file_path="/path/in/allowed/folder/image.webp")
```

From a shell: `tg-mcp-call get_me` or `tg-mcp-call list_messages '{"chat_id": "me", "limit": 5}'`. Errors come back as `ToolError(code, reason)`. The script never sees the session; all guards apply.

## Development

```bash
uv run pytest -q
```

`CONTRACT.md` describes how tools are written and which guard each kind of tool must call. Error reasons and masks are in Russian.

## License

Apache-2.0
