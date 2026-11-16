from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    digest TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_items (
    batch_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    item_generation INTEGER NOT NULL,
    status TEXT NOT NULL,
    evidence TEXT NOT NULL,
    state TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    revision_added INTEGER NOT NULL,
    revision_changed INTEGER NOT NULL,
    superseded_revision INTEGER,
    PRIMARY KEY (batch_id, item_key, item_generation)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_batch_items_active
    ON batch_items (batch_id, item_key) WHERE active = 1;
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    item_generation INTEGER NOT NULL,
    receipt_seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    evidence TEXT NOT NULL,
    state TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    issued_revision INTEGER NOT NULL,
    claimed_by TEXT,
    confirmed_by TEXT,
    claimed_at TEXT NOT NULL,
    confirmed_at TEXT,
    superseded_revision INTEGER,
    superseded_at TEXT,
    late_confirmed INTEGER NOT NULL DEFAULT 0,
    UNIQUE (batch_id, item_key, item_generation, receipt_seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_receipts_active
    ON receipts (batch_id, item_key, item_generation) WHERE active = 1;
CREATE TABLE IF NOT EXISTS batch_corrections (
    batch_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, digest)
);
CREATE TABLE IF NOT EXISTS reversals (
    reversal_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL UNIQUE,
    batch_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE TABLE IF NOT EXISTS batch_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    item_key TEXT,
    receipt_id TEXT,
    reversal_id TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);
"""


class Store:
    """SQLite 持久化。

    写操作通过 ``transaction()`` 串行化（``BEGIN IMMEDIATE``）。同一进程内
    所有批次写入共享一把可重入锁，保证领取、确认、撤销之间的顺序可判定；
    SQLite 的 WAL 与 UNIQUE 约束再提供跨连接的第二道防线。
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self):
        """串行化写事务；异常时整体回滚，不留半条记录。"""
        with self._lock:
            db = self.connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
            finally:
                db.close()

    @contextmanager
    def lock(self):
        """跨多个事务持有写锁，用于两阶段提交（可重入）。"""
        with self._lock:
            yield

    @contextmanager
    def read(self):
        with self._lock:
            db = self.connect()
            try:
                yield db
            finally:
                db.close()

    # -- 既有运行摘要（保持原行为） ----------------------------------------
    def record(self, digest: str, run_id: str, result: dict) -> dict:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?)",
                       (digest, run_id, json.dumps(result, ensure_ascii=False, sort_keys=True)))
            row = db.execute("SELECT run_id, result FROM runs WHERE digest=?", (digest,)).fetchone()
        return {"run_id": row[0], "result": json.loads(row[1])}

    def count(self) -> int:
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def all(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT run_id, digest, result FROM runs ORDER BY run_id").fetchall()
        return [{"run_id": r[0], "digest": r[1], "result": json.loads(r[2])} for r in rows]
