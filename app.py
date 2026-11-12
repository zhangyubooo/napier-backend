"""
AI Napier backend for https://zhangyubooo.github.io/napier/

Current use (51-265 exhibit, Nov 2026): the "Ask Napier" panel on the three
exhibit screens (rods, promptuary, location arithmetic).
    browser (GitHub Pages, ?zone=rods|promptuary|location)
        POST /chat           a preset question id, or a visitor's own question
    --> this Flask app (Render)
        --> Groq API (the LLM answers as Napier, from persona_chat.md + facts.md + zones.json)
        --> database (the question and answer are logged anonymously)

Legacy (15-113 Project 2, "Napier's Rod Box"): GET /visitor/<id> and POST /ask
still work exactly as submitted; the current frontend no longer calls them.

The Groq API key and the database URL live only here, in environment variables.
Visitors' own questions are length-capped, cleaned of control characters and
always sent as a visitor message (never as instructions).
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

# --- Exhibit chat (/chat) ---------------------------------------------------
CHAT_PERSONA = (HERE / "persona_chat.md").read_text(encoding="utf-8")
ZONES = {zone["id"]: zone for zone in json.loads((HERE / "zones.json").read_text(encoding="utf-8"))}
PRESETS = {p["id"]: (zone_id, p) for zone_id, zone in ZONES.items() for p in zone["presets"]}

# Three kiosks in one building share one public IP, so this limit is looser than /ask's.
CHAT_RATE_LIMIT_REQUESTS = int(os.environ.get("CHAT_RATE_LIMIT_REQUESTS", 30))
QUESTION_MAX_CHARS = 200     # a visitor's own question
HISTORY_MAX_TURNS = 6        # earlier messages in this conversation sent back for context
HISTORY_MAX_CHARS = 500      # per earlier message
REPLY_MAX_WORDS = 90         # persona asks for 70; this is the hard ceiling


def chat_system_prompt(zone, page=None):
    prompt = (CHAT_PERSONA + "\n\n# FACT SHEET\n\n" + FACTS
              + "\n\n# WHERE THE VISITOR IS STANDING\n\n" + zone["name"] + ". " + zone["where"])
    if page:
        prompt += (f"\n\n# THE PAGE ON THE SCREEN RIGHT NOW\n\nPage {page['page']} of {len(zone['pages'])}: "
                   f"{page['title']}. What the page shows: {page['screen']}")
    return prompt


def zone_page(zone, number):
    """The page the visitor is looking at (1-based), or None."""
    pages = zone.get("pages") or []
    if isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= len(pages):
        return pages[number - 1]
    return None


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
_recent_chats = defaultdict(deque)


def is_rate_limited(ip, log=_recent_requests, limit=RATE_LIMIT_REQUESTS):
    now = time.time()
    timestamps = log[ip]
    while timestamps and now - timestamps[0] > RATE_LIMIT_WINDOW:
        timestamps.popleft()
    if len(timestamps) >= limit:
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
            "POST /chat": "exhibit screens: {zone, page, preset} or {zone, page, question, history}",
            "POST /ask": "legacy Rod Box: present a rod: {visitor_id, card}",
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


# ---------------------------------------------------------------------------
# 5. Exhibit chat — "Ask Napier" on the three exhibit screens
# ---------------------------------------------------------------------------

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_question(value, max_chars):
    """A visitor's typed text: strip control characters, collapse spaces, cap length."""
    if not isinstance(value, str):
        return ""
    text = _CONTROL_CHARS.sub(" ", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars].strip()


def clean_history(value):
    """Earlier turns sent back by the page: [{"role": "visitor"|"napier", "text": "..."}]."""
    if not isinstance(value, list):
        return []
    turns = []
    for item in value[-HISTORY_MAX_TURNS:]:
        if not isinstance(item, dict) or item.get("role") not in ("visitor", "napier"):
            continue
        text = clean_question(item.get("text"), HISTORY_MAX_CHARS)
        if text:
            turns.append({"role": "user" if item["role"] == "visitor" else "assistant", "content": text})
    return turns


