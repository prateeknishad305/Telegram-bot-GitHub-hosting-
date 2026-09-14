from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field

import aiohttp

from .config import Settings
from .db import Database
from .models import Job, JobStatus

logger = logging.getLogger(__name__)

DEFAULT_HEALTH_PATHS = ["/health", "/healthz", "/api/health", "/ping", "/status", "/"]

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
UNKNOWN = "unknown"


@dataclass
class JobMetrics:
    job_id: str
    started_at: float
    is_api: bool = False
    health_path: str | None = None
    requests_total: int = 0
    requests_ok: int = 0
    requests_client_error: int = 0
    requests_failed: int = 0
    total_latency_ms: float = 0.0
    bytes_out: int = 0
    last_request_at: float | None = None
    last_status: int | None = None
    last_request_path: str | None = None
    checks: int = 0
    checks_up: int = 0
    last_check_at: float | None = None
    last_probe_latency_ms: float | None = None
    healthy: bool | None = None
    history: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> JobMetrics | None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or "job_id" not in data:
            return None
        allowed = {f for f in cls.__dataclass_fields__}
        clean = {key: value for key, value in data.items() if key in allowed}
        clean.setdefault("started_at", time.time())
        try:
            return cls(**clean)
        except TypeError:
            return None

    def snapshot(self, now: float | None = None) -> dict:
        now = now or time.time()
        uptime_seconds = max(0.0, now - self.started_at)
        total = self.requests_total
        failed = self.requests_failed
        availability = (self.checks_up / self.checks * 100.0) if self.checks else 100.0
        if self.healthy is None:
            health = UNKNOWN
        else:
            health = HEALTHY if self.healthy else UNHEALTHY
        return {
            "job_id": self.job_id,
            "is_api": self.is_api,
            "health": health,
            "uptime_seconds": round(uptime_seconds, 1),
            "started_at": self.started_at,
            "requests_total": total,
            "requests_ok": self.requests_ok,
            "requests_client_error": self.requests_client_error,
            "requests_failed": failed,
            "error_rate": round((failed / total * 100.0), 2) if total else 0.0,
            "avg_latency_ms": round(self.total_latency_ms / total, 1) if total else None,
            "last_latency_ms": round(self.last_probe_latency_ms, 1) if self.last_probe_latency_ms else None,
            "last_status": self.last_status,
            "last_request_at": self.last_request_at,
            "last_request_path": self.last_request_path,
            "health_checks": self.checks,
            "health_checks_up": self.checks_up,
            "availability_percent": round(availability, 2),
            "last_check_at": self.last_check_at,
            "health_path": self.health_path,
        }


