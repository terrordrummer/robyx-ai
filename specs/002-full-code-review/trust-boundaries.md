# Trust-Boundary Map — Pass 2 T063

**Date**: 2026-04-16
**Source**: `research.md` §R1 expanded with concrete file:line evidence from
reading the current codebase.

Every row describes one input that crosses the platform→bot trust boundary,
what validates it today, and what Phase 9 (P2-SEC) must verify or fix.

## Legend

- **Status**: `validated` (check in place and sufficient) · `partial` (check
  exists but gap identified) · `gap` (no check).

---

## Telegram — `bot/messaging/telegram.py`

| # | Input | Where enters | Downstream sink | Current validation | Status | Phase 9 action |
|---|-------|--------------|-----------------|--------------------|--------|----------------|
| TG-1 | `update.message.text` | handler entry | handler → AI subprocess (stdin) | PTB truncates at platform layer (~4096 chars); no explicit handler check | partial | T065: confirm ai_invoke stdin handling bounds input |
| TG-2 | `update.message.voice.file_id` | `download_voice(file_id)` at `telegram.py:175` | `voice_file.download_to_drive(tmp_path)` | PTB library handles size/timeout defaults; no explicit cap in our code | partial | T070: add explicit max-size assertion before download (e.g. 20 MB cap) and suffix whitelist |
| TG-3 | `update.message.document.file_name` | handler (via PTB) | any path concatenation | No explicit check | gap if used | T065 + T070: if filename is used to build a filesystem path anywhere, must normalize + validate (no `..`, no `/`, no absolute path) |
| TG-4 | `update.message.photo[*]` | `media.py` `load_image` | Pillow `Image.open` | `media.py` wraps Pillow; Pillow's default MAX_IMAGE_PIXELS (89 478 485 px) raises `DecompressionBombWarning`, not error | partial | T075: upgrade to DecompressionBombError (`Image.MAX_IMAGE_PIXELS = ...; Image.warnings.simplefilter("error", DecompressionBombWarning)`) and cap file size pre-Pillow |
| TG-5 | `chat.id` / `thread_id` | routing | workspace/topic lookup | `authorization.py` checks `OWNER_ID` | validated | T065: verify auth happens BEFORE any state mutation (not after) |
| TG-6 | Markdown in outgoing messages (re-Pass1 F12) | `send_to_channel` | Telegram API | Unconditional Markdown mode | deferred in Pass 1 | T109: decide fix or document as accepted behavior |

## Discord — `bot/messaging/discord.py`

| # | Input | Where enters | Downstream sink | Current validation | Status | Phase 9 action |
|---|-------|--------------|-----------------|--------------------|--------|----------------|
| DC-1 | `message.content` | handler | AI subprocess | None | partial | T065: same as TG-1 |
| DC-2 | Voice attachment URL (`file_id`) | `download_voice` at `discord.py:147` | `aiohttp.get(file_id)` | **HTTPS-only + hostname suffix allow-list** (`.discordapp.com` / `.discord.com`) at lines 159–163 — Pass 1 S3 fix | validated | Extend model: all attachment downloads should share this validator, not only voice |
| DC-3 | `resp.read()` during voice download | `discord.py:170-172` | `open(tmp_path, "wb").write(data)` | No streaming / no size cap — reads entire body into memory | **gap** | T071: stream with max-bytes guard; reject > 25 MB |
| DC-4 | Non-voice attachment URLs | `discord.py:135` (`discord.File(prepared)`) and any future download path | varies | Same allow-list NOT applied to non-voice paths | gap | T071: hoist allow-list into `_validate_discord_url(url)` helper and call from every HTTP fetch |
| DC-5 | Thread/channel IDs for routing | handler | workspace lookup | Authorization check | partial | T065/T071: race — thread may be created mid-handler; auth must re-check after resolution |

## Slack — `bot/messaging/slack.py`

| # | Input | Where enters | Downstream sink | Current validation | Status | Phase 9 action |
|---|-------|--------------|-----------------|--------------------|--------|----------------|
| SL-1 | `event.text` | handler | AI subprocess | None | partial | T065 |
| SL-2 | `file_id` = `url_private_download` URL | `download_voice` at `slack.py:172` | `httpx.get(file_id, headers={Authorization: Bearer <bot_token>}, follow_redirects=True)` | **None** — URL is trusted from event without host validation | **gap (high)** | T072: add `_validate_slack_url(url)` → must be `https://files.slack.com/...` or equivalent; **`follow_redirects=True` is dangerous with a bearer token** — a redirect to an attacker domain exfiltrates the bot token. Must either set `follow_redirects=False` or re-validate target after each redirect |
| SL-3 | `event_id` for dedup | Socket Mode handler | in-memory dedup store | Used for dedup | partial | T072: verify dedup store is size-bounded (LRU with max entries) — unbounded grows memory indefinitely |
| SL-4 | Outgoing error-reply strings | exception paths | user message | Varies | partial | T072: ensure `str(e)` or traceback never includes bot token in error text |

## Cross-adapter residual

| # | Concern | Status | Phase 9 action |
|---|---------|--------|----------------|
| X-1 | `ai_invoke.py` subprocess env | partial | T066: scrub env of host secrets the AI CLI doesn't need (keep PATH, HOME; drop SSH_AUTH_SOCK, GH_TOKEN unless required) |
| X-2 | Updater tarball handling | validated in Pass 1 F01 | T067: re-verify symlink/hardlink rejection still in place; add size cap on individual members |
| X-3 | Config hot-edit during AI call | gap | T074: guard `.env` reload behind a mutex that blocks during active AI invocation |
| X-4 | PID-lock race | partial | T068: verify `bot.py` uses POSIX `fcntl.flock` or similar; a pure `os.path.exists(bot.pid)` check is race-prone |

---

## Summary of gaps (high-severity candidates for Phase 9)

1. **SL-2 follow-redirects with bearer token** — token-exfiltration risk.
   Expected finding ID: **P2-10 (High, slack.py)**.
2. **DC-3 unbounded `resp.read()` in voice download** — memory DoS.
   Expected finding ID: **P2-11 (Med, discord.py)**.
3. **DC-4 attachment allow-list not generalized** — SSRF residual.
   Expected finding ID: **P2-12 (Med, discord.py)**.
4. **X-3 `.env` hot-reload during AI invocation** — secret rotation race.
   Expected finding ID: **P2-13 (Med, config_updates.py)**.

Other rows are to be verified (not pre-emptively fixed) during Phase 9.
