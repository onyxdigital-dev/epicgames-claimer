import aiosqlite
import os
from datetime import datetime

DB_PATH = os.path.join(os.environ.get("CONFIG_DIR", "/config"), "data.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS claimed_games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                claimed_at DATETIME NOT NULL,
                cover_url TEXT,
                epic_id TEXT UNIQUE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.commit()

    # Seed from environment variables if not already set
    for env_key, db_key in [
        ("EPIC_EMAIL", "epic_email"),
        ("EPIC_PASSWORD", "epic_password"),
        ("NOTIFY_WEBHOOK_URL", "notify_url"),
        ("NOTIFY_WEBHOOK_TYPE", "notify_type"),
    ]:
        val = os.environ.get(env_key)
        if val:
            existing = await get_setting(db_key)
            if not existing:
                await set_setting(db_key, val)


async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def add_claimed_game(title: str, cover_url: str | None, epic_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO claimed_games (title, claimed_at, cover_url, epic_id) VALUES (?, ?, ?, ?)",
                (title, datetime.utcnow().isoformat(), cover_url, epic_id),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            pass  # already claimed


async def get_claimed_games() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT title, claimed_at, cover_url, epic_id FROM claimed_games ORDER BY claimed_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
