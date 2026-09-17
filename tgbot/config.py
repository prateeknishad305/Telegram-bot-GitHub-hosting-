from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DOCKER_HOST = "unix:///var/run/docker.sock"


def normalize_docker_host(value: str) -> str:
    value = value.strip()
    if not value:
        return DEFAULT_DOCKER_HOST
    if "://" in value:
        return value
    return f"unix://{value}"


def rootless_socket_candidates() -> list[str]:
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    candidates = [
        f"{runtime}/docker.sock",
        f"/run/user/{uid}/docker.sock",
        f"{runtime}/podman/podman.sock",
        f"/run/user/{uid}/podman/podman.sock",
    ]
    return list(dict.fromkeys(candidates))


def discover_docker_host(
    explicit: str | None = None,
    env: str | None = None,
    exists: Callable[[str], bool] | None = None,
) -> str:
    exists = exists or os.path.exists
    if explicit:
        return normalize_docker_host(explicit)
    env_value = env if env is not None else os.environ.get("DOCKER_HOST")
    if env_value:
        return normalize_docker_host(env_value)
    if exists(DEFAULT_DOCKER_HOST.removeprefix("unix://")):
        return DEFAULT_DOCKER_HOST
    for candidate in rootless_socket_candidates():
        if exists(candidate):
            return normalize_docker_host(candidate)
    return DEFAULT_DOCKER_HOST


def _split_ints(raw: str) -> set[int]:
    result: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


def _split_strs(raw: str) -> set[str]:
    return {p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip()}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    public_url: str = Field(default="http://localhost:8080", alias="PUBLIC_URL")

    allowed_user_ids_raw: str = Field(default="", alias="ALLOWED_USER_IDS")
    admin_user_ids_raw: str = Field(default="", alias="ADMIN_USER_IDS")
    allowed_repo_owners_raw: str = Field(default="", alias="ALLOWED_REPO_OWNERS")
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")

    max_concurrent_jobs: int = Field(default=2, alias="MAX_CONCURRENT_JOBS")
    job_timeout_seconds: int = Field(default=3600, alias="JOB_TIMEOUT_SECONDS")
    mem_limit: str = Field(default="1g", alias="MEM_LIMIT")
    nano_cpus: int = Field(default=1_000_000_000, alias="NANO_CPUS")
    pids_limit: int = Field(default=256, alias="PIDS_LIMIT")

    preview_base_domain: str = Field(default="", alias="PREVIEW_BASE_DOMAIN")
    port_range_start: int = Field(default=20000, alias="PORT_RANGE_START")
    port_range_end: int = Field(default=30000, alias="PORT_RANGE_END")

    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=8080, alias="WEB_PORT")
    runtime_port: int | None = Field(default=None, alias="PORT")
    enable_terminal: bool = Field(default=True, alias="ENABLE_TERMINAL")

    health_check_interval: int = Field(default=15, alias="HEALTH_CHECK_INTERVAL")
    health_check_timeout: int = Field(default=5, alias="HEALTH_CHECK_TIMEOUT")
    metrics_flush_interval: int = Field(default=10, alias="METRICS_FLUSH_INTERVAL")
    health_history_size: int = Field(default=120, alias="HEALTH_HISTORY_SIZE")

    session_secret: str = Field(default="", alias="SESSION_SECRET")

    dev_login_user_id: int = Field(default=0, alias="DEV_LOGIN_USER_ID")

    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    docker_network: str = Field(default="tgbot-runner", alias="DOCKER_NETWORK")
    docker_host: str | None = Field(default=None, alias="DOCKER_HOST")

    archive_max_bytes: int = Field(default=512 * 1024 * 1024, alias="ARCHIVE_MAX_BYTES")
    archive_extract_max_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024, alias="ARCHIVE_EXTRACT_MAX_BYTES"
    )

    cache_cleanup_interval_seconds: int = Field(
        default=3 * 3600, alias="CACHE_CLEANUP_INTERVAL_SECONDS"
    )
    cache_max_age_seconds: int = Field(default=3 * 3600, alias="CACHE_MAX_AGE_SECONDS")
    restart_on_disk_full: bool = Field(default=False, alias="RESTART_ON_DISK_FULL")
    disk_restart_percent: float = Field(default=95.0, alias="DISK_RESTART_PERCENT")
    stale_job_grace_seconds: int = Field(default=120, alias="STALE_JOB_GRACE_SECONDS")

    @model_validator(mode="after")
    def _apply_runtime_port(self) -> Settings:
        explicit = os.environ.get("WEB_PORT") or "web_port" in self.model_fields_set
        if self.runtime_port and not explicit:
            self.web_port = self.runtime_port
        return self

    @property
    def allowed_user_ids(self) -> set[int]:
        return _split_ints(self.allowed_user_ids_raw)

    @property
    def admin_user_ids(self) -> set[int]:
        return _split_ints(self.admin_user_ids_raw)

    @property
    def allowed_repo_owners(self) -> set[str]:
        return _split_strs(self.allowed_repo_owners_raw)

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "runner.db"

    @property
    def public_base(self) -> str:
        return self.public_url.rstrip("/")

    def resolved_docker_host(self) -> str:
        return discover_docker_host(self.docker_host)

    def secret(self) -> str:
        if self.session_secret:
            return self.session_secret
        self.session_secret = secrets.token_urlsafe(48)
        return self.session_secret

    def is_user_allowed(self, user_id: int) -> bool:
        allowed = self.allowed_user_ids
        if not allowed:
            return True
        return user_id in allowed

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_user_ids

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
