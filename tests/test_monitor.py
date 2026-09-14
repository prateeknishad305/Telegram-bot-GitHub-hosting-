import socket
import tempfile
from pathlib import Path

import pytest
from aiohttp import web

from tgbot.config import Settings
from tgbot.db import Database
from tgbot.models import Job, JobStatus
from tgbot.monitor import HEALTHY, UNHEALTHY, ApiMonitor, format_duration


async def _start_fake_api():
    app = web.Application()

    async def health(request):
        return web.json_response({"status": "ok"})

    async def boom(request):
        return web.json_response({"error": "nope"}, status=500)

    app.router.add_get("/health", health)
    app.router.add_get("/boom", boom)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


@pytest.fixture
async def env():
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        TELEGRAM_BOT_TOKEN="123:ABC",
        DATA_DIR=str(tmp),
        HEALTH_CHECK_INTERVAL=3600,
        HEALTH_CHECK_TIMEOUT=5,
        METRICS_FLUSH_INTERVAL=1,
    )
    settings.ensure_dirs()
    db = Database(settings.db_path)
    await db.connect()
    runner, port = await _start_fake_api()
    yield settings, db, port
    await runner.cleanup()
    await db.close()


def _running_job(port: int) -> Job:
    return Job(
        id="job123",
        user_id=1,
        chat_id=1,
        repo_url="https://github.com/o/r.git",
        repo_full_name="o/r",
        status=JobStatus.RUNNING,
        host_port=port,
        app_port=8000,
        is_api=True,
    )


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_probe_detects_healthy_api(env):
    settings, db, port = env
    job = _running_job(port)
    await db.save(job)

    monitor = ApiMonitor(settings, db)
    await monitor.start()
    try:
        monitor.register(job)
        await monitor._probe(job)
        snapshot = monitor.snapshot(job.id)
    finally:
        await monitor.stop()

    assert snapshot is not None
    assert snapshot["health"] == HEALTHY
    assert snapshot["health_path"] == "/health"
    assert snapshot["availability_percent"] == 100.0
    assert snapshot["health_checks"] == 1


async def test_probe_detects_unhealthy_when_all_fail(env):
    settings, db, _port = env
    job = _running_job(_closed_port())
    await db.save(job)

    monitor = ApiMonitor(settings, db)
    await monitor.start()
    try:
        monitor.register(job)
        await monitor._probe(job)
        snapshot = monitor.snapshot(job.id)
    finally:
        await monitor.stop()

    assert snapshot is not None
    assert snapshot["health"] == UNHEALTHY
    assert snapshot["availability_percent"] == 0.0


async def test_request_counters(env):
    settings, db, port = env
    job = _running_job(port)
    await db.save(job)

    monitor = ApiMonitor(settings, db)
    await monitor.start()
    try:
        monitor.register(job)
        monitor.record_request(job.id, "/health", 200, 12.0)
        monitor.record_request(job.id, "/health", 200, 8.0)
        monitor.record_request(job.id, "/boom", 500, 30.0)
        monitor.record_request(job.id, "/missing", 404, 5.0)
        monitor.record_request(job.id, "/health", 0, 1.0, failed=True)
        snapshot = monitor.snapshot(job.id)
    finally:
        await monitor.stop()

    assert snapshot["requests_total"] == 5
    assert snapshot["requests_ok"] == 2
    assert snapshot["requests_failed"] == 2
    assert snapshot["requests_client_error"] == 1
    assert snapshot["error_rate"] == 40.0
    assert snapshot["avg_latency_ms"] == 11.2


async def test_metrics_persisted_and_reloaded(env):
    settings, db, port = env
    job = _running_job(port)
    await db.save(job)

    monitor = ApiMonitor(settings, db)
    await monitor.start()
    monitor.register(job)
    monitor.record_request(job.id, "/health", 200, 10.0)
    await monitor.flush()
    await monitor.stop()

    reloaded = ApiMonitor(settings, db)
    await reloaded.start()
    try:
        snapshot = reloaded.snapshot(job.id)
    finally:
        await reloaded.stop()
    assert snapshot is not None
    assert snapshot["requests_total"] == 1


def test_format_duration():
    assert format_duration(0) == "0s"
    assert format_duration(45) == "45s"
    assert format_duration(125) == "2m 5s"
    assert format_duration(3725) == "1h 2m 5s"
    assert format_duration(90061) == "1d 1h 1m"
