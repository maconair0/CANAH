"""
Parent passages for parent-child retrieval (UPGRADE_PLAN Phase 5).

Children are embedded and ranked; parents are what the Generator actually
reads. Parents live here rather than in the vector store because they are
never searched — storing them in Chroma would mean embedding every passage
twice for no retrieval benefit, and Chroma's metadata is meant for small
scalars, not 900-character bodies.

SQLite, same reasoning as the twin store and the calibration store: a
file-based lookup table with one writer, no service to run.

Absent parents are not an error. An index built before this existed has none,
and retrieval must return the child text unchanged in that case rather than
losing passages — which is what makes this safe to add without re-embedding
the corpus first.
"""
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("rag.parents")

from .settings import PARENTS_DB as DEFAULT_DB  # noqa: E402

_SCHEMA = """
CREATE TABLE IF NOT EXISTS parents (
    source        TEXT NOT NULL,
    parent_index  INTEGER NOT NULL,
    text          TEXT NOT NULL,
    section_kind  TEXT,
    page_start    INTEGER,
    page_end      INTEGER,
    PRIMARY KEY (source, parent_index)
);
"""


class ParentStore:
    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        parent_dir = os.path.dirname(db_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._len_cache: Dict[Any, Optional[int]] = {}
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def put_many(self, source: str, parents: Iterable[Dict[str, Any]]) -> int:
        rows = [
            (source, p.get("chunk_index"), p.get("text", ""),
             p.get("section_kind"), p.get("page_start"), p.get("page_end"))
            for p in parents if p.get("chunk_index") is not None
        ]
        if not rows:
            return 0
        self._len_cache.clear()
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO parents "
                "(source, parent_index, text, section_kind, page_start, page_end) "
                "VALUES (?,?,?,?,?,?)", rows)
            self._conn.commit()
        return len(rows)

    def get(self, source: str, parent_index: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM parents WHERE source = ? AND parent_index = ?",
                (source, int(parent_index)),
            ).fetchone()
        return dict(row) if row else None

    def length(self, source: str, parent_index: int) -> Optional[int]:
        """Character length of a parent, cached.

        Retrieval asks this for every fused candidate, so it has to be cheap.
        The lengths of already-ingested passages never change — a re-ingest
        replaces the rows and the process restarts — so an unbounded dict is
        bounded in practice by the corpus, not by traffic.
        """
        key = (source, int(parent_index))
        if key in self._len_cache:
            return self._len_cache[key]
        with self._lock:
            row = self._conn.execute(
                "SELECT LENGTH(text) FROM parents WHERE source = ? AND parent_index = ?",
                key,
            ).fetchone()
        value = int(row[0]) if row and row[0] is not None else None
        self._len_cache[key] = value
        return value

    def delete_by_source(self, source: str) -> int:
        self._len_cache.clear()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM parents WHERE source = ?", (source,))
            self._conn.commit()
        return cur.rowcount or 0

    def count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM parents").fetchone()[0]

    def expand(self, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Replace child hits with their parent passages, keeping rank order.

        Two children of the same parent are one result, not two: they are the
        same passage, and returning it twice would spend the Generator's
        context budget on a duplicate while pushing a distinct passage out of
        the top-k.
        """
        out: List[Dict[str, Any]] = []
        seen = set()
        for hit in hits:
            meta = hit.get("metadata") or hit
            source = meta.get("source") or hit.get("source")
            parent_index = meta.get("parent_index", hit.get("parent_index"))
            if source is None or parent_index is None:
                out.append(hit)          # not a child; pass through untouched
                continue
            key = (source, int(parent_index))
            if key in seen:
                continue
            seen.add(key)
            parent = self.get(source, int(parent_index))
            if parent is None:
                out.append(hit)          # index predates parent storage
                continue
            expanded = dict(hit)
            expanded["text"] = parent["text"]
            expanded["matched_child_text"] = hit.get("text")
            expanded["expanded_from_child"] = True
            out.append(expanded)
        return out

    def close(self):
        with self._lock:
            self._conn.close()
