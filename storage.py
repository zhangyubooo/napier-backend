"""
storage.py — everything that touches the database lives here.

Two tables:

    visitors   one row per browser that has visited
    rods       one row per (visitor, card) — the rods in that visitor's box

app.py never writes SQL itself; it calls the four functions at the bottom.

Which database?
- On Render, DATABASE_URL points at a free Postgres database (Neon), so rods
  survive restarts and redeploys.
- On my laptop, with no DATABASE_URL, it falls back to a local SQLite file
  (napier.db). Same code, same tables — SQLAlchemy hides the difference.
"""

import os
from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, Integer, MetaData, String, Table, Text,
    UniqueConstraint, create_engine, func, select,
)


# ---------------------------------------------------------------------------
# 1. Connect
# ---------------------------------------------------------------------------

def _database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "sqlite:///napier.db"
    # Neon/Render hand out "postgres://" or "postgresql://" URLs. SQLAlchemy
    # needs to be told which Python driver to use — we installed psycopg (v3).
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _database_url()
USING_SQLITE = DATABASE_URL.startswith("sqlite")

# pool_pre_ping: free Postgres hosts close idle connections; this checks a
# connection is still alive before using it instead of crashing on a dead one.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
metadata = MetaData()


# ---------------------------------------------------------------------------
# 2. Tables
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc)


visitors = Table(
    "visitors", metadata,
    Column("id", String(36), primary_key=True),          # random UUID made by the browser
    Column("created_at", DateTime(timezone=True), default=_now),
    Column("last_seen", DateTime(timezone=True), default=_now),
)

rods = Table(
    "rods", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("visitor_id", String(36), nullable=False, index=True),
    Column("card", Integer, nullable=False),              # 1–16, see cards.json
    Column("inscription", Text, nullable=False),          # Napier's full answer
    Column("motto", Text, nullable=False),                # short line carved on the rod
    Column("created_at", DateTime(timezone=True), default=_now),
    # A visitor can only hold each rod once.
    UniqueConstraint("visitor_id", "card", name="one_rod_per_card"),
)


def init_db():
    """Create the tables if they don't exist yet. Safe to call on every start."""
    metadata.create_all(engine)


# ---------------------------------------------------------------------------
# 3. The four operations app.py uses
# ---------------------------------------------------------------------------

def touch_visitor(visitor_id):
    """Record that this visitor is here now (create them on first visit)."""
    with engine.begin() as conn:
        found = conn.execute(select(visitors.c.id).where(visitors.c.id == visitor_id)).first()
        if found:
            conn.execute(visitors.update().where(visitors.c.id == visitor_id).values(last_seen=_now()))
        else:
            conn.execute(visitors.insert().values(id=visitor_id))


def get_rods(visitor_id):
    """This visitor's rods, oldest first: [{"card", "inscription", "motto"}, ...]."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(rods.c.card, rods.c.inscription, rods.c.motto)
            .where(rods.c.visitor_id == visitor_id)
            .order_by(rods.c.created_at, rods.c.id)
        ).mappings().all()
    return [dict(row) for row in rows]


def save_rod(visitor_id, card, inscription, motto):
    """Put a rod in the box. If the visitor already has this card, overwrite it
    (only happens for card XVI, whose answer changes as the box fills)."""
    with engine.begin() as conn:
        existing = conn.execute(
            select(rods.c.id).where(rods.c.visitor_id == visitor_id, rods.c.card == card)
        ).first()
        if existing:
            conn.execute(
                rods.update().where(rods.c.id == existing.id)
                .values(inscription=inscription, motto=motto)
            )
        else:
            conn.execute(rods.insert().values(
                visitor_id=visitor_id, card=card, inscription=inscription, motto=motto,
            ))


def card_counts():
    """How many different visitors have asked each card: {card_number: count}."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(rods.c.card, func.count()).group_by(rods.c.card)
        ).all()
    return {card: count for card, count in rows}
