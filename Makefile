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

docs-pull: ## download PDFs + metadata.json from the GCS bucket into ./docs
	gcloud storage rsync gs://$(DOCS_BUCKET) docs

docs-push: ## upload ./docs to the GCS bucket
	gcloud storage rsync docs gs://$(DOCS_BUCKET)

ask:       ## smoke-test the backend: make ask Q="does the overtime rule apply?"
	@curl -s -X POST http://127.0.0.1:$${BACKEND_PORT:-8000}/ask -H 'Content-Type: application/json' \
	  -d '{"question": "$(or $(Q),Does the new overtime rule apply to this client?)", "client": {"id": "janssens", "name": "Brouwerij Janssens NV", "country": "BE", "joint_committee": "118"}}' \
	  | python3 -m json.tool
