from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import ClientSession, web

from .config import Settings
from .db import Database
from .docker_runner import DockerRunner
from .jobs import JobManager
from .models import Job, JobStatus
from .monitor import ApiMonitor
from .proxy import is_websocket, proxy_http, proxy_websocket
from .security import (
    ValidationError,
    create_session_token,
    validate_init_data,
    verify_session_token,
)
from .terminal import bridge_terminal

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class AuthError(Exception):
    pass


class WebServer:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        manager: JobManager,
        runner: DockerRunner,
        monitor: ApiMonitor,
    ):
        self.settings = settings
        self.db = db
        self.manager = manager
        self.runner = runner
        self.monitor = monitor
        self.app = web.Application(middlewares=[self._preview_host_middleware], client_max_size=4 * 1024 * 1024)
        self.app["settings"] = settings
        self.app["db"] = db
        self.app["manager"] = manager
        self.app["runner"] = runner
        self.app["monitor"] = monitor
        self._runner: web.AppRunner | None = None
        self._session: ClientSession | None = None
        self._setup_routes()

    def _setup_routes(self) -> None:
        routes = self.app.router
        routes.add_get("/", self._index)
        routes.add_get("/favicon.ico", self._favicon)
        routes.add_get("/healthz", self._health)
        routes.add_static("/static/", WEB_DIR)
        routes.add_post("/api/session", self._api_session)
        routes.add_get("/api/me", self._api_me)
        routes.add_route("GET", "/api/jobs", self._api_list_jobs)
        routes.add_post("/api/jobs", self._api_create_job)
        routes.add_get("/api/jobs/{job_id}", self._api_job_detail)
        routes.add_get("/api/jobs/{job_id}/metrics", self._api_job_metrics)
        routes.add_post("/api/jobs/{job_id}/stop", self._api_stop_job)
        routes.add_get("/ws/terminal/{job_id}", self._terminal)
        routes.add_route("*", "/preview/{job_id}/{tail:.*}", self._preview)

    async def start(self) -> None:
        self._session = ClientSession()
        self.app["http"] = self._session
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.settings.web_host, self.settings.web_port)
        await site.start()

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
        if self._runner is not None:
            await self._runner.cleanup()

    @web.middleware
    async def _preview_host_middleware(self, request: web.Request, handler):
        base = self.settings.preview_base_domain.strip(".")
        if base:
            host = request.host.split(":")[0].lower()
            if host.endswith("." + base):
                subdomain = host[: -(len(base) + 1)].split(".")[0]
                job = await self.db.get(subdomain)
                if job and job.status == JobStatus.RUNNING and job.host_port:
                    tail = request.path.lstrip("/")
                    if is_websocket(request):
                        return await proxy_websocket(request, job.host_port, tail)
                    return await proxy_http(request, job.host_port, tail, job_id=job.id)
        return await handler(request)

    def _user_id(self, request: web.Request) -> int:
        init_data = request.headers.get("X-Telegram-Init-Data") or request.query.get("init")
        if init_data:
            result = validate_init_data(init_data, self.settings.telegram_bot_token)
            if result:
                return int(result["user"]["id"])
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            user_id = verify_session_token(authorization[7:].strip(), self.settings.secret())
            if user_id is not None:
                return user_id
        raise web.HTTPUnauthorized(text="Authentication required")
    async def _require_allowed(self, request: web.Request) -> int:
        user_id = self._user_id(request)
        if not self.settings.is_user_allowed(user_id):
            raise web.HTTPForbidden(text="You are not allowed to use this bot")
        return user_id

    async def _index(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEB_DIR / "index.html")

    async def _favicon(self, request: web.Request) -> web.Response:
        return web.Response(status=204)

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "active_jobs": await self.db.count_active()})

    async def _api_session(self, request: web.Request) -> web.Response:
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        result = validate_init_data(init_data, self.settings.telegram_bot_token)
        if result:
            user = result["user"]
            user_id = int(user["id"])
            display = {
                "id": user_id,
                "first_name": user.get("first_name", ""),
                "username": user.get("username", ""),
            }
        elif self.settings.dev_login_user_id and request.query.get("dev") == "1":
            user_id = self.settings.dev_login_user_id
            display = {"id": user_id, "first_name": "dev", "username": "dev"}
        else:
            return web.json_response({"error": "invalid init data"}, status=401)
        if not self.settings.is_user_allowed(user_id):
            return web.json_response({"error": "not allowed"}, status=403)
        return web.json_response(
            {"token": create_session_token(user_id, self.settings.secret()), "user": display}
        )

    async def _api_me(self, request: web.Request) -> web.Response:
        user_id = await self._require_allowed(request)
        return web.json_response({"id": user_id, "is_admin": self.settings.is_admin(user_id)})

    async def _api_list_jobs(self, request: web.Request) -> web.Response:
        user_id = await self._require_allowed(request)
        jobs = await self.db.list_for_user(user_id, 30)
        return web.json_response({"jobs": [self._serialize(job) for job in jobs]})

    async def _api_create_job(self, request: web.Request) -> web.Response:
        user_id = await self._require_allowed(request)
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")
        try:
            job = await self.manager.create(
                user_id=user_id,
                chat_id=user_id,
                repo_url=str(data.get("repo_url", "")),
                branch=data.get("branch") or None,
            )
        except ValidationError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response({"job": self._serialize(job)}, status=201)

    async def _api_job_detail(self, request: web.Request) -> web.Response:
        user_id = await self._require_allowed(request)
        job = await self.db.get(request.match_info["job_id"])
        if job is None or not self._can_access(job, user_id):
            raise web.HTTPNotFound(text="Job not found")
        payload = self._serialize(job)
        payload["log_tail"] = self.manager.read_log(job, 4000)
        return web.json_response({"job": payload})

    async def _api_job_metrics(self, request: web.Request) -> web.Response:
        user_id = await self._require_allowed(request)
        job = await self.db.get(request.match_info["job_id"])
        if job is None or not self._can_access(job, user_id):
            raise web.HTTPNotFound(text="Job not found")
        return web.json_response(
            {
                "job_id": job.id,
                "status": job.status.value,
                "is_api": job.is_api,
                "metrics": self.manager.metrics(job),
            }
        )

    async def _api_stop_job(self, request: web.Request) -> web.Response:
        user_id = await self._require_allowed(request)
        job = await self.db.get(request.match_info["job_id"])
        if job is None or not self._can_access(job, user_id):
            raise web.HTTPNotFound(text="Job not found")
        if job.status.terminal:
            return web.json_response({"job": self._serialize(job)})
        await self.manager.stop(job, reason="stopped from Mini App")
        return web.json_response({"job": self._serialize(job)})

    async def _terminal(self, request: web.Request) -> web.WebSocketResponse:
        token = request.query.get("token") or request.query.get("init") or ""
        user_id = verify_session_token(token, self.settings.secret())
        if user_id is None and token:
            result = validate_init_data(token, self.settings.telegram_bot_token)
            if result:
                user_id = int(result["user"]["id"])
        if user_id is None or not self.settings.is_user_allowed(user_id):
            raise web.HTTPUnauthorized(text="Authentication required")
        if not self.settings.enable_terminal:
            raise web.HTTPForbidden(text="Terminal is disabled")

        job = await self.db.get(request.match_info["job_id"])
        if job is None or not self._can_access(job, user_id):
            raise web.HTTPNotFound(text="Job not found")
        if job.status != JobStatus.RUNNING or not job.container_id:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_str("Terminal is available only while the job is running.")
            await ws.close()
            return ws

        ws = web.WebSocketResponse(autoping=False, max_msg_size=8 * 1024 * 1024)
        await ws.prepare(request)
        await bridge_terminal(ws, self.runner, job.container_id)
        return ws

    async def _preview(self, request: web.Request) -> web.StreamResponse:
        job = await self.db.get(request.match_info["job_id"])
        if job is None or job.status != JobStatus.RUNNING or not job.host_port:
            raise web.HTTPNotFound(text="Preview is not available")
        tail = request.match_info.get("tail", "")
        if is_websocket(request):
            return await proxy_websocket(request, job.host_port, tail)
        return await proxy_http(request, job.host_port, tail, prefix=f"/preview/{job.id}/", job_id=job.id)

    def _can_access(self, job: Job, user_id: int) -> bool:
        return job.user_id == user_id or self.settings.is_admin(user_id)

    def _serialize(self, job: Job) -> dict:
        payload = job.as_dict()
        payload["preview_url"] = self.manager.preview_url(job)
        payload["miniapp_url"] = f"{self.settings.public_base}/"
        payload["metrics"] = self.manager.metrics(job) if job.status == JobStatus.RUNNING else None
        return payload


async def run_web_server(server: WebServer) -> None:
    await server.start()
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
