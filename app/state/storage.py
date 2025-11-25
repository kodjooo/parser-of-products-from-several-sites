from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Iterable, Iterator

from app.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class CategoryState:
    site_name: str
    category_url: str
    last_page: int | None = None
    last_offset: int | None = None
    last_product_count: int | None = None
    last_run_ts: datetime | None = None


class StateStore:
    """SQLite-хранилище состояния и прогресса по категориям."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._ensure_schema()
        logger.info("Инициализировано локальное состояние", extra={"db": str(db_path)})

    def _ensure_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS category_state (
                    site_name TEXT NOT NULL,
                    category_url TEXT NOT NULL,
                    last_page INTEGER,
                    last_offset INTEGER,
                    last_product_count INTEGER,
                    last_run_ts TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (site_name, category_url)
                )
                """
            )

    def upsert(self, state: CategoryState) -> None:
        payload = (
            state.site_name,
            state.category_url,
            state.last_page,
            state.last_offset,
            state.last_product_count,
            state.last_run_ts.isoformat() if state.last_run_ts else None,
        )
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO category_state
                (site_name, category_url, last_page, last_offset, last_product_count, last_run_ts)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(site_name, category_url) DO UPDATE SET
                    last_page=excluded.last_page,
                    last_offset=excluded.last_offset,
                    last_product_count=excluded.last_product_count,
                    last_run_ts=excluded.last_run_ts,
                    updated_at=CURRENT_TIMESTAMP
                """,
                payload,
            )

    def get(self, site_name: str, category_url: str) -> CategoryState | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT site_name, category_url, last_page, last_offset,
                       last_product_count, last_run_ts
                  FROM category_state
                 WHERE site_name=? AND category_url=?
                """,
                (site_name, category_url),
            ).fetchone()
        if not row:
            return None
        return CategoryState(
            site_name=row["site_name"],
            category_url=row["category_url"],
            last_page=row["last_page"],
            last_offset=row["last_offset"],
            last_product_count=row["last_product_count"],
            last_run_ts=datetime.fromisoformat(row["last_run_ts"])
            if row["last_run_ts"]
            else None,
        )

    def iter_site_state(self, site_name: str) -> Iterator[CategoryState]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT site_name, category_url, last_page, last_offset,
                       last_product_count, last_run_ts
                  FROM category_state
                 WHERE site_name=?
                """,
                (site_name,),
            ).fetchall()
        for row in rows:
            yield CategoryState(
                site_name=row["site_name"],
                category_url=row["category_url"],
                last_page=row["last_page"],
                last_offset=row["last_offset"],
                last_product_count=row["last_product_count"],
                last_run_ts=datetime.fromisoformat(row["last_run_ts"])
                if row["last_run_ts"]
                else None,
            )

    def reset_site(self, site_name: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM category_state WHERE site_name=?", (site_name,)
            )

    def reset_category(self, site_name: str, category_url: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM category_state WHERE site_name=? AND category_url=?",
                (site_name, category_url),
            )

    def reset_all(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM category_state")

    def iter_all(self) -> Iterator[CategoryState]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT site_name, category_url, last_page, last_offset,
                       last_product_count, last_run_ts
                  FROM category_state
                """
            ).fetchall()
        for row in rows:
            yield CategoryState(
                site_name=row["site_name"],
                category_url=row["category_url"],
                last_page=row["last_page"],
                last_offset=row["last_offset"],
                last_product_count=row["last_product_count"],
                last_run_ts=datetime.fromisoformat(row["last_run_ts"])
                if row["last_run_ts"]
                else None,
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
