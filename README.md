# Smirnof_drinkers_sd_worx

Placeholder deployment of [Open WebUI](https://openwebui.com/), a self-hosted chat UI that can connect to local models (Ollama) or OpenAI-compatible APIs.

## Quick start

Requires Docker with Compose.

### macOS without Docker Desktop (Colima)

```bash
brew install colima docker docker-compose
mkdir -p ~/.docker
# let the docker CLI find the Homebrew compose plugin (merge by hand if config.json already exists)
[ -f ~/.docker/config.json ] || echo '{"cliPluginsExtraDirs": ["'"$(brew --prefix)"'/lib/docker/cli-plugins"]}' > ~/.docker/config.json
colima start --cpu 2 --memory 4
docker compose version   # should print a version
```

Colima has to be running (`colima start`) whenever you use Docker. Stop it with `colima stop`.

### Run it

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
