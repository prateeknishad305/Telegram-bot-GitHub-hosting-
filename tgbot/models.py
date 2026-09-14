from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    CLONING = "cloning"
    BUILDING = "building"
    RUNNING = "running"
    FAILED = "failed"
    STOPPED = "stopped"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self in {
            JobStatus.FAILED,
            JobStatus.STOPPED,
            JobStatus.EXPIRED,
        }

    @property
    def emoji(self) -> str:
        return {
            JobStatus.QUEUED: "[..]",
            JobStatus.CLONING: "[<-]",
            JobStatus.BUILDING: "[##]",
            JobStatus.RUNNING: "[OK]",
            JobStatus.FAILED: "[!!]",
            JobStatus.STOPPED: "[--]",
            JobStatus.EXPIRED: "[zz]",
        }[self]


def new_job_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass
class Job:
    id: str
    user_id: int
    chat_id: int
    repo_url: str
    repo_full_name: str
    branch: str | None = None
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    container_id: str | None = None
    image_tag: str | None = None
    host_port: int | None = None
    app_port: int | None = None
    run_command: str | None = None
    install_commands: str | None = None
    base_image: str | None = None
    commit_sha: str | None = None
    error: str | None = None
    log_path: str | None = None
    expires_at: float | None = None
    is_api: bool = False
    health_path: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "repo_url": self.repo_url,
            "repo_full_name": self.repo_full_name,
            "branch": self.branch,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "container_id": self.container_id,
            "image_tag": self.image_tag,
            "host_port": self.host_port,
            "app_port": self.app_port,
            "run_command": self.run_command,
            "install_commands": self.install_commands,
            "base_image": self.base_image,
            "commit_sha": self.commit_sha,
            "error": self.error,
            "log_path": self.log_path,
            "expires_at": self.expires_at,
            "is_api": self.is_api,
            "health_path": self.health_path,
        }
