from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .models import AuditEntry, Binding


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.init()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bindings (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  guild_id TEXT NOT NULL,
                  channel_id TEXT NOT NULL,
                  thread_id TEXT,
                  herdr_target TEXT NOT NULL,
                  label TEXT,
                  created_by TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(guild_id, channel_id, thread_id)
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  timestamp TEXT NOT NULL,
                  discord_user_id TEXT NOT NULL,
                  guild_id TEXT,
                  channel_id TEXT,
                  thread_id TEXT,
                  action TEXT NOT NULL,
                  herdr_target TEXT,
                  payload_preview TEXT,
                  result TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_dedupe (
                  key TEXT PRIMARY KEY,
                  created_at TEXT NOT NULL
                );
                """
            )

    def upsert_binding(
        self,
        *,
        guild_id: int | str,
        channel_id: int | str,
        thread_id: int | str | None,
        herdr_target: str,
        label: str | None,
        created_by: int | str,
    ) -> None:
        now = _now()
        guild = str(guild_id)
        channel = str(channel_id)
        thread = _optional_str(thread_id)
        with self._connect() as conn:
            if thread is None:
                conn.execute(
                    """
                    DELETE FROM bindings
                    WHERE guild_id = ? AND channel_id = ? AND thread_id IS NULL
                    """,
                    (guild, channel),
                )
            else:
                conn.execute(
                    """
                    DELETE FROM bindings
                    WHERE guild_id = ? AND channel_id = ? AND thread_id = ?
                    """,
                    (guild, channel, thread),
                )
            conn.execute(
                """
                INSERT INTO bindings (
                  guild_id, channel_id, thread_id, herdr_target, label,
                  created_by, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild,
                    channel,
                    thread,
                    herdr_target,
                    label,
                    str(created_by),
                    now,
                    now,
                ),
            )

    def get_binding(
        self, *, guild_id: int | str, channel_id: int | str, thread_id: int | str | None
    ) -> Binding | None:
        thread = _optional_str(thread_id)
        with self._connect() as conn:
            if thread is None:
                rows = conn.execute(
                    """
                    SELECT * FROM bindings
                    WHERE guild_id = ? AND channel_id = ? AND thread_id IS NULL
                    LIMIT 1
                    """,
                    (str(guild_id), str(channel_id)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM bindings
                    WHERE guild_id = ? AND channel_id = ? AND (thread_id = ? OR thread_id IS NULL)
                    ORDER BY thread_id IS NULL ASC
                    LIMIT 1
                    """,
                    (str(guild_id), str(channel_id), thread),
                ).fetchall()
        return _binding(rows[0]) if rows else None

    def list_bindings(self, *, guild_id: int | str | None = None) -> list[Binding]:
        sql = "SELECT * FROM bindings"
        args: list[str] = []
        if guild_id is not None:
            sql += " WHERE guild_id = ?"
            args.append(str(guild_id))
        sql += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_binding(row) for row in rows]

    def delete_binding(
        self, *, guild_id: int | str, channel_id: int | str, thread_id: int | str | None
    ) -> int:
        thread = _optional_str(thread_id)
        with self._connect() as conn:
            if thread is None:
                cursor = conn.execute(
                    """
                    DELETE FROM bindings
                    WHERE guild_id = ? AND channel_id = ? AND thread_id IS NULL
                    """,
                    (str(guild_id), str(channel_id)),
                )
            else:
                cursor = conn.execute(
                    """
                    DELETE FROM bindings
                    WHERE guild_id = ? AND channel_id = ? AND thread_id = ?
                    """,
                    (str(guild_id), str(channel_id), thread),
                )
            return cursor.rowcount

    def add_audit(self, entry: AuditEntry) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_log (
                  timestamp, discord_user_id, guild_id, channel_id, thread_id,
                  action, herdr_target, payload_preview, result
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    entry.discord_user_id,
                    entry.guild_id,
                    entry.channel_id,
                    entry.thread_id,
                    entry.action,
                    entry.herdr_target,
                    entry.payload_preview,
                    entry.result,
                ),
            )

    def mark_event_seen(self, key: str) -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO event_dedupe (key, created_at) VALUES (?, ?)",
                    (key, _now()),
                )
            return True
        except sqlite3.IntegrityError:
            return False


def _binding(row: sqlite3.Row) -> Binding:
    return Binding(
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        thread_id=row["thread_id"],
        herdr_target=row["herdr_target"],
        label=row["label"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _optional_str(value: int | str | None) -> str | None:
    return None if value is None else str(value)


def _now() -> str:
    return datetime.now(UTC).isoformat()
