# napier-backend

Backend for **Napier's Rod Box** — https://zhangyubooo.github.io/napier/

The project README and the prompt log live with the frontend:
[zhangyubooo.github.io/napier](https://github.com/zhangyubooo/zhangyubooo.github.io/tree/main/napier).

## What it does

A small Flask API. A visitor presents a rod (a card number 1–16); the server asks an LLM to answer
as the 1616 portrait of John Napier, returns the answer as JSON, and stores the rod in that
visitor's box so it is remembered on the next visit.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Wake-up ping (Render's free tier sleeps) |
| `GET /visitor/<uuid>` | `{rods: [{card, inscription, motto}], counts: {"4": 12}}` |
| `POST /ask` `{visitor_id, card}` | `{card, inscription, motto, repeat, count, saved}` |

## Files

| File | Role |
|---|---|
| `app.py` | Endpoints, validation, rate limit, prompt building, Groq call, JSON checking |
| `storage.py` | Database tables and the four operations app.py uses |
| `persona.md` | System prompt: who Napier is, how he speaks, the exact JSON format |
| `facts.md` | The fact sheet Napier may draw on (with sources) |
| `cards.json` | The sixteen rods: question + what each answer should draw on |

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
  Without `DATABASE_URL` Render still works but the SQLite file is wiped on every restart or deploy.

## Secrets

`GROQ_API_KEY` and `DATABASE_URL` live only in Render's environment settings and a local `.env`,
which `.gitignore` excludes. Nothing secret is in this repository or in the frontend.

## Safety

Visitors cannot type: the only inputs are a UUID and an integer 1–16, both validated, so there is
no user text to inject into the prompt. The model's reply must parse as JSON with two text fields;
it is stripped of markdown, length-capped, and the frontend inserts it with `textContent`.
