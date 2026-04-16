# Voice Messages and Images

← [Back to README](../README.md)

## Voice Messages

Send a voice message to any channel. Robyx will:

1. Transcribe it using OpenAI Whisper
2. Show you the transcription (so you can see what was said without replaying)
3. Route the transcribed text to the appropriate agent

If `OPENAI_API_KEY` is not configured, the bot replies with a clear message explaining what's missing and how to fix it. You can also send `OPENAI_API_KEY=sk-...` in chat and Robyx will update `.env` locally and restart.

## Receiving Images

Any workspace agent can send you an image file when you explicitly ask for one. Just ask in plain language — *"mandami il risultato dell'ultima iterazione"*, *"show me the benchmark output"*, *"send me the latest render"* — and the agent will deliver the file to the chat.

Under the hood the agent emits a `[SEND_IMAGE path="..." caption="..."]` tag in its reply; Robyx intercepts it, runs the file through an auto-compression pipeline (JPEG re-encoding with progressive quality and downscale fallback) if it exceeds the platform's upload cap, and uploads it via the native photo API of each messaging platform (`sendPhoto` on Telegram, `channel.send(file=...)` on Discord, `files_upload_v2` on Slack).

**Strict rule enforced in the agent system prompt**: agents only emit `[SEND_IMAGE]` when the user has explicitly asked to see, send, or share an image. They never attach images proactively, as a bonus, or because the conversation merely touched on an image.

**Path allowlist (v0.20.28).** Paths supplied in `[SEND_IMAGE path="..."]` are validated before any filesystem access and must resolve under one of: the agent's own `work_dir`, the bot's `data/` directory, the system temp directory, or — on POSIX — `/tmp`. Requests pointing anywhere else (`/etc/passwd`, a sibling user's home, etc.) are refused with a short error appended to the reply and a warning in `bot.log`. This protects against a prompt-injection attempt coaxing an agent into exfiltrating unrelated files.

---

← [Back to README](../README.md)
