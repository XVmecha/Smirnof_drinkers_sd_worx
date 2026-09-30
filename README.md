# Smirnof_drinkers_sd_worx

Placeholder deployment of [Open WebUI](https://openwebui.com/), a self-hosted chat UI that can connect to local models (Ollama) or OpenAI-compatible APIs.

## Quick start

Requires Docker with Compose.

```bash
cp .env.example .env
# set WEBUI_SECRET_KEY, e.g. with: openssl rand -hex 32
docker compose up -d
```

Open http://localhost:3000. The first account you create becomes the admin.

Chats, users, and settings persist in the `open-webui` Docker volume.

## Connecting a model

- **Ollama on the host:** run Ollama locally; Open WebUI reaches it via `host.docker.internal:11434`. Set `OLLAMA_BASE_URL` in `.env` if it lives elsewhere.
- **OpenAI-compatible API:** set `OPENAI_API_KEY` in `.env`, or add a connection in *Admin Settings → Connections*.

## Common commands

```bash
docker compose logs -f open-webui   # follow logs
docker compose pull && docker compose up -d   # update to latest image
docker compose down                 # stop (data volume is kept)
```
