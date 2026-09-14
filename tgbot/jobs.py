from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import Settings
from .db import Database
from .detector import RunPlan, detect_plan, render_dockerfile
from .docker_runner import DockerRunner, DockerRunnerError
from .git_client import GitError, clone_repo
from .models import Job, JobStatus, new_job_id
from .monitor import ApiMonitor, format_metrics
from .security import (
    ValidationError,
    check_owner_allowed,
    parse_repo_url,
    redact_secrets,
    validate_branch,
)

NotifyFn = Callable[[int, str], Awaitable[None]]

DOCKERFILE_NAME = ".tgrunner.Dockerfile"


class JobError(RuntimeError):
    pass


class JobManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        runner: DockerRunner,
        notify: NotifyFn,
        monitor: ApiMonitor,
    ):
        self.settings = settings
        self.db = db
        self.runner = runner
        self.notify = notify
        self.monitor = monitor
        self.semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_jobs))
        self._tasks: dict[str, asyncio.Task] = {}
        self._used_ports: set[int] = set()
        self._log_paths: dict[str, Path] = {}
        self._log_buffers: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()
        self._stopping = False

    async def create(
        self,
        user_id: int,
        chat_id: int,
        repo_url: str,
        branch: str | None = None,
    ) -> Job:
        repo = parse_repo_url(repo_url)
        check_owner_allowed(repo, self.settings.allowed_repo_owners)
        branch = validate_branch(branch)

        job = Job(
            id=new_job_id(),
            user_id=user_id,
            chat_id=chat_id,
            repo_url=repo.clone_url,
            repo_full_name=repo.full_name,
            branch=branch,
            status=JobStatus.QUEUED,
            log_path=str(self.settings.work_dir / "pending.log"),
        )
        work = self.settings.work_dir / job.id
        work.mkdir(parents=True, exist_ok=True)
        log_path = work / "build.log"
        job.log_path = str(log_path)
        self._log_paths[job.id] = log_path
        self._log_buffers[job.id] = []

        await self.db.save(job)
        self._tasks[job.id] = asyncio.create_task(self._run(job.id), name=f"job-{job.id}")
        return job

    async def _run(self, job_id: str) -> None:
        async with self.semaphore:
            job = await self.db.get(job_id)
            if job is None:
                return
            try:
                await self._pipeline(job)
            except (GitError, DockerRunnerError, ValidationError, JobError) as exc:
                await self._fail(job, str(exc))
            except asyncio.CancelledError:
                await self._stop_container(job)
                raise
            except Exception as exc:  # noqa: BLE001
                await self._fail(job, f"{type(exc).__name__}: {exc}")

    async def _pipeline(self, job: Job) -> None:
        repo = parse_repo_url(job.repo_url)
        work = self.settings.work_dir / job.id
        repo_dir = work / "repo"

        await self._set_status(job, JobStatus.CLONING)
        await self._log(job, f"==> Cloning {job.repo_full_name}" + (f"@{job.branch}" if job.branch else ""))
        await self.notify(job.chat_id, f"Cloning `{job.repo_full_name}` ...")

        token = self.settings.github_token
        commit = await clone_repo(repo, repo_dir, branch=job.branch, token=token)
        job.commit_sha = commit
        job.branch = job.branch or None
        await self._log(job, f"cloned at {commit[:10] or 'unknown'}")

        plan = detect_plan(repo_dir)
        job.base_image = plan.base_image or "repository Dockerfile"
        job.app_port = plan.app_port
        job.run_command = plan.run_command
        job.install_commands = " && ".join(plan.all_commands) or "(none)"
        job.is_api = plan.is_api
        job.health_path = plan.health_path
        await self.db.save(job)
        await self._log(
            job,
            "==> Detected plan:\n"
            f"    kind    : {plan.kind}\n"
            f"    image   : {plan.base_image or 'repository Dockerfile'}\n"
            f"    install : {job.install_commands}\n"
            f"    run     : {plan.run_command or '(from Dockerfile)'}\n"
            f"    port    : {plan.app_port}\n"
            f"    api     : {plan.is_api}",
        )
        await self.notify(
            job.chat_id,
            f"Detected *{plan.kind}* -> port `{plan.app_port}`\nBuilding image ...",
        )

        await self._set_status(job, JobStatus.BUILDING)
        image_tag = f"tgbot-{job.id}:{(commit[:8] if commit else 'latest') or 'latest'}"
        job.image_tag = image_tag
        await self.db.save(job)

        await asyncio.to_thread(self._build_image, job, repo_dir, plan, image_tag)

        host_port = self._allocate_port()
        job.host_port = host_port
        container_name = f"tgbot-{job.id}"
        container_id = await asyncio.to_thread(
            self.runner.run_container,
            image_tag,
            plan.app_port,
            host_port,
            container_name,
            self.settings.mem_limit,
            self.settings.nano_cpus,
            self.settings.pids_limit,
            {"PORT": str(plan.app_port), "HOST": "0.0.0.0"},
        )
        job.container_id = container_id
        job.status = JobStatus.RUNNING
        job.expires_at = time.time() + self.settings.job_timeout_seconds
        job.updated_at = time.time()
        await self.db.save(job)
        self.monitor.register(job)

        await self._log(job, f"==> Container running: {container_id[:12]} -> 127.0.0.1:{host_port}")
        await self.notify(
            job.chat_id,
            self._running_message(job),
        )
        if job.is_api:
            await self.notify(
                job.chat_id,
                format_metrics(job, self.monitor.snapshot(job.id), self.preview_url(job)),
            )

        asyncio.create_task(self._follow_logs(job), name=f"logs-{job.id}")
        await self._watch(job)

    def _build_image(self, job: Job, repo_dir: Path, plan: RunPlan, image_tag: str) -> None:
        if plan.uses_repo_dockerfile:
            dockerfile = "Dockerfile"
        else:
            dockerfile = DOCKERFILE_NAME
            (repo_dir / dockerfile).write_text(render_dockerfile(plan), encoding="utf-8")
        (repo_dir / ".dockerignore").write_text(
            ".git\n.gitignore\nnode_modules\n__pycache__\n*.pyc\n.venv\nvenv\ntarget\n",
            encoding="utf-8",
        )
        self.runner.build_image(
            context=repo_dir,
            tag=image_tag,
            dockerfile=dockerfile,
            on_log=lambda chunk: self._append_log(job, chunk),
        )

    async def _watch(self, job: Job) -> None:
        while True:
            await asyncio.sleep(5)
            fresh = await self.db.get(job.id)
            if fresh is None or fresh.status != JobStatus.RUNNING:
                return
            try:
                state = await asyncio.to_thread(self.runner.container_state, job.container_id or "")
            except DockerRunnerError:
                state = "unknown"
            if state != "running":
                tail = await asyncio.to_thread(self.runner.get_logs, job.container_id or "", 40)
                await self._fail(job, f"Container exited (state: {state}).\nLast output:\n{tail[-1500:]}")
                return
            if job.expires_at and time.time() > job.expires_at:
                await self._expire(job)
                return

    async def _follow_logs(self, job: Job) -> None:
        if not job.container_id:
            return
        stop_event = _SyncEvent()
        try:
            await asyncio.to_thread(
                self.runner.stream_logs,
                job.container_id,
                lambda line: self._append_log(job, line + "\n"),
                stop_event,
            )
        except asyncio.CancelledError:
            stop_event.set()
            raise
        except Exception:  # noqa: BLE001
            return

    async def _set_status(self, job: Job, status: JobStatus) -> None:
        job.status = status
        job.updated_at = time.time()
        await self.db.save(job)

    async def _fail(self, job: Job, reason: str) -> None:
        job.status = JobStatus.FAILED
        job.error = redact_secrets(reason)[:2000]
        job.updated_at = time.time()
        await self._log(job, f"!!! FAILED: {job.error}")
        await self.db.save(job)
        await self._stop_container(job)
        self._release_port(job.host_port)
        await self.notify(
            job.chat_id,
            f"Job `{job.id}` failed.\n\n```\n{job.error[-1200:]}\n```\n\nUse /logs {job.id} for full output.",
        )

    async def _expire(self, job: Job) -> None:
        job.status = JobStatus.EXPIRED
        job.updated_at = time.time()
        await self._log(job, "=== Job reached its time limit and was stopped")
        await self._stop_container(job)
        self._release_port(job.host_port)
        await self.db.save(job)
        await self.notify(job.chat_id, f"Job `{job.id}` reached its time limit and was stopped.")

    async def stop(self, job: Job, reason: str = "stopped by user") -> Job:
        task = self._tasks.get(job.id)
        if task and not task.done():
            task.cancel()
        await self._stop_container(job)
        self._release_port(job.host_port)
        job.status = JobStatus.STOPPED
        job.error = reason
        job.updated_at = time.time()
        await self.db.save(job)
        await self._log(job, f"=== stopped ({reason})")
        return job

    async def _stop_container(self, job: Job) -> None:
        if not job.container_id:
            return
        try:
            await asyncio.to_thread(self.runner.stop_container, job.container_id)
            await asyncio.to_thread(self.runner.remove_container, job.container_id)
        except DockerRunnerError:
            pass
        if job.image_tag:
            try:
                await asyncio.to_thread(self.runner.remove_image, job.image_tag)
            except DockerRunnerError:
                pass

    def _allocate_port(self) -> int:
        port = DockerRunner.find_free_port(
            self.settings.port_range_start, self.settings.port_range_end, self._used_ports
        )
        self._used_ports.add(port)
        return port

    def _release_port(self, port: int | None) -> None:
        if port is not None:
            self._used_ports.discard(port)

    def _running_message(self, job: Job) -> str:
        preview = self.preview_url(job)
        kind = "API" if job.is_api else "app"
        extra = f"\nAPI details: /api {job.id}" if job.is_api else ""
        return (
            f"Job `{job.id}` is *running* ({kind}).\n\n"
            f"Repo: `{job.repo_full_name}`\n"
            f"Port: `{job.app_port}` (host `{job.host_port}`)\n"
            f"Run: `{job.run_command}`\n\n"
            f"Preview: {preview}{extra}\n"
            f"Open the Mini App for the live terminal: {self.settings.public_base}/\n\n"
            f"Auto-stops in {self.settings.job_timeout_seconds // 60} min. Use /stop {job.id} to stop now."
        )

    def preview_url(self, job: Job) -> str:
        if self.settings.preview_base_domain:
            scheme = "https" if self.settings.public_url.startswith("https") else "http"
            return f"{scheme}://{job.id}.{self.settings.preview_base_domain.strip('.')}/"
        return f"{self.settings.public_base}/preview/{job.id}/"

    def api_details(self, job: Job) -> str:
        return format_metrics(job, self.monitor.snapshot(job.id), self.preview_url(job))

    def metrics(self, job: Job) -> dict | None:
        return self.monitor.snapshot(job.id)

    async def _log(self, job: Job, text: str) -> None:
        self._append_log(job, f"{text}\n")

    def _append_log(self, job: Job, text: str) -> None:
        path = self._log_paths.get(job.id) or (Path(job.log_path) if job.log_path else None)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(redact_secrets(text))
        except OSError:
            return
        buffer = self._log_buffers.setdefault(job.id, [])
        buffer.append(text)
        if len(buffer) > 400:
            del buffer[:200]

    def read_log(self, job: Job, max_chars: int = 3500) -> str:
        if not job.log_path:
            return "(no log yet)"
        try:
            content = Path(job.log_path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return "(no log yet)"
        if len(content) > max_chars:
            return "..." + content[-max_chars:]
        return content or "(no log yet)"

    async def active_count(self) -> int:
        return len([t for t in self._tasks.values() if not t.done()])

    async def shutdown(self) -> None:
        self._stopping = True
        for task in list(self._tasks.values()):
            task.cancel()
        for job in await self.db.list_active():
            await self._stop_container(job)
            job.status = JobStatus.STOPPED
            job.error = "bot shutting down"
            job.updated_at = time.time()
            await self.db.save(job)


class _SyncEvent:
    def __init__(self) -> None:
        self._flag = False
        import threading

        self._lock = threading.Lock()

    def set(self) -> None:
        with self._lock:
            self._flag = True

    def is_set(self) -> bool:
        with self._lock:
            return self._flag
