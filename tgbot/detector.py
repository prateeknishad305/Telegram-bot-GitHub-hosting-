from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_PORT = 8080

_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    "target",
    "vendor",
}


@dataclass
class RunPlan:
    kind: str
    base_image: str
    install_commands: list[str]
    build_commands: list[str]
    run_command: str
    app_port: int
    uses_repo_dockerfile: bool = False
    is_api: bool = False
    health_path: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def all_commands(self) -> list[str]:
        return [*self.install_commands, *self.build_commands]


def _read_text(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(_read_text(path) or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _has_web_ui(root: Path) -> bool:
    for marker in ("templates", "views", "public", "static", "frontend", "client"):
        path = root / marker
        if path.is_dir() and any(p.is_file() for p in path.rglob("*")):
            return True
    for name in ("index.html", "index.htm", "app/static", "src/App.jsx", "src/App.tsx"):
        if (root / name).exists():
            return True
    return False


def _iter_source_files(root: Path, suffixes: tuple[str, ...], max_files: int = 400):
    count = 0
    for path in sorted(root.rglob("*")):
        if count >= max_files:
            return
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        count += 1
        yield path


def _detect_node_version(package: dict) -> str:
    engines = package.get("engines", {})
    node = str(engines.get("node", "")) if isinstance(engines, dict) else ""
    match = re.search(r"(\d{2})", node)
    if match:
        major = int(match.group(1))
        if major >= 18:
            return str(major)
    return "20"


def _plan_node(root: Path, override_port: int | None = None) -> RunPlan:
    package = _read_json(root / "package.json")
    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict):
        scripts = {}
    deps = {}
    for key in ("dependencies", "devDependencies"):
        value = package.get(key, {})
        if isinstance(value, dict):
            deps.update(value)

    if (root / "pnpm-lock.yaml").exists():
        pm, install = "pnpm", "corepack enable && pnpm install --frozen-lockfile"
    elif (root / "yarn.lock").exists():
        pm, install = "yarn", "corepack enable && yarn install --frozen-lockfile"
    elif (root / "package-lock.json").exists():
        pm, install = "npm", "npm ci"
    else:
        pm, install = "npm", "npm install"

    is_vite = "vite" in deps
    is_next = "next" in deps
    is_nuxt = "nuxt" in deps
    is_cra = "react-scripts" in deps

    if is_next:
        app_port = override_port or 3000
        if "build" in scripts:
            build = [f"{pm} run build"]
            run = f"{pm} run start -- -H 0.0.0.0 -p {app_port}"
        else:
            build = []
            run = f"{pm} run dev -- -H 0.0.0.0 -p {app_port}"
    elif is_nuxt:
        app_port = override_port or 3000
        build = [f"{pm} run build"] if "build" in scripts else []
        run = f"{pm} run dev -- --host 0.0.0.0 --port {app_port}"
    elif is_vite:
        app_port = override_port or 5173
        build = [f"{pm} run build"] if "build" in scripts else []
        script = "dev" if "dev" in scripts else "preview"
        run = f"{pm} run {script} -- --host 0.0.0.0 --port {app_port}"
    elif is_cra:
        app_port = override_port or 3000
        build = [f"{pm} run build"] if "build" in scripts else []
        run = f"{pm} run start"
    else:
        app_port = override_port or 3000
        build = [f"{pm} run build"] if "build" in scripts else []
        if "start" in scripts:
            run = f"{pm} run start"
        elif "dev" in scripts:
            run = f"{pm} run dev"
        elif "serve" in scripts:
            run = f"{pm} run serve"
        elif (root / "server.js").exists():
            run = "node server.js"
        elif isinstance(package.get("main"), str) and package["main"]:
            run = f"node {package['main']}"
        else:
            run = "node index.js"

    node_version = _detect_node_version(package)
    plan = RunPlan(
        kind="node",
        base_image=f"node:{node_version}-slim",
        install_commands=[install],
        build_commands=build,
        run_command=run,
        app_port=app_port,
        is_api="express" in deps and not (is_next or is_vite or is_nuxt or is_cra) and not _has_web_ui(root),
        health_path="/health",
        notes=[f"package manager: {pm}"],
    )
    if is_next:
        plan.notes.append("Next.js detected")
    if is_vite:
        plan.notes.append("Vite detected")
    return plan


def _python_dependency_text(root: Path) -> str:
    parts = []
    for name in ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "Pipfile", "setup.py"):
        path = root / name
        if path.exists():
            parts.append(_read_text(path).lower())
    return "\n".join(parts)


