"""Durable, track-scoped subtitle and German-learning glossary caches.

Every operation opens its own SQLite connection. WAL permits readers while a
writer is active; clip insertion claims its segment keys in one transaction.
No connection or transaction is retained between calls.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Iterator


class SubtitleDB:
    def __init__(self, path: str | Path):
        if str(path) == ":memory:":
            raise ValueError("SubtitleDB requires a file path for persistent storage")
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"Unsupported subtitle database version: {version}")
            connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS clips (
                    id INTEGER PRIMARY KEY,
                    movie_key TEXT NOT NULL,
                    start REAL NOT NULL,
                    end REAL NOT NULL,
                    record_json TEXT NOT NULL,
                    cue_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    CHECK (start >= 0 AND end > start)
                );
                CREATE INDEX IF NOT EXISTS clips_movie_time ON clips(movie_key, start, end);
                CREATE TABLE IF NOT EXISTS segments (
                    movie_key TEXT NOT NULL,
                    segment_key TEXT NOT NULL,
                    clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
                    PRIMARY KEY (movie_key, segment_key)
                );
                CREATE TABLE IF NOT EXISTS glosses (
                    profile_key TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    enhanced_text TEXT NOT NULL,
                    annotations_json TEXT,
                    latency_ms REAL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    PRIMARY KEY (profile_key, source_hash, source_text)
                );
                PRAGMA user_version=1;
                COMMIT;
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _key(value: str, name: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a nonempty string")
        return value

    @staticmethod
    def _seconds(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite number")
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise ValueError(f"{name} must be a finite nonnegative number")
        return result

    @classmethod
    def _record(cls, record: dict) -> tuple[dict, str]:
        if not isinstance(record, dict):
            raise ValueError("A clip record must be an object")
        required = {"start", "end", "keys", "cues", "audio_seconds", "model"}
        if not required.issubset(record):
            raise ValueError("Clip record is missing required fields")
        # Copy through JSON so callers retain ownership of their record and all
        # extra provider fields survive without sharing mutable objects.
        clean = json.loads(json.dumps(record, ensure_ascii=False, allow_nan=False))
        clean["start"] = cls._seconds(clean["start"], "start")
        clean["end"] = cls._seconds(clean["end"], "end")
        clean["audio_seconds"] = cls._seconds(clean["audio_seconds"], "audio_seconds")
        if clean["end"] <= clean["start"]:
            raise ValueError("Clip end must be after its start")
        cls._key(clean["model"], "model")
        keys = clean["keys"]
        if not isinstance(keys, list) or not keys or any(not isinstance(key, str) or not key for key in keys):
            raise ValueError("Clip keys must be a nonempty list of segment keys")
        if len(set(keys)) != len(keys):
            raise ValueError("A clip cannot own a segment key more than once")
        if not isinstance(clean["cues"], list):
            raise ValueError("Clip cues must be a list")
        for cue in clean["cues"]:
            if not isinstance(cue, dict) or not {"start", "end", "text"}.issubset(cue):
                raise ValueError("Each cue needs start, end and text")
            cue["start"] = cls._seconds(cue["start"], "cue start")
            cue["end"] = cls._seconds(cue["end"], "cue end")
            if cue["end"] <= cue["start"] or not isinstance(cue["text"], str):
                raise ValueError("Each cue needs a positive duration and text")
        return clean, json.dumps(clean, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    def load_clips(self, movie_key: str) -> list[dict]:
        """Return independent original records, ordered by their movie time."""
        self._key(movie_key, "movie_key")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT record_json FROM clips WHERE movie_key=? ORDER BY start, end, id",
                (movie_key,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_clip(self, movie_key: str, record: dict) -> bool:
        """Save one clip, or return False if any owned segment already exists.

        The entire batch is rejected on overlap. Nothing from an earlier clip
        is overwritten, and a rejected batch never claims its new segment keys.
        """
        self._key(movie_key, "movie_key")
        clean, encoded = self._record(record)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Check each key without depending on SQLite's variable limit.
                if any(connection.execute(
                    "SELECT 1 FROM segments WHERE movie_key=? AND segment_key=?",
                    (movie_key, key),
                ).fetchone() for key in clean["keys"]):
                    connection.rollback()
                    return False
                cursor = connection.execute(
                    "INSERT INTO clips(movie_key,start,end,record_json,cue_count) VALUES(?,?,?,?,?)",
                    (movie_key, clean["start"], clean["end"], encoded, len(clean["cues"])),
                )
                connection.executemany(
                    "INSERT INTO segments(movie_key,segment_key,clip_id) VALUES(?,?,?)",
                    ((movie_key, key, cursor.lastrowid) for key in clean["keys"]),
                )
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def import_json(self, movie_key: str, legacy_path: str | Path) -> int:
        """Import legacy {clips:[...]} data without editing or removing the file."""
        self._key(movie_key, "movie_key")
        source = Path(legacy_path)
        if not source.exists():
            return 0
        payload = json.loads(source.read_text(encoding="utf-8"))
        clips = payload.get("clips") if isinstance(payload, dict) else payload
        if not isinstance(clips, list):
            raise ValueError("Legacy subtitle data must contain a clips list")
        # Validate first so a malformed source does not produce a partial import.
        validated = [self._record(record)[0] for record in clips]
        return sum(self.save_clip(movie_key, record) for record in validated)

    @staticmethod
    def _source_hash(source_text: str) -> str:
        if not isinstance(source_text, str):
            raise ValueError("source_text must be a string")
        return hashlib.sha256(source_text.encode("utf-8")).hexdigest()

    def get_gloss(self, profile_key: str, source_text: str) -> str | None:
        self._key(profile_key, "profile_key")
        source_hash = self._source_hash(source_text)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT enhanced_text FROM glosses WHERE profile_key=? AND source_hash=? AND source_text=?",
                (profile_key, source_hash, source_text),
            ).fetchone()
        return row[0] if row is not None else None

    def put_gloss(self, profile_key: str, source_text: str, enhanced_text: str,
                  annotations: Any = None, latency_ms: float | None = None) -> None:
        self._key(profile_key, "profile_key")
        source_hash = self._source_hash(source_text)
        if not isinstance(enhanced_text, str):
            raise ValueError("enhanced_text must be a string")
        annotation_json = None if annotations is None else json.dumps(annotations, ensure_ascii=False, allow_nan=False)
        if latency_ms is not None:
            latency_ms = self._seconds(latency_ms, "latency_ms")
        with self._connection() as connection:
            connection.execute("""
                INSERT INTO glosses(profile_key,source_hash,source_text,enhanced_text,annotations_json,latency_ms)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(profile_key,source_hash,source_text) DO UPDATE SET
                    enhanced_text=excluded.enhanced_text,
                    annotations_json=excluded.annotations_json,
                    latency_ms=excluded.latency_ms,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """, (profile_key, source_hash, source_text, enhanced_text, annotation_json, latency_ms))

    def counts(self, movie_key: str | None = None, profile_key: str | None = None) -> dict[str, int]:
        """Count clips/cues/segments for a movie and glosses for a profile.

        Omit either filter to count all records in that respective namespace.
        """
        movie_where, movie_args = ("", ()) if movie_key is None else (" WHERE movie_key=?", (movie_key,))
        gloss_where, gloss_args = ("", ()) if profile_key is None else (" WHERE profile_key=?", (profile_key,))
        with self._connection() as connection:
            connection.execute("BEGIN")
            clips, cues = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(cue_count),0) FROM clips" + movie_where, movie_args
            ).fetchone()
            segments = connection.execute("SELECT COUNT(*) FROM segments" + movie_where, movie_args).fetchone()[0]
            glosses = connection.execute("SELECT COUNT(*) FROM glosses" + gloss_where, gloss_args).fetchone()[0]
            connection.commit()
        return {"clips": clips, "cues": cues, "segments": segments, "glosses": glosses}
