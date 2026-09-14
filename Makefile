.PHONY: install run test lint docker-build docker-up docker-down

install:
	python -m pip install --break-system-packages -r requirements.txt

run:
	python -m tgbot.main

test:
	python -m pytest -q

lint:
	python -m ruff check tgbot tests

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down
