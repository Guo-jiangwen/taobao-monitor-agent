"""SQLite 快照仓库。

存储策略：只存「内容有变化」的快照。同一内容重复采集不会新增行，
这样快照表既是历史版本库，又是天然的去重账本。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    title        TEXT,
    price        REAL,
    stock        INTEGER,
    listing_status TEXT,
    main_image   TEXT,
    promotion    TEXT,
    review_count INTEGER,
    rating       REAL,
    content_fp   TEXT NOT NULL,
    payload      TEXT,
    fetched_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_item_time ON snapshot(item_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_snap_fp ON snapshot(content_fp);

CREATE TABLE IF NOT EXISTS change_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    severity    TEXT NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    event       TEXT NOT NULL,
    detected_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evt_item_time ON change_event(item_id, detected_at DESC);

CREATE TABLE IF NOT EXISTS fetch_audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    outcome   TEXT NOT NULL,
    detail    TEXT,
    ts        INTEGER NOT NULL
);
"""


class SnapshotRepository:
    def __init__(self, db_path: str = "data/monitor.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        """释放连接。常驻进程退出前必须调用，否则备份 / 删库 / 迁移都会被占用挡住。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- 写 ----
    def save_snapshot(self, snap: Any, content_fp: str) -> bool:
        """返回 True 表示落库了新版本；False 表示内容指纹一致，被去重跳过。"""
        with self._lock:
            same = self._conn.execute(
                "SELECT 1 FROM snapshot WHERE item_id=? AND content_fp=? LIMIT 1",
                (snap.item_id, content_fp),
            ).fetchone()
            if same:
                return False
            cur = self._conn.execute(
                """INSERT INTO snapshot
                   (item_id, source_id, title, price, stock, listing_status,
                    main_image, promotion, review_count, rating, content_fp, payload, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snap.item_id, snap.source_id, snap.title, snap.price, snap.stock,
                    snap.listing_status, snap.main_image, snap.promotion,
                    snap.review_count, snap.rating, content_fp, snap.payload, snap.fetched_at,
                ),
            )
            self._conn.commit()
            return cur.lastrowid is not None

    def save_events(self, events: list[dict[str, Any]]) -> int:
        if not events:
            return 0
        rows = [
            (e["item_id"], e["source_id"], e["severity"], e["field"],
             str(e.get("old_value")), str(e.get("new_value")), e["event"], e["detected_at"])
            for e in events
        ]
        with self._lock:
            self._conn.executemany(
                """INSERT INTO change_event
                   (item_id, source_id, severity, field, old_value, new_value, event, detected_at)
                   VALUES (?,?,?,?,?,?,?,?)""", rows
            )
            self._conn.commit()
            return len(rows)

    def log_fetch(self, source_id: str, outcome: str, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO fetch_audit (source_id, outcome, detail, ts) VALUES (?,?,?,?)",
                (source_id, outcome, detail[:500], int(time.time())),
            )
            self._conn.commit()

    # ---- 读 ----
    def latest(self, item_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM snapshot WHERE item_id=? ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return dict(row) if row else None

    def previous(self, item_id: str, before_fetched_at: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM snapshot WHERE item_id=? AND fetched_at < ? ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (item_id, before_fetched_at),
        ).fetchone()
        return dict(row) if row else None

    def history(self, item_id: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM snapshot WHERE item_id=? ORDER BY fetched_at DESC LIMIT ?", (item_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM change_event ORDER BY detected_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def drop_old(self, retention_days: int) -> int:
        cutoff = int(time.time()) - retention_days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM snapshot WHERE fetched_at < ?", (cutoff,))
            n1 = cur.rowcount
            cur = self._conn.execute("DELETE FROM change_event WHERE detected_at < ?", (cutoff,))
            n2 = cur.rowcount
            self._conn.commit()
            return (n1 or 0) + (n2 or 0)
