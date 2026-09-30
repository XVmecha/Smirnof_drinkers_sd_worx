.PHONY: up down logs backend-logs restart reset pull functions docs-pull docs-push ask

DOCS_BUCKET ?= qwiklabs-gcp-02-19c5a9bec7a3-payroll-docs

up:        ## start in the background
	docker compose up -d --build

down:      ## stop (data volume is kept)
	docker compose down

logs:      ## follow logs
	docker compose logs -f open-webui

backend-logs: ## follow backend logs
	docker compose logs -f backend

restart:   ## apply docker-compose.yml / .env changes
	docker compose up -d --build --force-recreate

reset:     ## wipe all chats and settings, start fresh
	docker compose down -v
	docker compose up -d

pull:      ## pull the image for the pinned version
	docker compose pull

functions: ## install/update functions/*.py into Open WebUI
	docker compose exec open-webui python /app/hackathon/scripts/install_functions.py

docs-pull: ## download data/ from the GCS bucket
	gcloud storage rsync --recursive gs://$(DOCS_BUCKET) data

docs-push: ## upload data/ to the GCS bucket
	gcloud storage rsync --recursive data gs://$(DOCS_BUCKET)

ask:       ## smoke-test the backend: make ask Q="..." AS=U-001
	@curl -s -X POST http://127.0.0.1:$${BACKEND_PORT:-8000}/ask -H 'Content-Type: application/json' \
	  -d '{"question": "$(or $(Q),Brouwerij Delta, Belgium, 4h Saturday overtime - which rate?)", "consultant_id": "$(or $(AS),U-001)"}' \
	  | python3 -m json.tool
