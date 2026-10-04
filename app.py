"""
Napier's Rod Box — backend for https://zhangyubooo.github.io/napier/

Flow:
    browser (GitHub Pages)
        GET  /visitor/<id>   which rods are already in this visitor's box?
        POST /ask            visitor presents a rod (a question card)
    --> this Flask app (Render)
        --> Groq API (the LLM writes Napier's inscription + motto as JSON)
        --> database (the rod goes into the visitor's box)

The Groq API key and the database URL live only here, in environment variables.
Visitors never type free text: they can only send a card number 1–16, so there
is nothing to inject into the prompt.
"""

import json
import os
import re
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
import groq
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()  # reads .env on my laptop; does nothing on Render (must run before storage reads DATABASE_URL)

import storage  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")

# MOCK_AI=1 → skip Groq and return a canned answer. Only for testing the
# frontend offline; never set on Render.
MOCK_AI = os.environ.get("MOCK_AI") == "1"

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "ALLOWED_ORIGINS",
        "https://zhangyubooo.github.io,http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if origin.strip()
]

RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", 8))  # each IP may present at most 8 rods...
RATE_LIMIT_WINDOW = 60       # ...per 60 seconds

INSCRIPTION_MAX_WORDS = 60   # persona asks for 45; this is the hard ceiling
MOTTO_MAX_WORDS = 12         # persona asks for 10
MEMORY_CARD = 16             # "What do you remember of me?" — re-answered every time

HERE = Path(__file__).parent
PERSONA = (HERE / "persona.md").read_text(encoding="utf-8")
FACTS = (HERE / "facts.md").read_text(encoding="utf-8")
CARDS = {card["id"]: card for card in json.loads((HERE / "cards.json").read_text(encoding="utf-8"))}

# The system prompt never changes between requests: persona + fact sheet.
SYSTEM_PROMPT = PERSONA + "\n\n# FACT SHEET\n\n" + FACTS


# ---------------------------------------------------------------------------
# 2. App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)   # real visitor IP behind Render's proxy
CORS(app, origins=ALLOWED_ORIGINS)

storage.init_db()
print(f"Database: {'local SQLite file' if storage.USING_SQLITE else 'Postgres (DATABASE_URL)'}")

client = groq.Groq(api_key=GROQ_API_KEY, timeout=30, max_retries=1) if GROQ_API_KEY else None
if client is None and not MOCK_AI:
    print("WARNING: GROQ_API_KEY is not set — /ask will return 500 until it is.")


# ---------------------------------------------------------------------------
# 3. Helpers
# ---------------------------------------------------------------------------

def error(message, status):
    """Every error has the same JSON shape: {"error": "..."}."""
    return jsonify({"error": message}), status


_recent_requests = defaultdict(deque)


def is_rate_limited(ip):
    now = time.time()
    timestamps = _recent_requests[ip]
    while timestamps and now - timestamps[0] > RATE_LIMIT_WINDOW:
        timestamps.popleft()
    if len(timestamps) >= RATE_LIMIT_REQUESTS:
        return True
    timestamps.append(now)
    return False


def valid_visitor_id(value):
    """Visitor ids are UUIDs made by the browser (crypto.randomUUID())."""
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def build_user_message(card, previous_rods):
    """The per-request part of the prompt: which rod, and what's already in the box."""
    if previous_rods:
        memory = "\n".join(
            f'- Rod {CARDS[r["card"]]["numeral"]}: "{CARDS[r["card"]]["question"]}" '
            f'(you answered with the motto: "{r["motto"]}")'
            for r in previous_rods
        )
    else:
        memory = "- none yet — this is the first rod they have presented"

    return (
        f'The visitor presents Rod {card["numeral"]}: "{card["question"]}"\n\n'
        f"What to draw on for this rod: {card['focus']}\n\n"
        f"Rods already in this visitor's box (oldest first):\n{memory}\n\n"
        "If it fits naturally, you may glance back at one earlier rod in a few words. "
        'Reply with the JSON object only: {"inscription": "...", "motto": "..."}'
    )


def clean_text(value, max_words):
    """Strip markdown leftovers and quotes; cut at max_words. Returns '' if unusable."""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"[*_#`]", "", value).strip().strip('"“”').strip()
    text = re.sub(r"\s+", " ", text)
    words = text.split(" ")
    if len(words) > max_words:
        text = " ".join(words[:max_words]).rstrip(",;:") + "…"
    return text


def parse_answer(raw):
    """
    The model is told to answer with JSON, but models sometimes wrap it in
    extra words or code fences. Find the {...} part, parse it, and check that
    both fields are there. Returns (inscription, motto) or None.
    """
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.DOTALL)   # outermost braces
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    inscription = clean_text(data.get("inscription"), INSCRIPTION_MAX_WORDS)
    motto = clean_text(data.get("motto"), MOTTO_MAX_WORDS).rstrip(".")
    if not inscription or not motto:
        return None
    return inscription, motto


