"""SQLite 存储层。

一个文件就是一个客服后台的全部记忆：
会话与消息、水位线、每日配额账本、审计流水、人物档案、承诺台账。

设计要点：
- 所有写入走一个连接 + 显式事务，避免多线程下 SQLite 锁冲突；
- 幂等键唯一索引保证"同一条消息不会重复回复"；
- 审计表只追加不修改，是事后追责的唯一凭据。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .contracts import Action, Envelope

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT NOT NULL,
    platform        TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'private',
    title           TEXT DEFAULT '',
    last_at         REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, conversation_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT NOT NULL,
    platform        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    sender_id       TEXT NOT NULL,
    sender_name     TEXT DEFAULT '',
    direction       TEXT NOT NULL,
    text            TEXT DEFAULT '',
    reply_to        TEXT,
    created_at      REAL NOT NULL,
    raw             TEXT DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_inbound_unique
    ON messages (platform, message_id) WHERE direction = 'inbound';
CREATE INDEX IF NOT EXISTS idx_messages_conv
    ON messages (platform, conversation_id, created_at);

CREATE TABLE IF NOT EXISTS watermarks (
    platform        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    last_at         REAL NOT NULL DEFAULT 0,
    last_message_id TEXT DEFAULT '',
    PRIMARY KEY (platform, conversation_id)
);

CREATE TABLE IF NOT EXISTS ledger (
    day         TEXT NOT NULL,
    platform    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, platform, kind)
);

CREATE TABLE IF NOT EXISTS audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      REAL NOT NULL,
    platform        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    sender_id       TEXT DEFAULT '',
    stage           TEXT NOT NULL,
    decision        TEXT NOT NULL,
    reason          TEXT DEFAULT '',
    payload         TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_audit_time ON audit (created_at DESC);

CREATE TABLE IF NOT EXISTS profiles (
    platform    TEXT NOT NULL,
    sender_id   TEXT NOT NULL,
    display     TEXT DEFAULT '',
    tier        TEXT NOT NULL DEFAULT 'normal',
    notes       TEXT DEFAULT '',
    turns       INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, sender_id)
);

CREATE TABLE IF NOT EXISTS promises (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    owner           TEXT NOT NULL,
    content         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      REAL NOT NULL,
    due_at          REAL
);

CREATE INDEX IF NOT EXISTS idx_promises_status ON promises (status, platform);

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class Store:
    """线程安全的 SQLite 门面。"""

    def __init__(self, path: str | Path | None = None) -> None:
        import os

        self.path = Path(path or os.getenv("MPC_DB", "data/mpc.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---------- 基础设施 ----------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _execute(self, sql: str, params: tuple = ()) -> int:
        with self._tx() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid or 0

    # ---------- 消息与会话 ----------

    def has_inbound(self, platform: str, message_id: str) -> bool:
        """幂等检查：该消息是否已处理过。"""
        rows = self._query(
            "SELECT 1 FROM messages WHERE platform = ? AND message_id = ? AND direction = 'inbound'",
            (platform, message_id),
        )
        return bool(rows)

    def record_inbound(self, env: Envelope) -> None:
        self._execute(
            """INSERT OR IGNORE INTO messages
               (message_id, platform, conversation_id, sender_id, sender_name,
                direction, text, reply_to, created_at, raw)
               VALUES (?, ?, ?, ?, ?, 'inbound', ?, ?, ?, ?)""",
            (
                env.message_id,
                env.platform.value,
                env.conversation_id,
                env.sender_id,
                env.sender_name,
                env.text,
                env.reply_to_message_id,
                env.timestamp,
                json.dumps(env.raw, ensure_ascii=False, default=str),
            ),
        )
        self._execute(
            """INSERT INTO conversations (conversation_id, platform, kind, title, last_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(platform, conversation_id) DO UPDATE SET
                   last_at = excluded.last_at,
                   title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE conversations.title END""",
            (
                env.conversation_id,
                env.platform.value,
                env.conversation_kind.value,
                "",
                env.timestamp,
            ),
        )
        self._execute(
            """INSERT INTO watermarks (platform, conversation_id, last_at, last_message_id)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(platform, conversation_id) DO UPDATE SET
                   last_at = excluded.last_at, last_message_id = excluded.last_message_id""",
            (env.platform.value, env.conversation_id, env.timestamp, env.message_id),
        )

    def record_outbound(self, action: Action) -> None:
        self._execute(
            """INSERT INTO messages
               (message_id, platform, conversation_id, sender_id, sender_name,
                direction, text, reply_to, created_at, raw)
               VALUES (?, ?, ?, 'hub', 'hub', 'outbound', ?, ?, ?, ?)""",
            (
                action.idempotency_key or action.action_id,
                action.platform.value,
                action.conversation_id,
                action.text,
                action.reply_to_message_id,
                action.created_at,
                json.dumps(action.meta, ensure_ascii=False, default=str),
            ),
        )

    def conversation_history(
        self, platform: str, conversation_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        rows = self._query(
            """SELECT direction, sender_id, sender_name, text, created_at
               FROM messages WHERE platform = ? AND conversation_id = ?
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (platform, conversation_id, limit),
        )
        return [dict(r) for r in reversed(rows)]

    def watermark(self, platform: str, conversation_id: str) -> dict[str, Any]:
        rows = self._query(
            "SELECT last_at, last_message_id FROM watermarks WHERE platform = ? AND conversation_id = ?",
            (platform, conversation_id),
        )
        if not rows:
            return {"last_at": 0.0, "last_message_id": ""}
        return dict(rows[0])

    # ---------- 每日账本（配额） ----------

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def bump_ledger(self, platform: str, kind: str, delta: int = 1) -> int:
        day = self._today()
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO ledger (day, platform, kind, count) VALUES (?, ?, ?, ?)
                   ON CONFLICT(day, platform, kind) DO UPDATE SET count = count + excluded.count""",
                (day, platform, kind, delta),
            )
            row = conn.execute(
                "SELECT count FROM ledger WHERE day = ? AND platform = ? AND kind = ?",
                (day, platform, kind),
            ).fetchone()
        return int(row["count"]) if row else 0

    def ledger_count(self, platform: str, kind: str, day: str | None = None) -> int:
        day = day or self._today()
        rows = self._query(
            "SELECT count FROM ledger WHERE day = ? AND platform = ? AND kind = ?",
            (day, platform, kind),
        )
        return int(rows[0]["count"]) if rows else 0

    def ledger_snapshot(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM ledger WHERE day = ? ORDER BY platform", (self._today(),))]

    # ---------- 审计 ----------

    def audit(
        self,
        stage: str,
        decision: str,
        reason: str = "",
        platform: str = "",
        conversation_id: str = "",
        sender_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> int:
        return self._execute(
            """INSERT INTO audit (created_at, platform, conversation_id, sender_id, stage, decision, reason, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                platform,
                conversation_id,
                sender_id,
                stage,
                decision,
                reason,
                json.dumps(payload or {}, ensure_ascii=False, default=str),
            ),
        )

    def audit_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload") or "{}")
            except json.JSONDecodeError:
                d["payload"] = {}
            out.append(d)
        return out

    # ---------- 人物档案 ----------

    def upsert_profile(
        self, platform: str, sender_id: str, display: str = "", tier: str = "", notes: str = ""
    ) -> None:
        self._execute(
            """INSERT INTO profiles (platform, sender_id, display, tier, notes, turns, updated_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)
               ON CONFLICT(platform, sender_id) DO UPDATE SET
                   display = CASE WHEN excluded.display <> '' THEN excluded.display ELSE profiles.display END,
                   tier = CASE WHEN excluded.tier <> '' THEN excluded.tier ELSE profiles.tier END,
                   notes = CASE WHEN excluded.notes <> '' THEN excluded.notes ELSE profiles.notes END,
                   updated_at = excluded.updated_at""",
            (platform, sender_id, display, tier or "normal", notes, time.time()),
        )

    def bump_profile_turns(self, platform: str, sender_id: str) -> None:
        self._execute(
            "UPDATE profiles SET turns = turns + 1, updated_at = ? WHERE platform = ? AND sender_id = ?",
            (time.time(), platform, sender_id),
        )

    def profile(self, platform: str, sender_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM profiles WHERE platform = ? AND sender_id = ?", (platform, sender_id)
        )
        return dict(rows[0]) if rows else None

    # ---------- 承诺台账 ----------

    def add_promise(
        self, platform: str, conversation_id: str, owner: str, content: str, due_at: float | None = None
    ) -> int:
        return self._execute(
            """INSERT INTO promises (platform, conversation_id, owner, content, status, created_at, due_at)
               VALUES (?, ?, ?, ?, 'open', ?, ?)""",
            (platform, conversation_id, owner, content, time.time(), due_at),
        )

    def open_promises(self, platform: str, conversation_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if conversation_id:
            rows = self._query(
                """SELECT * FROM promises WHERE platform = ? AND conversation_id = ? AND status = 'open'
                   ORDER BY id DESC LIMIT ?""",
                (platform, conversation_id, limit),
            )
        else:
            rows = self._query(
                "SELECT * FROM promises WHERE platform = ? AND status = 'open' ORDER BY id DESC LIMIT ?",
                (platform, limit),
            )
        return [dict(r) for r in rows]

    def close_promise(self, promise_id: int, status: str = "done") -> None:
        self._execute("UPDATE promises SET status = ? WHERE id = ?", (status, promise_id))

    # ---------- KV ----------

    def kv_get(self, key: str, default: str = "") -> str:
        rows = self._query("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def kv_set(self, key: str, value: str) -> None:
        self._execute(
            """INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, time.time()),
        )
