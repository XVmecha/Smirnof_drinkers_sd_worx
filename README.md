# Smirnof_drinkers_sd_worx

Placeholder deployment of [Open WebUI](https://openwebui.com/), a self-hosted chat UI that can connect to local models (Ollama) or OpenAI-compatible APIs.

Stripped down for the hackathon: no login, no secret key, no required config, and a few unused features turned off (see `docker-compose.yml`).

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
make up        # or: docker compose up -d
```

Open http://localhost:3000. There is no login: auth is off (`WEBUI_AUTH=False`), so anyone who can reach the port can use it. Keep it on localhost.

The first start takes a minute or two while the image downloads and the app initialises. `make logs` shows progress.

No `.env` is required. Copy `.env.example` to `.env` only to change the port, name, Ollama URL, default model, or add an OpenAI key.

If you already ran it with login enabled, wipe the data volume once so no-auth mode can start: `make reset`.

## Connecting a model

- **Ollama (recommended on a Mac):** install Ollama natively (`brew install ollama`, then `ollama serve` and `ollama pull llama3.2`). Running it natively uses the Mac GPU; inside Docker it would be CPU-only and slow. Open WebUI reaches it via `host.docker.internal:11434`.
- **OpenAI-compatible API:** set `OPENAI_API_KEY` in `.env`, then `make restart`.

## Configuration

`docker-compose.yml` is the source of truth. `ENABLE_PERSISTENT_CONFIG=False` means env vars always win, and changes made in *Admin Settings* are lost on restart. To change something for everyone, edit `docker-compose.yml` (or your `.env`) and run `make restart`.

All options: https://docs.openwebui.com/reference/env-configuration

## Common commands

```bash
make up        # start
make logs      # follow logs
make restart   # apply config changes
make down      # stop (data volume is kept)
make reset     # wipe chats/settings and start fresh
```