def ask_napier(card, previous_rods):
    """Call the LLM. Returns (inscription, motto); raises RuntimeError with a visitor-safe message."""
    if MOCK_AI:
        return (f"(Mock answer for rod {card['numeral']}.) I confess the ink is not yet dry on this reply, "
                "good visitor, but the rod is yours all the same."), "A mock motto for a patient visitor"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(card, previous_rods)},
    ]
    # Two attempts. The first uses Groq's JSON mode, which forces a JSON reply.
    # If that fails (the model's JSON was invalid, or JSON mode is refused),
    # the second asks without it and parse_answer() digs the JSON out of the text.
    for attempt, json_mode in ((1, True), (2, False)):
        options = {"response_format": {"type": "json_object"}} if json_mode else {}
        try:
            result = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=0.6,          # a little personality, still grounded in the fact sheet
                max_completion_tokens=2048,
                **options,
            )
        except groq.APIStatusError as exc:
            print(f"Groq API error {exc.status_code} (attempt {attempt}): {exc.message}")
            if exc.status_code == 429:
                raise RuntimeError("Napier has written too much today — please try again later.")
            if exc.status_code == 400 and attempt == 1:
                continue
            raise RuntimeError("The ink would not flow. Please try again.")
        except Exception as exc:
            print(f"Unexpected error calling Groq: {exc!r}")
            raise RuntimeError("Napier could not be reached. Please try again.")

        raw = result.choices[0].message.content or ""
        parsed = parse_answer(raw)
        if parsed:
            return parsed
        print(f"Attempt {attempt}: unusable reply from model: {raw[:300]!r}")

    raise RuntimeError("Napier's hand slipped. Please present the rod again.")


# ---------------------------------------------------------------------------
# 4. Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return jsonify({
        "name": "Napier's Rod Box backend",
        "endpoints": {
            "GET /health": "wake-up check",
            "GET /visitor/<id>": "rods already in this visitor's box",
            "POST /ask": "present a rod: {visitor_id, card}",
        },
    })


@app.get("/health")
def health():
    """Called as the page loads, so Render's free tier wakes up while the visitor looks around."""
    return jsonify({"status": "ok"})


@app.get("/visitor/<visitor_id>")
def visitor(visitor_id):
    if not valid_visitor_id(visitor_id):
        return error("Unknown visitor.", 400)
    try:
        storage.touch_visitor(visitor_id)
        return jsonify({"rods": storage.get_rods(visitor_id), "counts": storage.card_counts()})
    except Exception as exc:
        print(f"Database error in /visitor: {exc!r}")
        return error("The rod box could not be opened. Please try again.", 503)


@app.post("/ask")
def ask():
    # --- 4a. Rate limit ----------------------------------------------------
    if is_rate_limited(request.remote_addr):
        response, status = error("Slowly, good visitor — wait a minute before the next rod.", 429)
        response.headers["Retry-After"] = str(RATE_LIMIT_WINDOW)
        return response, status

    # --- 4b. Validate input: only a visitor id and a card number are accepted
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error('Send JSON like {"visitor_id": "...", "card": 4}.', 400)
    visitor_id, card_id = data.get("visitor_id"), data.get("card")
    if not valid_visitor_id(visitor_id):
        return error("Unknown visitor.", 400)
    if not isinstance(card_id, int) or isinstance(card_id, bool) or card_id not in CARDS:
        return error("There is no such rod.", 400)
    card = CARDS[card_id]

    if client is None and not MOCK_AI:
        return error("The server is missing its API key.", 500)

    # --- 4c. Memory: what's already in the box? ----------------------------
    try:
        storage.touch_visitor(visitor_id)
        previous = storage.get_rods(visitor_id)
    except Exception as exc:
        print(f"Database error reading rods: {exc!r}")
        return error("The rod box could not be opened. Please try again.", 503)

    # A rod already in the box is not re-asked (saves an AI call) — except
    # card XVI, whose answer depends on everything asked since.
    existing = next((r for r in previous if r["card"] == card_id), None)
    if existing and card_id != MEMORY_CARD:
        return jsonify({**existing, "repeat": True, "count": storage.card_counts().get(card_id, 1)})

    others = [r for r in previous if r["card"] != card_id]

    # --- 4d. Ask Napier ----------------------------------------------------
    try:
        inscription, motto = ask_napier(card, others)
    except RuntimeError as exc:
        return error(str(exc), 502)

    # --- 4e. Put the rod in the box ----------------------------------------
    try:
        storage.save_rod(visitor_id, card_id, inscription, motto)
        count = storage.card_counts().get(card_id, 1)
    except Exception as exc:
        # The answer still reaches the visitor; it just won't be remembered.
        print(f"Database error saving rod: {exc!r}")
        count = None

    return jsonify({
        "card": card_id, "inscription": inscription, "motto": motto,
        "repeat": False, "count": count, "saved": count is not None,
    })


@app.errorhandler(404)
def not_found(_):
    return error("Not found.", 404)


@app.errorhandler(405)
def method_not_allowed(_):
    return error("Method not allowed.", 405)


# ---------------------------------------------------------------------------
# 5. Local development: `python app.py` → http://127.0.0.1:5002
#    (5001 is Ask Yubo's backend, so both can run at once.)
#    On Render, `gunicorn app:app` runs instead and this block is skipped.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5002)), debug=True)
