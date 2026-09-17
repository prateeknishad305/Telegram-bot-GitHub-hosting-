from __future__ import annotations

import socket
from pathlib import Path

import docker
from docker.errors import APIError, DockerException, NotFound

from .config import discover_docker_host


class DockerRunnerError(RuntimeError):
    pass


class DockerRunner:
    def __init__(self, network: str, docker_host: str | None = None):
        self.network = network
        self.docker_host = discover_docker_host(docker_host)
        self._client: docker.DockerClient | None = None

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            try:
                self._client = docker.DockerClient(base_url=self.docker_host)
                self._client.ping()
            except DockerException as exc:
                raise DockerRunnerError(
                    f"Cannot connect to the Docker daemon at {self.docker_host}: {exc}\n"
                    "If you run rootless Docker or Podman, start the user service and point "
                    "DOCKER_HOST at its socket, for example "
                    "DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock "
                    "or DOCKER_HOST=unix:///run/user/$(id -u)/podman/podman.sock."
                ) from exc
        return self._client

    def ensure_network(self) -> None:
        try:
            self.client.networks.get(self.network)
        except NotFound:
            self.client.networks.create(self.network, driver="bridge", internal=False)

    def image_exists(self, tag: str) -> bool:
        try:
            self.client.images.get(tag)
            return True
        except NotFound:
            return False

    def build_image(self, context: Path, tag: str, dockerfile: str = "Dockerfile", on_log=None) -> None:
        low = self.client.api
        try:
            stream = low.build(
                path=str(context),
                dockerfile=dockerfile,
                tag=tag,
                rm=True,
                forcerm=True,
                decode=True,
                pull=False,
            )
            for chunk in stream:
                if on_log and chunk.get("stream"):
                    on_log(chunk["stream"])
                if chunk.get("error"):
                    raise DockerRunnerError(chunk["error"].strip())
        except APIError as exc:
            raise DockerRunnerError(f"Docker build failed: {exc}") from exc

    def run_container(
        self,
        image: str,
        app_port: int,
        host_port: int,
        name: str,
        mem_limit: str,
        nano_cpus: int,
        pids_limit: int,
        environment: dict | None = None,
    ) -> str:
        low = self.client.api
        try:
            host_config = low.create_host_config(
                port_bindings={f"{app_port}/tcp": ("127.0.0.1", host_port)},
                mem_limit=mem_limit,
                nano_cpus=nano_cpus,
                pids_limit=pids_limit,
                security_opt=["no-new-privileges:true"],
                restart_policy={"Name": "no"},
                network_mode=self.network,
            )
            container = low.create_container(
                image=image,
                name=name,
                host_config=host_config,
                ports=[app_port],
                environment=environment or {},
                labels={"tgbot.runner": "1"},
            )
            low.start(container["Id"])
            return container["Id"]
        except (APIError, DockerException) as exc:
            raise DockerRunnerError(f"Failed to start container: {exc}") from exc

    def get_logs(self, container_id: str, tail: int = 300) -> str:
        try:
            raw = self.client.api.logs(container_id, tail=tail, stdout=True, stderr=True)
        except (APIError, NotFound) as exc:
            raise DockerRunnerError(f"Cannot read container logs: {exc}") from exc
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="ignore")
        return str(raw)

    def stream_logs(self, container_id: str, on_line, stop_event) -> None:
        try:
            stream = self.client.api.logs(
                container_id, stdout=True, stderr=True, stream=True, follow=True, tail=50
            )
        except (APIError, NotFound):
            return
        buffer = b""
        for chunk in stream:
            if stop_event.is_set():
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                on_line(line.decode("utf-8", errors="ignore"))
        if buffer:
            on_line(buffer.decode("utf-8", errors="ignore"))

    def container_state(self, container_id: str) -> str:
        try:
            info = self.client.api.inspect_container(container_id)
        except NotFound:
            return "removed"
        state = info.get("State", {})
        return "running" if state.get("Running") else (state.get("Status") or "unknown")

    def exec_shell(self, container_id: str) -> tuple[str, str]:
        low = self.client.api
        for shell in ("/bin/sh", "/bin/bash"):
            try:
                exec_id = low.exec_create(container_id, cmd=[shell], tty=True, stdin=True)["Id"]
                return exec_id, shell
            except APIError:
                continue
        raise DockerRunnerError("No usable shell inside the container")

    def exec_stream(self, exec_id: str):
        low = self.client.api
        try:
            return low.exec_start(exec_id, tty=True, socket=True, demux=False)
        except APIError as exc:
            raise DockerRunnerError(f"Cannot attach to container shell: {exc}") from exc

    def exec_running(self, exec_id: str) -> bool:
        try:
            return bool(self.client.api.exec_inspect(exec_id).get("Running"))
        except (APIError, NotFound):
            return False

    def resize_exec(self, exec_id: str, rows: int, cols: int) -> None:
        try:
            self.client.api.exec_resize(exec_id, h=rows, w=cols)
        except APIError:
            pass

    def stop_container(self, container_id: str) -> None:
        try:
            self.client.api.stop(container_id, timeout=10)
        except NotFound:
            return
        except APIError:
            pass

    def remove_container(self, container_id: str) -> None:
        try:
            self.client.api.remove_container(container_id, force=True, v=True)
        except NotFound:
            return
        except APIError:
            pass

    def remove_image(self, tag: str) -> None:
        try:
            self.client.images.remove(image=tag, force=True)
        except (NotFound, APIError):
            return

    def stop_orphans(self) -> int:
        """Stop leftover job containers after a bot restart."""
        try:
            containers = self.client.containers.list(all=True, filters={"label": "tgbot.runner=1"})
        except (APIError, DockerException):
            return 0
        stopped = 0
        for container in containers:
            try:
                container.remove(force=True, v=True)
                stopped += 1
            except (APIError, NotFound):
                continue
        return stopped

    @staticmethod
    def find_free_port(start: int, end: int, used: set[int] | None = None) -> int:
        used = used or set()
        for port in range(start, end):
            if port in used:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise DockerRunnerError(f"No free port in range {start}-{end}")
