import json

from tgbot.detector import detect_plan, render_dockerfile


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_detect_node_vite(tmp_path):
    _write(tmp_path / "package.json", json.dumps({"dependencies": {"vite": "^5"}, "scripts": {"dev": "vite", "build": "vite build"}}))
    plan = detect_plan(tmp_path)
    assert plan.kind == "node"
    assert plan.base_image.startswith("node:")
    assert plan.app_port == 5173
    assert "npm run dev" in plan.run_command
    assert "0.0.0.0" in plan.run_command


def test_detect_node_next_uses_production(tmp_path):
    _write(tmp_path / "package.json", json.dumps({"dependencies": {"next": "14"}, "scripts": {"build": "next build", "start": "next start"}}))
    plan = detect_plan(tmp_path)
    assert plan.app_port == 3000
    assert any("build" in command for command in plan.build_commands)
    assert "next" not in plan.run_command or "start" in plan.run_command


def test_detect_python_flask(tmp_path):
    _write(tmp_path / "requirements.txt", "flask==3.0\n")
    _write(tmp_path / "app.py", "from flask import Flask\napp = Flask(__name__)\n")
    plan = detect_plan(tmp_path)
    assert plan.kind == "python-flask"
    assert plan.app_port == 5000
    assert "flask" in plan.run_command
    assert plan.is_api is True


def test_detect_python_fastapi_module(tmp_path):
    _write(tmp_path / "requirements.txt", "fastapi\nuvicorn\n")
    _write(tmp_path / "main.py", "from fastapi import FastAPI\napi = FastAPI()\n")
    plan = detect_plan(tmp_path)
    assert plan.kind == "python-fastapi"
    assert "uvicorn main:api" in plan.run_command
    assert plan.is_api is True


def test_flask_with_templates_is_not_api(tmp_path):
    _write(tmp_path / "requirements.txt", "flask\n")
    _write(tmp_path / "app.py", "from flask import Flask\napp = Flask(__name__)\n")
    _write(tmp_path / "templates" / "index.html", "<h1>hi</h1>")
    plan = detect_plan(tmp_path)
    assert plan.kind == "python-flask"
    assert plan.is_api is False


def test_express_api_detection(tmp_path):
    _write(tmp_path / "package.json", json.dumps({"dependencies": {"express": "4"}, "scripts": {"start": "node server.js"}}))
    plan = detect_plan(tmp_path)
    assert plan.is_api is True
    assert plan.health_path == "/health"


def test_express_with_public_dir_is_not_api(tmp_path):
    _write(tmp_path / "package.json", json.dumps({"dependencies": {"express": "4"}, "scripts": {"start": "node server.js"}}))
    _write(tmp_path / "public" / "index.html", "<h1>hi</h1>")
    plan = detect_plan(tmp_path)
    assert plan.is_api is False


def test_detect_django(tmp_path):
    _write(tmp_path / "requirements.txt", "django\n")
    _write(tmp_path / "manage.py", "#!/usr/bin/env python\n")
    plan = detect_plan(tmp_path)
    assert plan.kind == "python-django"
    assert plan.app_port == 8000


def test_detect_static(tmp_path):
    _write(tmp_path / "index.html", "<h1>hi</h1>")
    plan = detect_plan(tmp_path)
    assert plan.kind == "static"
    assert plan.base_image == "nginx:alpine"


def test_override_file_wins(tmp_path):
    _write(tmp_path / "package.json", json.dumps({"scripts": {"start": "node ."}}))
    _write(
        tmp_path / ".tgrunner.yml",
        "base_image: node:22-slim\ninstall:\n  - npm ci\nrun: node server.js\nport: 9000\n",
    )
    plan = detect_plan(tmp_path)
    assert plan.base_image == "node:22-slim"
    assert plan.app_port == 9000
    assert plan.run_command == "node server.js"


def test_render_dockerfile_has_expected_directives(tmp_path):
    _write(tmp_path / "package.json", json.dumps({"dependencies": {"express": "4"}, "scripts": {"start": "node server.js"}}))
    plan = detect_plan(tmp_path)
    dockerfile = render_dockerfile(plan)
    assert dockerfile.startswith("FROM node:")
    assert "WORKDIR /app" in dockerfile
    assert "RUN npm install" in dockerfile
    assert f"EXPOSE {plan.app_port}" in dockerfile
    assert "CMD" in dockerfile
