"""SQLite-backed FSM storage so mid-workout state survives bot restarts."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

logger = logging.getLogger("gymflex.fsm")


def sqlite_path_from_database_url(database_url: str) -> Path:
    """Map SQLAlchemy async SQLite URL → filesystem path."""
    raw = database_url.strip()
    if raw.startswith("sqlite+aiosqlite:///"):
        path_part = raw[len("sqlite+aiosqlite:///") :]
    elif raw.startswith("sqlite:///"):
        path_part = raw[len("sqlite:///") :]
    else:
        parsed = urlparse(raw)
        path_part = unquote(parsed.path or "")
        if parsed.netloc and not path_part.startswith("/"):
            path_part = f"/{parsed.netloc}{path_part}"

    # sqlalchemy: sqlite:///./file.db → ./file.db; sqlite:////abs → /abs
    if path_part.startswith("//"):
        path_part = path_part[1:]
    return Path(path_part)


class SQLiteStorage(BaseStorage):
    """Persist FSM state+data in the same SQLite file as the app DB."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self._path)
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS fsm_storage (
                    bot_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    destiny TEXT NOT NULL DEFAULT 'default',
                    state TEXT,
                    data TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (bot_id, chat_id, user_id, destiny)
                )
                """
            )
            await self._db.commit()
            logger.info("FSM SQLite storage ready at %s", self._path)
        return self._db

    @staticmethod
    def _key_tuple(key: StorageKey) -> tuple[int, int, int, str]:
        return (int(key.bot_id), int(key.chat_id), int(key.user_id), str(key.destiny or "default"))

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        state_s = state.state if isinstance(state, State) else state
        bot_id, chat_id, user_id, destiny = self._key_tuple(key)
        db = await self._conn()
        await db.execute(
            """
            INSERT INTO fsm_storage (bot_id, chat_id, user_id, destiny, state, data)
            VALUES (?, ?, ?, ?, ?, '{}')
            ON CONFLICT(bot_id, chat_id, user_id, destiny) DO UPDATE SET state = excluded.state
            """,
            (bot_id, chat_id, user_id, destiny, state_s),
        )
        await db.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        bot_id, chat_id, user_id, destiny = self._key_tuple(key)
        db = await self._conn()
        cur = await db.execute(
            """
            SELECT state FROM fsm_storage
            WHERE bot_id=? AND chat_id=? AND user_id=? AND destiny=?
            """,
            (bot_id, chat_id, user_id, destiny),
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        if not isinstance(data, dict):
            raise TypeError(f"Data must be a dict, got {type(data).__name__}")
        payload = json.dumps(data, ensure_ascii=False, default=str)
        bot_id, chat_id, user_id, destiny = self._key_tuple(key)
        db = await self._conn()
        await db.execute(
            """
            INSERT INTO fsm_storage (bot_id, chat_id, user_id, destiny, state, data)
            VALUES (?, ?, ?, ?, NULL, ?)
            ON CONFLICT(bot_id, chat_id, user_id, destiny) DO UPDATE SET data = excluded.data
            """,
            (bot_id, chat_id, user_id, destiny, payload),
        )
        await db.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        bot_id, chat_id, user_id, destiny = self._key_tuple(key)
        db = await self._conn()
        cur = await db.execute(
            """
            SELECT data FROM fsm_storage
            WHERE bot_id=? AND chat_id=? AND user_id=? AND destiny=?
            """,
            (bot_id, chat_id, user_id, destiny),
        )
        row = await cur.fetchone()
        if not row or not row[0]:
            return {}
        try:
            parsed = json.loads(row[0])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            logger.warning("Corrupt FSM data for %s; resetting", key)
            return {}

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