def _python_install_commands(root: Path) -> list[str]:
    if (root / "poetry.lock").exists() and (root / "pyproject.toml").exists():
        return ["pip install --no-cache-dir poetry && poetry config virtualenvs.create false && poetry install --no-root"]
    if (root / "uv.lock").exists() and (root / "pyproject.toml").exists():
        return ["pip install --no-cache-dir uv && uv sync"]
    if (root / "Pipfile").exists():
        return ["pip install --no-cache-dir pipenv && pipenv install --system --deploy || pipenv install --system"]
    if (root / "requirements.txt").exists():
        return ["pip install --no-cache-dir -r requirements.txt"]
    if (root / "pyproject.toml").exists():
        return ["pip install --no-cache-dir ."]
    return ["pip install --no-cache-dir ."]


def _find_python_entry(root: Path, needles: tuple[str, ...], preferred: tuple[str, ...]) -> Path | None:
    for name in preferred:
        path = root / name
        if path.exists() and any(needle in _read_text(path) for needle in needles):
            return path
    for path in _iter_source_files(root, (".py",)):
        content = _read_text(path, limit=50_000)
        if any(needle in content for needle in needles):
            return path
    return None


def _plan_python(root: Path, override_port: int | None = None) -> RunPlan:
    deps = _python_dependency_text(root)
    install = _python_install_commands(root)

    manage_py = root / "manage.py"
    if manage_py.exists():
        app_port = override_port or 8000
        return RunPlan(
            kind="python-django",
            base_image="python:3.12-slim",
            install_commands=install,
            build_commands=["python manage.py migrate --noinput"],
            run_command=f"python manage.py runserver 0.0.0.0:{app_port}",
            app_port=app_port,
            notes=["Django detected"],
        )

    if "streamlit" in deps:
        app_port = override_port or 8501
        entry = _find_python_entry(root, ("import streamlit",), ("streamlit_app.py", "app.py", "main.py"))
        script = entry.name if entry else "app.py"
        return RunPlan(
            kind="python-streamlit",
            base_image="python:3.12-slim",
            install_commands=install,
            build_commands=[],
            run_command=(
                f"streamlit run {script} --server.address 0.0.0.0 "
                f"--server.port {app_port} --server.headless true"
            ),
            app_port=app_port,
            notes=["Streamlit detected"],
        )

    if "gradio" in deps:
        app_port = override_port or 7860
        entry = _find_python_entry(root, ("import gradio",), ("app.py", "main.py", "demo.py"))
        script = entry.name if entry else "app.py"
        return RunPlan(
            kind="python-gradio",
            base_image="python:3.12-slim",
            install_commands=install,
            build_commands=[],
            run_command=f"python {script}",
            app_port=app_port,
            notes=["Gradio detected (set server_name=0.0.0.0 in launch())"],
        )

    if "fastapi" in deps or "uvicorn" in deps:
        app_port = override_port or 8000
        entry = _find_python_entry(root, ("FastAPI(",), ("main.py", "app.py", "api.py", "server.py"))
        module = f"{entry.stem}:app" if entry else "main:app"
        if entry:
            content = _read_text(entry, limit=50_000)
            var_match = re.search(r"(\w+)\s*=\s*FastAPI\(", content)
            if var_match:
                module = f"{entry.stem}:{var_match.group(1)}"
        return RunPlan(
            kind="python-fastapi",
            base_image="python:3.12-slim",
            install_commands=install,
            build_commands=[],
            run_command=f"uvicorn {module} --host 0.0.0.0 --port {app_port}",
            app_port=app_port,
            is_api=True,
            health_path="/health",
            notes=["FastAPI detected"],
        )

    if "flask" in deps:
        app_port = override_port or 5000
        entry = _find_python_entry(root, ("Flask(",), ("app.py", "main.py", "wsgi.py", "server.py"))
        module = entry.stem if entry else "app"
        return RunPlan(
            kind="python-flask",
            base_image="python:3.12-slim",
            install_commands=install,
            build_commands=[],
            run_command=f"flask --app {module} run --host 0.0.0.0 --port {app_port}",
            app_port=app_port,
            is_api=not _has_web_ui(root),
            health_path="/health",
            notes=["Flask detected"],
        )

    app_port = override_port or DEFAULT_PORT
    entry = _find_python_entry(root, ("if __name__",), ("main.py", "app.py", "bot.py", "server.py"))
    script = entry.name if entry else "main.py"
    return RunPlan(
        kind="python",
        base_image="python:3.12-slim",
        install_commands=install,
        build_commands=[],
        run_command=f"python {script}",
        app_port=app_port,
        notes=["generic Python entrypoint"],
    )


