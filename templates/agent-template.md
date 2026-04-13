# [Agent Name]

Brief description of what this agent does.

---

## Telegram Configuration

Read your bot token, chat ID, and thread ID from the project's `.env` file and `tasks.md`.

To send a message to your topic:
```bash
curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  -d chat_id=${CHAT_ID} \
  -d message_thread_id=${THREAD_ID} \
  -d parse_mode=HTML \
  -d text="<message>"
```

---

## Instructions

1. Step one — what to do first
2. Step two — next action
3. Step three — ...

---

## Data Storage

Use `data/<task-name>/` for any files this agent needs to persist between runs:
- `data/<task-name>/output.log` — last execution output
- `data/<task-name>/results.md` — accumulated results (optional)

---

## Rules

- List constraints and boundaries
- e.g., "Never modify files outside data/<task-name>/"
- e.g., "Always verify data before alerting"
