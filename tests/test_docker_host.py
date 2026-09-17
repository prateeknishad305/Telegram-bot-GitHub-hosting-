from tgbot.config import (
    DEFAULT_DOCKER_HOST,
    discover_docker_host,
    normalize_docker_host,
    rootless_socket_candidates,
)


def test_normalize_docker_host():
    assert normalize_docker_host("/run/user/1000/docker.sock") == "unix:///run/user/1000/docker.sock"
    assert normalize_docker_host("unix:///run/user/1000/docker.sock") == "unix:///run/user/1000/docker.sock"
    assert normalize_docker_host("tcp://1.2.3.4:2375") == "tcp://1.2.3.4:2375"
    assert normalize_docker_host("  ") == DEFAULT_DOCKER_HOST


def test_explicit_host_wins():
    host = discover_docker_host(explicit="/run/user/1000/docker.sock", env="tcp://1.2.3.4:2375")
    assert host == "unix:///run/user/1000/docker.sock"


def test_env_host_wins_over_detection():
    host = discover_docker_host(env="tcp://1.2.3.4:2375", exists=lambda _path: True)
    assert host == "tcp://1.2.3.4:2375"


def test_default_socket_is_preferred():
    host = discover_docker_host(env="", exists=lambda path: path == "/var/run/docker.sock")
    assert host == DEFAULT_DOCKER_HOST


def test_rootless_socket_is_detected(monkeypatch):
    monkeypatch.setattr("tgbot.config.rootless_socket_candidates", lambda: ["/run/user/1000/docker.sock"])
    host = discover_docker_host(env="", exists=lambda path: path == "/run/user/1000/docker.sock")
    assert host == "unix:///run/user/1000/docker.sock"


def test_falls_back_to_default_when_nothing_exists():
    host = discover_docker_host(env="", exists=lambda _path: False)
    assert host == DEFAULT_DOCKER_HOST


def test_rootless_candidates_are_unique():
    candidates = rootless_socket_candidates()
    assert len(candidates) == len(set(candidates))
    assert all(candidate.endswith((".sock",)) for candidate in candidates)
