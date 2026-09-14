from __future__ import annotations

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .config import Settings
from .jobs import JobManager
from .models import Job, JobStatus
from .security import ValidationError

logger = logging.getLogger(__name__)

USAGE = (
    "Send me a GitHub repository and I will clone it, detect how to build it, "
    "install dependencies and run it in a sandbox.\n\n"
    "Commands:\n"
    "/run <repo_url> [branch] - start a repository\n"
    "/jobs - list your jobs\n"
    "/status <job_id> - job details\n"
    "/api <job_id> - API link, uptime, requests and health\n"
    "/logs <job_id> - recent build/run output\n"
    "/stop <job_id> - stop a running job\n"
    "/open - open the Mini App (terminal + preview + metrics)\n"
    "/help - show this help\n\n"
    "Tip: add a .tgrunner.yml to your repo to control base image, install, build, run, "
    "port, api and health path."
)


def _manager(context: ContextTypes.DEFAULT_TYPE) -> JobManager:
    return context.application.bot_data["manager"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _mini_app_button(settings: Settings) -> InlineKeyboardMarkup | None:
    if not settings.public_base.startswith("https://"):
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Open Mini App", web_app=WebAppInfo(url=f"{settings.public_base}/"))]]
    )


async def _deny(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    if user is None:
        return True
    if not settings.is_user_allowed(user.id):
        if update.effective_message:
            await update.effective_message.reply_text(
                "You are not authorised to use this bot. Ask the operator to add your user id "
                f"({user.id}) to ALLOWED_USER_IDS."
            )
        return True
    return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    if await _deny(update, settings):
        return
    await update.effective_message.reply_text(
        "Hi. I run GitHub repositories, installing dependencies automatically.\n\n" + USAGE,
        reply_markup=_mini_app_button(settings),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    if await _deny(update, settings):
        return
    markup = _mini_app_button(settings)
    if markup is None:
        await update.effective_message.reply_text(
            f"Mini App URL (needs HTTPS): {settings.public_base}/"
        )
        return
    await update.effective_message.reply_text("Open the Mini App for the live terminal:", reply_markup=markup)


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    if await _deny(update, settings):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /run <github_repo_url> [branch]")
        return
    repo_url = context.args[0]
    branch = context.args[1] if len(context.args) > 1 else None
    try:
        job = await _manager(context).create(
            user_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            repo_url=repo_url,
            branch=branch,
        )
    except ValidationError as exc:
        await update.effective_message.reply_text(f"Cannot run this repository: {exc}")
        return
    await update.effective_message.reply_text(
        f"Queued job `{job.id}` for `{job.repo_full_name}`.\nI will report progress here.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    if await _deny(update, settings):
        return
    jobs = await _manager(context).db.list_for_user(update.effective_user.id, 15)
    if not jobs:
        await update.effective_message.reply_text("You have no jobs yet. Use /run <repo_url>.")
        return
    lines = []
    for job in jobs:
        preview = _manager(context).preview_url(job) if job.status == JobStatus.RUNNING else "-"
        lines.append(
            f"{job.status.emoji} `{job.id}` {job.repo_full_name} "
            f"[{job.status.value}]\n   preview: {preview}"
        )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def _get_job(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Job | None:
    settings = _settings(context)
    if not context.args:
        await update.effective_message.reply_text("Usage: provide a job id, e.g. /status abc123")
        return None
    job = await _manager(context).db.get(context.args[0].strip())
    if job is None:
        await update.effective_message.reply_text("No such job.")
        return None
    if job.user_id != update.effective_user.id and not settings.is_admin(update.effective_user.id):
        await update.effective_message.reply_text("This job belongs to another user.")
        return None
    return job


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    if await _deny(update, settings):
        return
    job = await _get_job(update, context)
    if job is None:
        return
    manager = _manager(context)
    preview = manager.preview_url(job) if job.status == JobStatus.RUNNING else "-"
    text = (
        f"*{job.repo_full_name}* `{job.id}`\n"
        f"status  : `{job.status.value}`\n"
        f"branch  : `{job.branch or '(default)'}`\n"
        f"commit  : `{(job.commit_sha or '')[:10]}`\n"
        f"kind    : `{job.base_image or ''}`\n"
        f"type    : `{'api' if job.is_api else 'web'}`\n"
        f"run     : `{job.run_command or ''}`\n"
        f"port    : `{job.app_port}`\n"
        f"preview : {preview}\n"
    )
    if job.error:
        text += f"\nerror   : {job.error[:500]}"
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_api(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    if await _deny(update, settings):
        return
    job = await _get_job(update, context)
    if job is None:
        return
    if job.status != JobStatus.RUNNING:
        await update.effective_message.reply_text(f"Job `{job.id}` is not running ({job.status.value}).")
        return
    await update.effective_message.reply_text(
        _manager(context).api_details(job), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    if await _deny(update, settings):
        return
    job = await _get_job(update, context)
    if job is None:
        return
    content = _manager(context).read_log(job, 3500).replace("```", "'''")
    for chunk_start in range(0, max(len(content), 1), 3500):
        chunk = content[chunk_start : chunk_start + 3500]
        await update.effective_message.reply_text(f"```\n{chunk}\n```", parse_mode=ParseMode.MARKDOWN)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    if await _deny(update, settings):
        return
    job = await _get_job(update, context)
    if job is None:
        return
    if job.status.terminal:
        await update.effective_message.reply_text(f"Job `{job.id}` is already {job.status.value}.")
        return
    await _manager(context).stop(job, reason="stopped from Telegram")
    await update.effective_message.reply_text(f"Stopped job `{job.id}`.", parse_mode=ParseMode.MARKDOWN)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Handler error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something went wrong while processing your request.")
