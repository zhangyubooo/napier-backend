# napier-backend

Backend for **Ask Napier**, the exhibit screens of *Making Calculation Visible* (51-265, fall 2026),
at https://zhangyubooo.github.io/napier/. It started as the backend for **Napier's Rod Box**
(15-113 Project 2); those endpoints still work unchanged.

The project README and the prompt log live with the frontend:
[zhangyubooo.github.io/napier](https://github.com/zhangyubooo/zhangyubooo.github.io/tree/main/napier).

## What it does

A small Flask API. On each exhibit screen a visitor taps Napier's portrait and asks a suggested
question or types their own; the server asks an LLM to answer as John Napier, from a fact sheet and
a description of the station the visitor is standing at, and logs the question anonymously.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Wake-up ping (Render's free tier sleeps) |
| `POST /chat` `{zone, preset, history?}` or `{zone, question, history?}` | `{zone, reply, saved}` |
| `GET /visitor/<uuid>` | Legacy Rod Box: `{rods: [{card, inscription, motto}], counts: {"4": 12}}` |
| `POST /ask` `{visitor_id, card}` | Legacy Rod Box: `{card, inscription, motto, repeat, count, saved}` |

`zone` is `rods`, `promptuary` or `location`; `preset` is a question id from `zones.json` (e.g. `r2`);
`question` is the visitor's own text (cut to 200 characters); `history` is up to 6 earlier messages,
`[{"role": "visitor" | "napier", "text": "..."}]`.

## Files

| File | Role |
|---|---|
| `app.py` | Endpoints, validation, rate limits, prompt building, Groq calls, JSON checking |
| `storage.py` | Database tables and the operations app.py uses (incl. the anonymous `questions` log) |
| `persona_chat.md` | System prompt for the exhibit chat: who Napier is, how he speaks, how to treat typed questions, JSON format |
| `zones.json` | The three stations: what the visitor is looking at, and the suggested questions + what each should draw on. Ids must match the frontend's `js/zones.js` |
| `facts.md` | The fact sheet Napier may draw on (with sources) — shared by both versions |
| `persona.md`, `cards.json` | Legacy Rod Box: persona and the sixteen rods |

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your Groq key in .env
python app.py               # http://127.0.0.1:5002
```

Without `DATABASE_URL` it uses a local SQLite file (`napier.db`, ignored by git).
`MOCK_AI=1` in `.env` returns canned answers without calling Groq, for testing the frontend offline.

## Deploy (Render)

- Build command `pip install -r requirements.txt`, start command `gunicorn app:app`, health check path `/health`.
- Environment variables: `GROQ_API_KEY`, `DATABASE_URL` (a free Neon Postgres URL).
  Optional: `CHAT_RATE_LIMIT_REQUESTS` (default 30 per minute per IP; the exhibit's screens share one IP).
- The new `questions` table is created automatically on the first start after deploying.
  Without `DATABASE_URL` Render still works but the SQLite file is wiped on every restart or deploy.

## Secrets

`GROQ_API_KEY` and `DATABASE_URL` live only in Render's environment settings and a local `.env`,
which `.gitignore` excludes. Nothing secret is in this repository or in the frontend.

## Reading what visitors asked

Every `/chat` question is logged in the `questions` table: zone, preset id (or the typed text),
Napier's reply and the time. There is no visitor id and no IP. Read it in the Neon console's SQL
editor, e.g. `select zone, preset, question, reply, created_at from questions order by id desc;`.
Use it to see what people want to know and to catch any wrong answer.

## Safety

- Suggested questions send only a zone and a preset id, both validated.
- Typed questions are cleaned of control characters, collapsed and cut to 200 characters, and always
  sent as the visitor's message. `persona_chat.md` tells the model to treat them as questions, never as
  instructions, and to steer off-topic or unkind messages back to the exhibit.
- History turns are limited to 6, 500 characters each, roles `visitor`/`napier` only.
- The model's reply must parse as JSON; it is stripped of markdown, capped at 90 words, and the
  frontend inserts it with `textContent`.
- Legacy `/ask` accepts only a UUID and an integer 1–16.
