"""SQLite job/clip state + dedupe.

Two tables:
  jobs   — one row per source video URL that entered the pipeline.
  clips  — one row per rendered clip, tracking render + per-platform publish state.

Dedupe rules:
  * a source URL is processed at most once (jobs.source_url is UNIQUE);
  * a clip is keyed by (job_id, start_sec, end_sec) so re-running highlight never
    double-renders the same segment;
  * daily_post_cap is enforced by counting clips published in the last 24h.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url    TEXT UNIQUE NOT NULL,
    title         TEXT,
    channel       TEXT,
    duration_sec  REAL,
    status        TEXT NOT NULL DEFAULT 'queued',  -- queued|ingested|transcribed|highlighted|rendered|done|error
    error         TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS clips (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         INTEGER NOT NULL REFERENCES jobs(id),
    idx            INTEGER NOT NULL,
    start_sec      REAL NOT NULL,
    end_sec        REAL NOT NULL,
    hook_title     TEXT,
    caption        TEXT,
    hashtags       TEXT,
    render_path    TEXT,
    status         TEXT NOT NULL DEFAULT 'planned',  -- planned|rendered|approved|rejected|published|error
    yt_video_id    TEXT,
    yt_url         TEXT,
    ig_media_id    TEXT,
    ig_url         TEXT,
    published_at   REAL,
    error          TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    UNIQUE(job_id, start_sec, end_sec)
);
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # jobs ---------------------------------------------------------------
    def get_job_by_url(self, url: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM jobs WHERE source_url = ?", (url,)
        ).fetchone()

    def add_job(self, url: str) -> Optional[int]:
        """Insert a job. Returns job id, or None if the URL was already present."""
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO jobs (source_url, created_at, updated_at) "
                "VALUES (?, ?, ?)",
                (url, now, now),
            )
        if cur.rowcount == 0:
            return None
        return cur.lastrowid

    def update_job(self, job_id: int, **fields) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def jobs_by_status(self, *statuses: str) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self._conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY created_at", statuses
        ).fetchall()

    # clips ------------------------------------------------------------------
    def upsert_clip(
        self,
        job_id: int,
        idx: int,
        start_sec: float,
        end_sec: float,
        hook_title: str,
        caption: str,
        hashtags: str,
    ) -> int:
        now = time.time()
        with self._tx() as c:
            c.execute(
                """INSERT INTO clips
                   (job_id, idx, start_sec, end_sec, hook_title, caption, hashtags,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id, start_sec, end_sec) DO UPDATE SET
                     idx=excluded.idx, hook_title=excluded.hook_title,
                     caption=excluded.caption, hashtags=excluded.hashtags,
                     updated_at=excluded.updated_at""",
                (job_id, idx, start_sec, end_sec, hook_title, caption, hashtags, now, now),
            )
        row = self._conn.execute(
            "SELECT id FROM clips WHERE job_id=? AND start_sec=? AND end_sec=?",
            (job_id, start_sec, end_sec),
        ).fetchone()
        return int(row["id"])

    def update_clip(self, clip_id: int, **fields) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as c:
            c.execute(
                f"UPDATE clips SET {cols} WHERE id = ?", (*fields.values(), clip_id)
            )

    def clips_for_job(self, job_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM clips WHERE job_id = ? ORDER BY idx", (job_id,)
        ).fetchall()

    def clip(self, clip_id: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()

    def published_in_last_24h(self, platform: str) -> int:
        col = {"youtube": "yt_video_id", "instagram": "ig_media_id"}[platform]
        cutoff = time.time() - 24 * 3600
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM clips "
            f"WHERE {col} IS NOT NULL AND published_at >= ?",
            (cutoff,),
        ).fetchone()
        return int(row["n"])

    def uploaded_in_last_24h(self) -> int:
        """Clips that got a YouTube video id (public or unlisted) in the last 24h."""
        cutoff = time.time() - 24 * 3600
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM clips "
            "WHERE yt_video_id IS NOT NULL AND updated_at >= ?",
            (cutoff,),
        ).fetchone()
        return int(row["n"])

    def clip_by_yt_id(self, video_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM clips WHERE yt_video_id = ?", (video_id,)
        ).fetchone()
