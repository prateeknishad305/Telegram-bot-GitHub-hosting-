from __future__ import annotations

import time
from pathlib import Path

import aiosqlite

from .models import Job, JobStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    repo_url TEXT NOT NULL,
    repo_full_name TEXT NOT NULL,
    branch TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    container_id TEXT,
    image_tag TEXT,
    host_port INTEGER,
    app_port INTEGER,
    run_command TEXT,
    install_commands TEXT,
    base_image TEXT,
    commit_sha TEXT,
    error TEXT,
    log_path TEXT,
    expires_at REAL,
    is_api INTEGER DEFAULT 0,
    health_path TEXT,
    source_kind TEXT DEFAULT 'git',
    release_tag TEXT,
    archive_name TEXT,
    archive_path TEXT,
    requested_port INTEGER,
    build_started_at REAL,
    build_total_steps INTEGER DEFAULT 0,
    build_step INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS metrics (
    job_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

_COLUMNS = [
    "id",
    "user_id",
    "chat_id",
    "repo_url",
    "repo_full_name",
    "branch",
    "status",
    "created_at",
    "updated_at",
    "container_id",
    "image_tag",
    "host_port",
    "app_port",
    "run_command",
    "install_commands",
    "base_image",
    "commit_sha",
    "error",
    "log_path",
    "expires_at",
    "is_api",
    "health_path",
    "source_kind",
    "release_tag",
    "archive_name",
    "archive_path",
    "requested_port",
    "build_started_at",
    "build_total_steps",
    "build_step",
]

_MIGRATION_COLUMNS = {
    "is_api": "INTEGER DEFAULT 0",
    "health_path": "TEXT",
    "source_kind": "TEXT DEFAULT 'git'",
    "release_tag": "TEXT",
    "archive_name": "TEXT",
    "archive_path": "TEXT",
    "requested_port": "INTEGER",
    "build_started_at": "REAL",
    "build_total_steps": "INTEGER DEFAULT 0",
    "build_step": "INTEGER DEFAULT 0",
}


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        cursor = await self.conn.execute("PRAGMA table_info(jobs)")
        existing = {row["name"] for row in await cursor.fetchall()}
        await cursor.close()
        for column, definition in _MIGRATION_COLUMNS.items():
            if column not in existing:
                await self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def save(self, job: Job) -> None:
        placeholders = ", ".join("?" for _ in _COLUMNS)
        updates = ", ".join(f"{col}=excluded.{col}" for col in _COLUMNS if col != "id")
        values = [getattr(job, col) if col != "status" else job.status.value for col in _COLUMNS]
        await self.conn.execute(
            f"INSERT INTO jobs ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            values,
        )
        await self.conn.commit()

    async def get(self, job_id: str) -> Job | None:
        cursor = await self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_job(row) if row else None

    async def list_for_user(self, user_id: int, limit: int = 20) -> list[Job]:
        cursor = await self.conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_job(row) for row in rows]

    async def list_by_status(self, statuses: list[JobStatus]) -> list[Job]:
        marks = ", ".join("?" for _ in statuses)
        cursor = await self.conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks})",
            [s.value for s in statuses],
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_job(row) for row in rows]

    async def list_active(self, user_id: int | None = None) -> list[Job]:
        active = [JobStatus.QUEUED, JobStatus.CLONING, JobStatus.BUILDING, JobStatus.RUNNING]
        marks = ", ".join("?" for _ in active)
        params: list = [s.value for s in active]
        query = f"SELECT * FROM jobs WHERE status IN ({marks})"
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        query += " ORDER BY created_at DESC"
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_job(row) for row in rows]

    async def count_active(self) -> int:
        active = [JobStatus.QUEUED, JobStatus.CLONING, JobStatus.BUILDING, JobStatus.RUNNING]
        marks = ", ".join("?" for _ in active)
        cursor = await self.conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE status IN ({marks})",
            [s.value for s in active],
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0]) if row else 0

    async def fail_stale_jobs(self, reason: str) -> int:
        """Close jobs left active by a previous run.

        Queued/cloning/building jobs cannot resume after a restart, so they
        would otherwise stay "queued" forever and block a concurrency slot.
        Running jobs are marked stopped because their container is gone.
        """
        now = time.time()
        unfinished = [JobStatus.QUEUED, JobStatus.CLONING, JobStatus.BUILDING]
        marks = ", ".join("?" for _ in unfinished)
        cursor = await self.conn.execute(
            f"UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE status IN ({marks})",
            [JobStatus.FAILED.value, reason, now, *[s.value for s in unfinished]],
        )
        failed = cursor.rowcount or 0
        await cursor.close()
        cursor = await self.conn.execute(
            "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE status = ?",
            [JobStatus.STOPPED.value, reason, now, JobStatus.RUNNING.value],
        )
        stopped = cursor.rowcount or 0
        await cursor.close()
        await self.conn.commit()
        return failed + stopped

    async def save_metrics(self, job_id: str, data: str, updated_at: float) -> None:
        await self.conn.execute(
            "INSERT INTO metrics (job_id, data, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (job_id, data, updated_at),
        )
        await self.conn.commit()

    async def load_metrics(self) -> list[tuple[str, str]]:
        cursor = await self.conn.execute("SELECT job_id, data FROM metrics")
        rows = await cursor.fetchall()
        await cursor.close()
        return [(row["job_id"], row["data"]) for row in rows]


def _row_to_job(row: aiosqlite.Row) -> Job:
    data = {key: row[key] for key in row}
    data["status"] = JobStatus(data["status"])
    if not data.get("source_kind"):
        data["source_kind"] = "git"
    return Job(**data)
