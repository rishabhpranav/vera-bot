# Pranav Yadav DTU Vera Chatbot

A WhatsApp-style growth assistant for small Indian businesses — dental clinics, salons, gyms, restaurants and pharmacies. Vera reads what's happening with a business (its profile numbers, offers, reviews, customers, and events like a festival, a new competitor or a drug recall) and writes the next message: specific, in the right tone, in English or Hinglish, with one clear call to action. It then carries the conversation — handling "yes", questions, auto-replies, "not now", off-topic asks and "stop".

**Built by Pranav Yadav** — B.Tech, Mathematics & Computing, Delhi Technological University (DTU).

## Try it
Open the base URL in a browser for the chat page: pick a business and an event, see Vera's opening message, then reply and watch what it does.

## API
| Endpoint | What it does |
|---|---|
| `POST /v1/context` | Store/update a category, merchant, customer or trigger (versioned, idempotent) |
| `POST /v1/tick` | Decide which proactive messages to send right now |
| `POST /v1/reply` | Handle an incoming reply → `send` / `wait` / `end` |
| `GET /v1/healthz` | Liveness + how much context is loaded |
| `GET /v1/metadata` | Bot info |

## How it works
- **Composer** (`vera/composer.py`) — one handler per event type (research update, compliance deadline, dip/spike in calls, renewal, festival, match day, review pattern, milestone, recall, refill, win-back, competitor, heatwave…). Every number, date and price comes from the stored data; if data is thin it falls back to the business's real numbers vs. local averages instead of making anything up.
- **Tone** — "Dr. Meera" for clinics, first names elsewhere; Hinglish where the owner prefers Hindi; the customer's own language preference for customer messages; banned words (e.g. "guaranteed") are scrubbed and links are never included.
- **Scheduling** (`/v1/tick`) — highest urgency first, one message per business per round, no duplicates, respects consent, opt-outs and "try later".
- **Conversation engine** (`vera/replies.py`) — "yes" → does the work immediately (no extra questions); auto-reply → one nudge, then wait, then a polite goodbye; "stop" / rude → apologise and stop; off-topic (GST, loans…) → polite redirect; questions → answered from the stored facts; every chat ends with "Thanks, and have a nice day! 🙏".
- **Optional LLM polish** (`vera/llm.py`) — set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` to rephrase messages; any output that adds a number not in the data is rejected and the original text is kept.

## Run locally
```bash
pip install -r requirements.txt
uvicorn vera.app:app --port 8080 --workers 1
python tests/harness.py   # loads the sample data and runs every event type
python tests/replay.py    # multi-turn conversation scenarios
```

## Deploy
- **GitHub Codespaces**: Code → Codespaces → Create codespace. The bot starts automatically on port 8080 (set the port to Public).
- **Render / Railway / Fly / Docker**: `render.yaml`, `Procfile` and `Dockerfile` are included. Keep it to one worker — state is held in memory.

## What I'd add next
Real appointment slots and review text from the business, per-customer purchase history, and a small database so state survives restarts.