def _plan_go(root: Path, override_port: int | None = None) -> RunPlan:
    app_port = override_port or DEFAULT_PORT
    return RunPlan(
        kind="go",
        base_image="golang:1.23-bookworm",
        install_commands=["go mod download || true"],
        build_commands=["go build -o /app/__server__ . || go build -o /app/__server__ ./..."],
        run_command="/app/__server__",
        app_port=app_port,
        is_api=True,
        health_path="/health",
        notes=["Go module build"],
    )


def _cargo_package_name(root: Path) -> str:
    content = _read_text(root / "Cargo.toml")
    match = re.search(r'^\s*name\s*=\s*"([^"]+)"', content, re.MULTILINE)
    return match.group(1) if match else "app"


def _plan_rust(root: Path, override_port: int | None = None) -> RunPlan:
    app_port = override_port or DEFAULT_PORT
    name = _cargo_package_name(root)
    return RunPlan(
        kind="rust",
        base_image="rust:1.82-slim",
        install_commands=[],
        build_commands=["cargo build --release"],
        run_command=f"./target/release/{name}",
        app_port=app_port,
        notes=["Cargo release build"],
    )


def _plan_ruby(root: Path, override_port: int | None = None) -> RunPlan:
    app_port = override_port or 3000
    install = ["bundle config set --local path vendor/bundle", "bundle install"]
    if (root / "Gemfile").exists() and "rails" in _read_text(root / "Gemfile").lower():
        run = f"bundle exec rails server -b 0.0.0.0 -p {app_port}"
        notes = ["Rails detected"]
    else:
        entry = root / "app.rb"
        run = f"bundle exec ruby {entry.name}" if entry.exists() else "bundle exec ruby app.rb"
        notes = ["generic Ruby entrypoint"]
    return RunPlan(
        kind="ruby",
        base_image="ruby:3.3-slim",
        install_commands=install,
        build_commands=[],
        run_command=run,
        app_port=app_port,
        notes=notes,
    )


def _plan_php(root: Path, override_port: int | None = None) -> RunPlan:
    install = ["composer install --no-interaction --no-progress"] if (root / "composer.json").exists() else []
    if (root / "artisan").exists():
        app_port = override_port or 8000
        run = f"php artisan serve --host=0.0.0.0 --port={app_port}"
        notes = ["Laravel detected"]
    else:
        app_port = override_port or 8000
        run = f"php -S 0.0.0.0:{app_port} -t ."
        notes = ["PHP built-in server"]
    return RunPlan(
        kind="php",
        base_image="php:8.3-cli",
        install_commands=install,
        build_commands=[],
        run_command=run,
        app_port=app_port,
        notes=notes,
    )


def _plan_maven(root: Path, override_port: int | None = None) -> RunPlan:
    app_port = override_port or DEFAULT_PORT
    return RunPlan(
        kind="java-maven",
        base_image="maven:3.9-eclipse-temurin-21",
        install_commands=[],
        build_commands=["mvn -q -DskipTests package"],
        run_command="java -jar target/*.jar",
        app_port=app_port,
        notes=["Maven project"],
    )


def _plan_gradle(root: Path, override_port: int | None = None) -> RunPlan:
    app_port = override_port or DEFAULT_PORT
    gradlew = "sh ./gradlew" if (root / "gradlew").exists() else "gradle"
    return RunPlan(
        kind="java-gradle",
        base_image="gradle:8-jdk21",
        install_commands=[],
        build_commands=[f"{gradlew} build -x test"],
        run_command=f"{gradlew} bootRun",
        app_port=app_port,
        notes=["Gradle project"],
    )


def _plan_static(root: Path, override_port: int | None = None) -> RunPlan:
    return RunPlan(
        kind="static",
        base_image="nginx:alpine",
        install_commands=[],
        build_commands=[],
        run_command="nginx -g 'daemon off;'",
        app_port=override_port or 80,
        notes=["static site served by nginx"],
    )


def _detect_expose_ports(root: Path) -> list[int]:
    content = _read_text(root / "Dockerfile")
    return [int(m) for m in re.findall(r"(?im)^\s*EXPOSE\s+(\d+)", content)]