class ApiMonitor:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._metrics: dict[str, JobMetrics] = {}
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._last_flush = 0.0

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.health_check_timeout, sock_connect=3)
        )
        await self._load()
        self._task = asyncio.create_task(self._loop(), name="api-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush()
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _load(self) -> None:
        try:
            rows = await self.db.load_metrics()
        except Exception:  # noqa: BLE001
            return
        for job_id, raw in rows:
            metrics = JobMetrics.from_json(raw)
            if metrics is not None:
                self._metrics[job_id] = metrics

    def register(self, job: Job) -> JobMetrics:
        metrics = self._metrics.get(job.id)
        if metrics is None:
            metrics = JobMetrics(job_id=job.id, started_at=time.time(), is_api=job.is_api)
            self._metrics[job.id] = metrics
        metrics.is_api = job.is_api
        metrics.started_at = time.time()
        metrics.healthy = None
        metrics.health_path = job.health_path
        return metrics

    def drop(self, job_id: str) -> None:
        self._metrics.pop(job_id, None)

    def record_request(self, job_id: str, path: str, status: int, latency_ms: float, failed: bool = False) -> None:
        metrics = self._metrics.get(job_id)
        if metrics is None:
            return
        metrics.requests_total += 1
        metrics.total_latency_ms += max(0.0, latency_ms)
        metrics.last_request_at = time.time()
        metrics.last_status = status or None
        metrics.last_request_path = path
        if failed or status == 0 or status >= 500:
            metrics.requests_failed += 1
        elif status >= 400:
            metrics.requests_client_error += 1
        else:
            metrics.requests_ok += 1

    def get(self, job_id: str) -> JobMetrics | None:
        return self._metrics.get(job_id)

    def snapshot(self, job_id: str) -> dict | None:
        metrics = self._metrics.get(job_id)
        if metrics is None:
            return None
        return metrics.snapshot()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.health_check_interval)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Health monitor tick failed")

    async def _tick(self) -> None:
        for job in await self.db.list_active():
            if job.status == JobStatus.RUNNING and job.host_port:
                if job.id not in self._metrics:
                    self.register(job)
                await self._probe(job)
        if time.monotonic() - self._last_flush >= self.settings.metrics_flush_interval:
            await self.flush()

    async def _probe(self, job: Job) -> None:
        metrics = self._metrics.get(job.id)
        if metrics is None or self._session is None:
            return

        candidates: list[str] = []
        if metrics.health_path:
            candidates.append(metrics.health_path)
        candidates.extend(path for path in DEFAULT_HEALTH_PATHS if path not in candidates)

        status: int | None = None
        latency: float | None = None
        for path in candidates:
            status, latency = await self._http_probe(job.host_port or 0, path)
            if status is not None and status < 500:
                metrics.health_path = path
                break

        if metrics.health_path is None and candidates:
            metrics.health_path = candidates[-1]

        up = status is not None and status < 500
        metrics.checks += 1
        if up:
            metrics.checks_up += 1
        metrics.healthy = up
        metrics.last_check_at = time.time()
        metrics.last_probe_latency_ms = latency
        metrics.history.append({"t": metrics.last_check_at, "up": up, "ms": latency, "code": status})
        if len(metrics.history) > self.settings.health_history_size:
            del metrics.history[: len(metrics.history) - self.settings.health_history_size]

    async def _http_probe(self, host_port: int, path: str) -> tuple[int | None, float | None]:
        if not host_port or self._session is None:
            return None, None
        url = f"http://127.0.0.1:{host_port}{path}"
        started = time.monotonic()
        try:
            async with self._session.get(url, allow_redirects=False) as response:
                await response.read()
                return response.status, (time.monotonic() - started) * 1000.0
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return None, (time.monotonic() - started) * 1000.0

    async def flush(self) -> None:
        self._last_flush = time.monotonic()
        now = time.time()
        for job_id, metrics in list(self._metrics.items()):
            try:
                await self.db.save_metrics(job_id, metrics.to_json(), now)
            except Exception:  # noqa: BLE001
                logger.warning("Could not persist metrics for %s", job_id)


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_metrics(job: Job, metrics: dict | None, api_url: str) -> str:
    if metrics is None:
        return f"API link: {api_url}\n\nNo metrics collected yet. Try again in a few seconds."

    health_line = {
        HEALTHY: "healthy",
        UNHEALTHY: "unhealthy",
        UNKNOWN: "unknown",
    }.get(metrics["health"], metrics["health"])

    last_request = "-"
    if metrics.get("last_request_at"):
        age = time.time() - metrics["last_request_at"]
        last_request = f"{format_duration(age)} ago"
        if metrics.get("last_status") is not None:
            last_request += f" (HTTP {metrics['last_status']})"

    latency = metrics.get("avg_latency_ms")
    latency_line = f"{latency} ms avg" if latency is not None else "n/a"
    if metrics.get("last_latency_ms") is not None:
        latency_line += f", {metrics['last_latency_ms']} ms last probe"

    return (
        f"*API: {job.repo_full_name}* (`{job.id}`)\n\n"
        f"Link     : {api_url}\n"
        f"Docs/health: {api_url.rstrip('/')}{(metrics.get('health_path') or '/health')}\n"
        f"Health   : *{health_line}*\n"
        f"Uptime   : {format_duration(metrics['uptime_seconds'])} "
        f"({metrics['availability_percent']}% checks ok)\n"
        f"Requests : {metrics['requests_total']} total, "
        f"{metrics['requests_failed']} failed, "
        f"{metrics['requests_client_error']} 4xx\n"
        f"Error rate: {metrics['error_rate']}%\n"
        f"Latency  : {latency_line}\n"
        f"Last req : {last_request}\n"
        f"Checks   : {metrics['health_checks_up']}/{metrics['health_checks']} up"
    )
