"""Read-only access to the ingestion database (``analytics`` table)."""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

from .event import RawEvent

log = logging.getLogger(__name__)

_COLUMNS = ("id, event_id, event_type, timestamp_ms, user_id, session_id, source, application, "
            "environment, schema_version, payload_json, attributes, received_at_ms")


class SourceUnavailable(Exception):
    """The ingestion database does not exist yet / cannot be read right now."""


class IngestReader:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._conn: Optional[sqlite3.Connection] = None
        self._inode: Optional[int] = None
        self._last_inode: Optional[int] = None
        self.replaced = False

    def _connect(self) -> sqlite3.Connection:
        try:
            inode = os.stat(self.path).st_ino
        except FileNotFoundError:
            self.close()
            raise SourceUnavailable(f"{self.path} does not exist yet")
        if self._last_inode is not None and inode != self._last_inode:
            log.info("ingestion database file was replaced")
            self.replaced = True
            self.close()
        self._last_inode = inode
        if self._conn is None:
            try:
                conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10,
                                       check_same_thread=False)
                conn.execute("PRAGMA query_only = 1")
                conn.execute("SELECT 1 FROM analytics LIMIT 1")
            except sqlite3.Error as exc:
                raise SourceUnavailable(f"cannot read {self.path}: {exc}") from exc
            conn.row_factory = sqlite3.Row
            self._conn, self._inode = conn, inode
        return self._conn

    def fetch(self, after_id: int, limit: int) -> list[RawEvent]:
        try:
            rows = self._connect().execute(
                f"SELECT {_COLUMNS} FROM analytics WHERE id > ? ORDER BY id LIMIT ?",
                (after_id, limit)).fetchall()
        except sqlite3.OperationalError as exc:
            self.close()
            raise SourceUnavailable(str(exc)) from exc
        return [RawEvent(**dict(row)) for row in rows]

    def max_id(self) -> int:
        try:
            return self._connect().execute("SELECT COALESCE(MAX(id), 0) FROM analytics").fetchone()[0]
        except sqlite3.OperationalError as exc:
            self.close()
            raise SourceUnavailable(str(exc)) from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
