.PHONY: up down logs restart reset pull

up:        ## start in the background
	docker compose up -d

down:      ## stop (data volume is kept)
	docker compose down

logs:      ## follow logs
	docker compose logs -f open-webui

restart:   ## apply docker-compose.yml / .env changes
	docker compose up -d --force-recreate

reset:     ## wipe all chats and settings, start fresh
	docker compose down -v
	docker compose up -d

pull:      ## pull the image for the pinned version
	docker compose pull
