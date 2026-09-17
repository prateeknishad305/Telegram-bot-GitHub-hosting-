from __future__ import annotations

import asyncio
import logging
import os

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler

from .config import Settings, get_settings
from .db import Database
from .docker_runner import DockerRunner, DockerRunnerError
from .handlers import (
    cmd_api,
    cmd_cancel,
    cmd_download,
    cmd_help,
    cmd_info,
    cmd_jobs,
    cmd_logs,
    cmd_open,
    cmd_run,
    cmd_start,
    cmd_status,
    cmd_stop,
    on_error,
)
from .jobs import JobManager
from .maintenance import MaintenanceLoop
from .monitor import ApiMonitor
from .webapp import WebServer

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("tgbot")


async def _notify(application: Application, chat_id: int, text: str) -> None:
    try:
        await application.bot.send_message(chat_id=chat_id, text=text[:4000], parse_mode="Markdown")
    except Exception:  # noqa: BLE001
        try:
            await application.bot.send_message(chat_id=chat_id, text=text[:4000])
        except Exception:  # noqa: BLE001
            logger.warning("Could not deliver notification to chat %s", chat_id)


async def post_init(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]

    db = Database(settings.db_path)
    await db.connect()

    runner = DockerRunner(settings.docker_network, settings.resolved_docker_host())
    try:
        await asyncio.to_thread(runner.ensure_network)
        logger.info("Using Docker daemon at %s", runner.docker_host)
    except DockerRunnerError as exc:
        logger.warning("Docker is not ready yet: %s", exc)

    async def notify(chat_id: int, text: str) -> None:
        await _notify(application, chat_id, text)

    monitor = ApiMonitor(settings, db)
    await monitor.start()

    manager = JobManager(settings, db, runner, notify, monitor)
    recovered = await manager.recover_stale_jobs()
    if recovered:
        logger.info("Recovered %s stale job(s) from a previous run", recovered)

    maintenance = MaintenanceLoop(settings, db)
    await maintenance.start()

    server = WebServer(settings, db, manager, runner, monitor, maintenance)
    await server.start()

    application.bot_data.update(
        db=db,
        runner=runner,
        manager=manager,
        server=server,
        monitor=monitor,
        maintenance=maintenance,
    )
    asyncio.create_task(_restart_watcher(application), name="restart-watcher")
    logger.info("Mini App and runner API listening on %s:%s", settings.web_host, settings.web_port)


async def _restart_watcher(application: Application) -> None:
    """Exit the process when maintenance asks for a restart and a supervisor is present."""
    maintenance: MaintenanceLoop = application.bot_data["maintenance"]
    while True:
        await asyncio.sleep(30)
        if maintenance.restart_requested:
            logger.error("Disk still full after cleanup; exiting so the supervisor restarts the bot")
            asyncio.get_running_loop().call_later(1, os._exit, 1)
            return


async def post_shutdown(application: Application) -> None:
    manager: JobManager | None = application.bot_data.get("manager")
    if manager is not None:
        await manager.shutdown()
    maintenance: MaintenanceLoop | None = application.bot_data.get("maintenance")
    if maintenance is not None:
        await maintenance.stop()
    server: WebServer | None = application.bot_data.get("server")
    if server is not None:
        await server.stop()
    monitor: ApiMonitor | None = application.bot_data.get("monitor")
    if monitor is not None:
        await monitor.stop()
    db: Database | None = application.bot_data.get("db")
    if db is not None:
        await db.close()
    logger.info("Shutdown complete")


def build_application(settings: Settings) -> Application:
    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("run", cmd_run))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("jobs", cmd_jobs))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("api", cmd_api))
    application.add_handler(CommandHandler("download", cmd_download))
    application.add_handler(CommandHandler("info", cmd_info))
    application.add_handler(CommandHandler("logs", cmd_logs))
    application.add_handler(CommandHandler("stop", cmd_stop))
    application.add_handler(CommandHandler("open", cmd_open))
    application.add_error_handler(on_error)
    return application


def main() -> None:
    settings = get_settings()
    if not settings.allowed_user_ids:
        logger.warning(
            "ALLOWED_USER_IDS is empty: anyone who finds this bot can execute code on this host."
        )
    application = build_application(settings)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
