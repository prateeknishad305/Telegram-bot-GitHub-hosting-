from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .db import Database
from .models import JobStatus

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {JobStatus.FAILED, JobStatus.STOPPED, JobStatus.EXPIRED}


@dataclass
class DiskUsage:
    total: int
    used: int
    free: int
    percent: float

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "used": self.used,
            "free": self.free,
            "percent": round(self.percent, 1),
        }


def format_bytes(num: int) -> str:
    value = float(max(0, num))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def disk_usage(path: Path) -> DiskUsage:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        usage = shutil.disk_usage("/")
    percent = (usage.used / usage.total * 100.0) if usage.total else 0.0
    return DiskUsage(total=usage.total, used=usage.used, free=usage.free, percent=percent)


def dir_size(path: Path, limit_seconds: float = 5.0) -> int:
    total = 0
    deadline = time.monotonic() + limit_seconds
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            for entry in current.iterdir():
                if time.monotonic() > deadline:
                    return total
                if entry.is_dir() and not entry.is_symlink():
                    stack.append(entry)
                elif entry.is_file():
                    total += entry.stat().st_size
        except OSError:
            continue
    return total


def cache_size(work_dir: Path) -> int:
    return dir_size(work_dir) if work_dir.exists() else 0


def list_cache_dirs(work_dir: Path) -> list[tuple[str, Path, float]]:
    if not work_dir.exists():
        return []
    entries: list[tuple[str, Path, float]] = []
    for entry in work_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            entries.append((entry.name, entry, entry.stat().st_mtime))
        except OSError:
            continue
    return entries


def cleanup_work_dir(work_dir: Path, max_age_seconds: int) -> tuple[int, int]:
    """Remove build directories of finished jobs older than max_age_seconds.

    Returns (removed_count, freed_bytes). Only the runner's own cache directory
    is touched; archives still referenced by an active job are left alone.
    """
    if max_age_seconds <= 0:
        return 0, 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    freed = 0
    for _job_id, path, mtime in list_cache_dirs(work_dir):
        if mtime > cutoff:
            continue
        size = dir_size(path)
        try:
            shutil.rmtree(path)
            removed += 1
            freed += size
        except OSError as exc:
            logger.warning("Could not clean cache dir %s: %s", path, exc)
    return removed, freed


class MaintenanceLoop:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._task: asyncio.Task | None = None
        self.last_cleanup_at: float | None = None
        self.last_removed: int = 0
        self.last_freed: int = 0
        self.last_disk: DiskUsage | None = None
        self.restart_requested: bool = False

    async def start(self) -> None:
        self.last_disk = await asyncio.to_thread(disk_usage, self.settings.data_dir)
        self._task = asyncio.create_task(self._loop(), name="maintenance")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.cache_cleanup_interval_seconds)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Maintenance tick failed")

    async def tick(self) -> dict:
        before = await asyncio.to_thread(disk_usage, self.settings.data_dir)
        removed, freed = await asyncio.to_thread(
            cleanup_work_dir, self.settings.work_dir, self.settings.cache_max_age_seconds
        )
        after = await asyncio.to_thread(disk_usage, self.settings.data_dir)

        self.last_cleanup_at = time.time()
        self.last_removed = removed
        self.last_freed = freed
        self.last_disk = after
        logger.info(
            "Cache cleanup: removed %s dirs, freed %s (disk %.1f%% used)",
            removed,
            format_bytes(freed),
            after.percent,
        )

        if (
            self.settings.restart_on_disk_full
            and after.percent >= self.settings.disk_restart_percent
            and after.percent >= before.percent
        ):
            self.restart_requested = True
            logger.error(
                "Disk is %.1f%% full and cleanup did not help; requesting restart",
                after.percent,
            )
        return {
            "removed": removed,
            "freed": freed,
            "disk": after.as_dict(),
            "restart_requested": self.restart_requested,
        }

    def snapshot(self) -> dict:
        disk = self.last_disk or disk_usage(self.settings.data_dir)
        return {
            "disk": disk.as_dict(),
            "work_dir": str(self.settings.work_dir),
            "cache_bytes": cache_size(self.settings.work_dir),
            "cleanup_interval_seconds": self.settings.cache_cleanup_interval_seconds,
            "cache_max_age_seconds": self.settings.cache_max_age_seconds,
            "last_cleanup_at": self.last_cleanup_at,
            "last_removed": self.last_removed,
            "last_freed": self.last_freed,
            "restart_requested": self.restart_requested,
        }