def parse_reply(raw):
    """Find {"reply": "..."} in the model's output. Returns the cleaned reply or None."""
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return clean_text(data.get("reply"), REPLY_MAX_WORDS) or None


def chat_napier(zone, question, focus, history, page=None):
    """Call the LLM for the exhibit chat. Returns the reply; raises RuntimeError with a visitor-safe message."""
    if MOCK_AI:
        return (f"(Mock answer · {zone['name']}.) You asked: {question} "
                "I confess the ink is not yet dry on this reply, good visitor.")

    user_message = f"The visitor asks: {question}"
    if focus:
        user_message += f"\n\n(Exhibit note, not from the visitor — what to draw on: {focus})"
    user_message += '\n\nReply with the JSON object only: {"reply": "..."}'

    messages = [{"role": "system", "content": chat_system_prompt(zone, page)}, *history,
                {"role": "user", "content": user_message}]

    for attempt, json_mode in ((1, True), (2, False)):
        options = {"response_format": {"type": "json_object"}} if json_mode else {}
        try:
            result = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=0.5,
                max_completion_tokens=2048,
                **options,
            )
        except groq.APIStatusError as exc:
            print(f"Groq API error {exc.status_code} (chat attempt {attempt}): {exc.message}")
            if exc.status_code == 429:
                raise RuntimeError("Napier has spoken too much today. Please try again later.")
            if exc.status_code == 400 and attempt == 1:
                continue
            raise RuntimeError("The ink would not flow. Please try again.")
        except Exception as exc:
            print(f"Unexpected error calling Groq (chat): {exc!r}")
            raise RuntimeError("Napier could not be reached. Please try again.")

        reply = parse_reply(result.choices[0].message.content or "")
        if reply:
            return reply
        print(f"Chat attempt {attempt}: unusable reply from model.")

    raise RuntimeError("Napier lost his thread. Please ask again.")


@app.post("/chat")
def chat():
    if is_rate_limited(request.remote_addr, _recent_chats, CHAT_RATE_LIMIT_REQUESTS):
        response, status = error("Slowly, good visitor. Wait a moment before the next question.", 429)
        response.headers["Retry-After"] = str(RATE_LIMIT_WINDOW)
        return response, status

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error('Send JSON like {"zone": "rods", "preset": "r1"}.', 400)

    zone = ZONES.get(data.get("zone"))
    if zone is None:
        return error("There is no such part of the exhibit.", 400)

    page = zone_page(zone, data.get("page"))      # optional: which screen page the visitor is on
    preset_id = data.get("preset")
    if preset_id is not None:
        if preset_id not in PRESETS or PRESETS[preset_id][0] != zone["id"]:
            return error("There is no such question here.", 400)
        preset = PRESETS[preset_id][1]
        question, focus = preset["question"], preset.get("focus")
        page = page or zone_page(zone, preset.get("page"))
    else:
        question, focus = clean_question(data.get("question"), QUESTION_MAX_CHARS), None
        if not question:
            return error("Ask Napier a question first.", 400)

    if client is None and not MOCK_AI:
        return error("The server is missing its API key.", 500)

    try:
        reply = chat_napier(zone, question, focus, clean_history(data.get("history")), page)
    except RuntimeError as exc:
        return error(str(exc), 502)

    try:
        storage.log_question(zone["id"], preset_id, None if preset_id else question, reply,
                             page["page"] if page else None)
        saved = True
    except Exception as exc:
        print(f"Database error logging question: {exc!r}")
        saved = False

    return jsonify({"zone": zone["id"], "reply": reply, "saved": saved})


@app.errorhandler(404)
def not_found(_):
    return error("Not found.", 404)


@app.errorhandler(405)
def method_not_allowed(_):
    return error("Method not allowed.", 405)


# ---------------------------------------------------------------------------
# 6. Local development: `python app.py` → http://127.0.0.1:5002
#    (5001 is Ask Yubo's backend, so both can run at once.)
#    On Render, `gunicorn app:app` runs instead and this block is skipped.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5002)), debug=True)
