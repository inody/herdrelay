from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .models import (
    AuditEntry,
    Binding,
    PendingQuestion,
    QuestionOption,
    TelegramConfirmation,
    TelegramTopicRoute,
    TransportAuditEntry,
)


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

                CREATE TABLE IF NOT EXISTS bot_state (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_threads (
                  pane_id TEXT PRIMARY KEY,
                  thread_id TEXT NOT NULL,
                  guild_id TEXT NOT NULL,
                  alias TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_questions (
                  pane_id TEXT PRIMARY KEY,
                  event_id TEXT NOT NULL,
                  prompt TEXT NOT NULL,
                  options_json TEXT NOT NULL,
                  multi_select INTEGER NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transport_audit (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  timestamp TEXT NOT NULL,
                  transport TEXT NOT NULL,
                  actor_id TEXT NOT NULL,
                  container_id TEXT NOT NULL,
                  conversation_id TEXT,
                  action TEXT NOT NULL,
                  target_key TEXT,
                  payload_preview TEXT,
                  observed_turn INTEGER,
                  outcome TEXT NOT NULL,
                  detail TEXT
                );

                CREATE TABLE IF NOT EXISTS telegram_confirmations (
                  token TEXT PRIMARY KEY,
                  actor_id TEXT NOT NULL,
                  chat_id TEXT NOT NULL,
                  thread_id TEXT NOT NULL,
                  core_id TEXT NOT NULL,
                  terminal_address TEXT NOT NULL,
                  action TEXT NOT NULL,
                  expires_at REAL NOT NULL,
                  consumed_at TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS telegram_topics (
                  core_id TEXT NOT NULL,
                  terminal_address TEXT NOT NULL,
                  chat_id TEXT NOT NULL,
                  thread_id TEXT NOT NULL,
                  name TEXT NOT NULL,
                  closed INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(core_id, terminal_address),
                  UNIQUE(chat_id, thread_id)
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

    def find_binding_for_target(self, herdr_target: str) -> Binding | None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM bindings
                WHERE herdr_target = ?
                ORDER BY thread_id IS NULL ASC, updated_at DESC
                LIMIT 1
                """,
                (herdr_target,),
            ).fetchall()
        return _binding(rows[0]) if rows else None

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

    def add_transport_audit(self, entry: TransportAuditEntry) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO transport_audit (
                  timestamp, transport, actor_id, container_id,
                  conversation_id, action, target_key, payload_preview,
                  observed_turn, outcome, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    entry.transport,
                    entry.actor_id,
                    entry.container_id,
                    entry.conversation_id,
                    entry.action,
                    entry.target_key,
                    entry.payload_preview,
                    entry.observed_turn,
                    entry.outcome,
                    entry.detail,
                ),
            )
            return int(cursor.lastrowid)

    def claim_transport_mutation(
        self, *, event_key: str, entry: TransportAuditEntry
    ) -> int | None:
        """Atomically claim a remote mutation and persist its attempt audit."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO event_dedupe (key, created_at) VALUES (?, ?)",
                    (event_key, _now()),
                )
                cursor = conn.execute(
                    """
                    INSERT INTO transport_audit (
                      timestamp, transport, actor_id, container_id,
                      conversation_id, action, target_key, payload_preview,
                      observed_turn, outcome, detail
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _now(),
                        entry.transport,
                        entry.actor_id,
                        entry.container_id,
                        entry.conversation_id,
                        entry.action,
                        entry.target_key,
                        entry.payload_preview,
                        entry.observed_turn,
                        entry.outcome,
                        entry.detail,
                    ),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def finish_transport_audit(
        self,
        audit_id: int,
        *,
        outcome: str,
        detail: str | None = None,
        observed_turn: int | None = None,
    ) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE transport_audit
                SET outcome = ?, detail = ?,
                    observed_turn = COALESCE(?, observed_turn)
                WHERE id = ?
                """,
                (outcome, detail, observed_turn, audit_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"transport audit not found: {audit_id}")

    def list_transport_audits(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transport_audit ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def count_transport_mutations(
        self,
        *,
        transport: str,
        since: str,
        actor_id: str | None = None,
        container_id: str | None = None,
        target_key: str | None = None,
    ) -> int:
        filters = [
            ("actor_id", actor_id),
            ("container_id", container_id),
            ("target_key", target_key),
        ]
        selected = [(column, value) for column, value in filters if value is not None]
        if len(selected) != 1:
            raise ValueError("exactly one transport mutation rate-limit key is required")
        column, value = selected[0]
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM transport_audit
                WHERE transport = ? AND timestamp >= ? AND {column} = ?
                  AND action IN ('send_text', 'interrupt')
                  AND outcome IN ('attempt', 'succeeded', 'failed', 'outcome_unknown')
                """,
                (transport, since, value),
            ).fetchone()
        return int(row["count"])

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

    def has_event_key(self, key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM event_dedupe WHERE key = ? LIMIT 1", (key,)
            ).fetchone()
        return row is not None

    def has_event_key_prefix(self, prefix: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM event_dedupe WHERE key LIKE ? LIMIT 1",
                (f"{prefix}%",),
            ).fetchone()
        return row is not None

    def get_state(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value = excluded.value,
                  updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def create_telegram_confirmation(
        self,
        *,
        token: str,
        actor_id: int | str,
        chat_id: int | str,
        thread_id: int | str,
        core_id: str,
        terminal_address: str,
        action: str,
        expires_at: float,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telegram_confirmations (
                  token, actor_id, chat_id, thread_id, core_id,
                  terminal_address, action, expires_at, consumed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    token,
                    str(actor_id),
                    str(chat_id),
                    str(thread_id),
                    core_id,
                    terminal_address,
                    action,
                    expires_at,
                    _now(),
                ),
            )

    def consume_telegram_confirmation(
        self,
        *,
        token: str,
        actor_id: int | str,
        chat_id: int | str,
        thread_id: int | str,
        action: str,
        now: float | None = None,
    ) -> TelegramConfirmation | None:
        timestamp = time.time() if now is None else now
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM telegram_confirmations
                WHERE token = ? AND actor_id = ? AND chat_id = ?
                  AND thread_id = ? AND action = ? AND consumed_at IS NULL
                  AND expires_at >= ?
                """,
                (
                    token,
                    str(actor_id),
                    str(chat_id),
                    str(thread_id),
                    action,
                    timestamp,
                ),
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """
                UPDATE telegram_confirmations
                SET consumed_at = ?
                WHERE token = ? AND consumed_at IS NULL
                """,
                (_now(), token),
            )
            if cursor.rowcount != 1:
                return None
            return _telegram_confirmation(row)

    def get_telegram_topic(
        self, *, core_id: str, terminal_address: str
    ) -> TelegramTopicRoute | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM telegram_topics
                WHERE core_id = ? AND terminal_address = ?
                """,
                (core_id, terminal_address),
            ).fetchone()
        return _telegram_topic(row) if row else None

    def get_telegram_topic_by_destination(
        self, *, chat_id: int | str, thread_id: int | str
    ) -> TelegramTopicRoute | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM telegram_topics
                WHERE chat_id = ? AND thread_id = ?
                """,
                (str(chat_id), str(thread_id)),
            ).fetchone()
        return _telegram_topic(row) if row else None

    def upsert_telegram_topic(
        self,
        *,
        core_id: str,
        terminal_address: str,
        chat_id: int | str,
        thread_id: int | str,
        name: str,
        closed: bool = False,
    ) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telegram_topics (
                  core_id, terminal_address, chat_id, thread_id, name,
                  closed, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(core_id, terminal_address) DO UPDATE SET
                  chat_id = excluded.chat_id,
                  thread_id = excluded.thread_id,
                  name = excluded.name,
                  closed = excluded.closed,
                  updated_at = excluded.updated_at
                """,
                (
                    core_id,
                    terminal_address,
                    str(chat_id),
                    str(thread_id),
                    name,
                    int(closed),
                    now,
                    now,
                ),
            )

    def set_telegram_topic_closed(
        self, *, core_id: str, terminal_address: str, closed: bool
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE telegram_topics
                SET closed = ?, updated_at = ?
                WHERE core_id = ? AND terminal_address = ?
                """,
                (int(closed), _now(), core_id, terminal_address),
            )

    def upsert_pending_question(self, question: PendingQuestion) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_questions (
                  pane_id, event_id, prompt, options_json, multi_select, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(pane_id) DO UPDATE SET
                  event_id = excluded.event_id,
                  prompt = excluded.prompt,
                  options_json = excluded.options_json,
                  multi_select = excluded.multi_select,
                  created_at = excluded.created_at
                """,
                (
                    question.pane_id,
                    question.event_id,
                    question.prompt,
                    json.dumps(
                        [
                            {"label": option.label, "description": option.description}
                            for option in question.options
                        ],
                        ensure_ascii=False,
                    ),
                    question.multi_select,
                    _now(),
                ),
            )

    def get_pending_question(self, pane_id: str) -> PendingQuestion | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_questions WHERE pane_id = ?", (pane_id,)
            ).fetchone()
        if row is None:
            return None
        return _pending_question(row)

    def list_pending_questions(self) -> list[PendingQuestion]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM pending_questions").fetchall()
        questions = (_pending_question(row) for row in rows)
        return [question for question in questions if question is not None]

    def clear_pending_question(self, pane_id: str, *, event_id: str | None = None) -> None:
        with self._connect() as conn:
            if event_id is None:
                conn.execute("DELETE FROM pending_questions WHERE pane_id = ?", (pane_id,))
            else:
                conn.execute(
                    "DELETE FROM pending_questions WHERE pane_id = ? AND event_id = ?",
                    (pane_id, event_id),
                )

    def clear_pending_questions_except(self, pane_ids: set[str]) -> None:
        """Drop pending questions for panes that no longer exist."""
        with self._connect() as conn:
            stale = [
                row["pane_id"]
                for row in conn.execute("SELECT pane_id FROM pending_questions")
                if row["pane_id"] not in pane_ids
            ]
            conn.executemany(
                "DELETE FROM pending_questions WHERE pane_id = ?",
                [(pane_id,) for pane_id in stale],
            )

    def upsert_agent_thread(
        self,
        *,
        pane_id: str,
        thread_id: int | str,
        guild_id: int | str,
        alias: str | None = None,
    ) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_threads (pane_id, thread_id, guild_id, alias, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(pane_id) DO UPDATE SET
                  thread_id = excluded.thread_id,
                  guild_id = excluded.guild_id,
                  alias = excluded.alias,
                  updated_at = excluded.updated_at
                """,
                (pane_id, str(thread_id), str(guild_id), alias, now, now),
            )

    def get_agent_thread(self, pane_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT thread_id FROM agent_threads WHERE pane_id = ?",
                (pane_id,),
            ).fetchone()
        return str(row["thread_id"]) if row else None

    def list_agent_threads(self) -> dict[str, str]:
        """Return mapping of pane_id -> thread_id for all recorded threads."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT pane_id, thread_id FROM agent_threads"
            ).fetchall()
        return {str(row["pane_id"]): str(row["thread_id"]) for row in rows}


def _pending_question(row: sqlite3.Row) -> PendingQuestion | None:
    try:
        raw_options = json.loads(row["options_json"])
        options = tuple(
            QuestionOption(
                label=str(option["label"]),
                description=(
                    str(option["description"])
                    if option.get("description") is not None
                    else None
                ),
            )
            for option in raw_options
            if isinstance(option, dict) and isinstance(option.get("label"), str)
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not options:
        return None
    return PendingQuestion(
        pane_id=str(row["pane_id"]),
        event_id=str(row["event_id"]),
        prompt=str(row["prompt"]),
        options=options,
        multi_select=bool(row["multi_select"]),
    )


def _telegram_confirmation(row: sqlite3.Row) -> TelegramConfirmation:
    return TelegramConfirmation(
        token=str(row["token"]),
        actor_id=str(row["actor_id"]),
        chat_id=str(row["chat_id"]),
        thread_id=str(row["thread_id"]),
        core_id=str(row["core_id"]),
        terminal_address=str(row["terminal_address"]),
        action=str(row["action"]),
        expires_at=float(row["expires_at"]),
    )


def _telegram_topic(row: sqlite3.Row) -> TelegramTopicRoute:
    return TelegramTopicRoute(
        core_id=str(row["core_id"]),
        terminal_address=str(row["terminal_address"]),
        chat_id=str(row["chat_id"]),
        thread_id=str(row["thread_id"]),
        name=str(row["name"]),
        closed=bool(row["closed"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


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