def _plan_from_override(root: Path, config: dict) -> RunPlan:
    kind = str(config.get("kind", "override"))
    base_image = str(config.get("base_image") or config.get("image") or "ubuntu:24.04")

    def as_list(value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    app_port = int(config.get("port") or DEFAULT_PORT)
    return RunPlan(
        kind=kind,
        base_image=base_image,
        install_commands=as_list(config.get("install") or config.get("dependencies")),
        build_commands=as_list(config.get("build")),
        run_command=str(config.get("run") or config.get("command") or "sleep infinity"),
        app_port=app_port,
        is_api=bool(config.get("api", False)),
        health_path=str(config["health"]) if config.get("health") else None,
        notes=["configured via .tgrunner.yml"],
    )


def detect_plan(repo_dir: Path, override_port: int | None = None) -> RunPlan:
    override_file = repo_dir / ".tgrunner.yml"
    if not override_file.exists():
        override_file = repo_dir / ".tgrunner.yaml"
    if override_file.exists():
        try:
            config = yaml.safe_load(_read_text(override_file)) or {}
            if isinstance(config, dict):
                return _plan_from_override(repo_dir, config)
        except yaml.YAMLError:
            pass

    if (repo_dir / "package.json").exists():
        return _plan_node(repo_dir, override_port)

    python_markers = ("requirements.txt", "pyproject.toml", "Pipfile", "setup.py", "manage.py")
    if any((repo_dir / marker).exists() for marker in python_markers):
        return _plan_python(repo_dir, override_port)

    if (repo_dir / "go.mod").exists():
        return _plan_go(repo_dir, override_port)

    if (repo_dir / "Cargo.toml").exists():
        return _plan_rust(repo_dir, override_port)

    if (repo_dir / "Gemfile").exists():
        return _plan_ruby(repo_dir, override_port)

    if (repo_dir / "composer.json").exists() or (repo_dir / "index.php").exists():
        return _plan_php(repo_dir, override_port)

    if (repo_dir / "pom.xml").exists():
        return _plan_maven(repo_dir, override_port)

    if (repo_dir / "build.gradle").exists() or (repo_dir / "build.gradle.kts").exists():
        return _plan_gradle(repo_dir, override_port)

    if (repo_dir / "Dockerfile").exists():
        ports = _detect_expose_ports(repo_dir)
        return RunPlan(
            kind="dockerfile",
            base_image="",
            install_commands=[],
            build_commands=[],
            run_command="",
            app_port=override_port or (ports[0] if ports else DEFAULT_PORT),
            uses_repo_dockerfile=True,
            notes=["using repository Dockerfile", f"EXPOSE: {ports or 'none'}"],
        )

    for name in ("index.html", "index.htm"):
        if (repo_dir / name).exists():
            return _plan_static(repo_dir, override_port)

    return _plan_static(repo_dir, override_port)


def render_dockerfile(plan: RunPlan) -> str:
    lines: list[str] = []
    if plan.uses_repo_dockerfile:
        raise ValueError("render_dockerfile is not used for repository Dockerfiles")

    lines.append(f"FROM {plan.base_image}")
    lines.append(
        "ENV DEBIAN_FRONTEND=noninteractive \\\n"
        "    PYTHONUNBUFFERED=1 \\\n"
        "    PYTHONDONTWRITEBYTECODE=1 \\\n"
        "    PIP_NO_CACHE_DIR=1 \\\n"
        "    HOST=0.0.0.0 \\\n"
        f"    PORT={plan.app_port} \\\n"
        f"    GRADIO_SERVER_NAME=0.0.0.0 \\\n"
        f"    GRADIO_SERVER_PORT={plan.app_port}"
    )
    lines.append("WORKDIR /app")

    if plan.kind == "static":
        lines.append("COPY . /usr/share/nginx/html")
        lines.append(f"EXPOSE {plan.app_port}")
        lines.append(f"CMD {json.dumps(['sh', '-lc', plan.run_command])}")
        return "\n".join(lines) + "\n"

    lines.append("COPY . /app")

    if plan.all_commands and any("git+" in cmd or "git@" in cmd for cmd in plan.all_commands):
        lines.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates "
            "&& rm -rf /var/lib/apt/lists/*"
        )

    for command in plan.install_commands:
        lines.append(f"RUN {command}")
    for command in plan.build_commands:
        lines.append(f"RUN {command}")

    lines.append(f"EXPOSE {plan.app_port}")
    lines.append(f"CMD {json.dumps(['sh', '-lc', plan.run_command])}")
    return "\n".join(lines) + "\n"
