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

## Payroll Assistant + trust card

`functions/payroll_assistant.py` is an Open WebUI *pipe* function. It adds one model per client to the dropdown ("Payroll Assistant · Brouwerij Janssens NV (BE)"), sends the question to our FastAPI backend, and shows the answer with a **trust card** underneath: main source, freshness, owner, country/client fit, conflicts side by side, excluded sources and who to ask.

Install or update it after starting the stack (and after every edit to the file):

```bash
make up
make functions
```

With `TRUST_API_URL` unset, it uses built-in mock data so the UI can be demoed before the backend exists. Point it at the backend in `.env`:

```bash
TRUST_API_URL=http://host.docker.internal:8000/ask
```

The request/response contract for the backend is at the top of `functions/payroll_assistant.py`. The trust score is computed from the source metadata with fixed rules (see `assess_source`), so every point on the card can be explained.

## Voice

Works out of the box with no keys:

- **Speech-to-text** (mic button, voice call mode): Whisper runs locally in the container. The first use downloads the `base` model (~150 MB).
- **Text-to-speech** (speaker icon under an answer, voice call mode): the browser's built-in voices.

For better voices, use ElevenLabs. In `.env`:

```bash
AUDIO_TTS_ENGINE=elevenlabs
AUDIO_TTS_API_KEY=<your ElevenLabs API key>
AUDIO_TTS_MODEL=eleven_multilingual_v2
AUDIO_TTS_VOICE=21m00Tcm4TlvDq8ikWAM   # voice ID, from the ElevenLabs Voices page
```

Then `make restart`. OpenAI, Azure and Mistral also work via `AUDIO_TTS_ENGINE`; see the [env reference](https://docs.openwebui.com/reference/env-configuration).

ElevenLabs is optional. On startup, `scripts/start.py` checks it and falls back to browser voices if the key is missing, rejected, or ElevenLabs can't be reached, so the app always starts and the speaker button keeps working. It also swaps in a valid voice if `AUDIO_TTS_VOICE` isn't in your account. See what it decided with:

```bash
make logs | grep '\[voice\]'
```

The browser only allows the mic on `localhost` or HTTPS, so open the app via http://localhost:3000, not an IP address.

## Backend (FastAPI `/ask`)

`backend/` is a small FastAPI service that runs next to Open WebUI in `docker-compose.yml`:

1. BM25 search over `docs/` (`.pdf`, `.md`, `.txt`). The index rebuilds itself when files change.
2. Trust metadata per file comes from `docs/metadata.json` (title, doc_type, country, joint_committees, owner, expert, updated, superseded_by).
3. Gemini writes the answer with `[n]` citations and flags conflicting sources.

Setup: put your keys in `.env` (Gemini from https://aistudio.google.com, Groq from https://console.groq.com/keys; either one alone works):

```bash
GEMINI_API_KEY=...
GROQ_API_KEY=...
```

then `make restart`. Test it without the UI: `make ask Q="Does the new overtime rule apply to this client?"`. Health check: http://127.0.0.1:8000/health.

Documents live in the bucket `gs://qwiklabs-gcp-02-19c5a9bec7a3-payroll-docs`: `make docs-pull` downloads them into `docs/`, `make docs-push` uploads local changes. `docs/` ships with six sample Markdown docs containing the planted problems (outdated, wrong country, conflicting chat message).

### LLM providers: Gemini first, Groq as backup

The backend tries a chain of models and uses the first one that answers:

1. Gemini: `gemini-3.8-flash`, then `gemini-flash-latest`, `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-flash-lite-latest`
2. Groq: `openai/gpt-oss-120b`, then `qwen/qwen3.8-27b`, `openai/gpt-oss-20b`

A model is skipped when it is overloaded (503), rate limited (429), retired (404) or times out (`LLM_TIMEOUT_SECONDS`, default 20 s). A rejected key (401/403) skips the rest of that provider. The trust card shows which model answered ("Answered by gemini/gemini-3.5-flash"), so a fallback is never hidden.

**Why Gemini is primary.** Google Cloud is a hackathon partner, Gemini handles Dutch/French/English payroll text well, and it follows the JSON answer format reliably.

**Why a backup at all.** We run on the free Gemini API tier. While building we hit all three failure modes within an hour: `gemini-2.5-flash` was closed to new users (404), `gemini-3.8-flash` returned "high demand" (503), and the per-minute free quota ran out (429). The Google Cloud project we were given could not help either: its org policy blocks every Vertex AI model and its Gemini API quota is 0. A live demo can't depend on one free endpoint.

**Why Groq.** It has a free tier, it speaks the same OpenAI-compatible API (so it's the same code path, just a different base URL and key), it is fast, and it is a different company's infrastructure, so an outage or rate limit at Google doesn't affect it. Its open-weight models are a little weaker at spotting subtle conflicts than Gemini, which is why it is the backup and not the primary.

Keys go in `.env` (`GEMINI_API_KEY`, `GROQ_API_KEY`); either one alone is enough. If Gemini is unreliable on demo day, set `LLM_ORDER=groq,gemini` and `make restart` to skip the wait on failing Gemini calls. `GET /health` lists the active chain.

## Connecting a model directly

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
make functions # install/update functions/*.py
make down      # stop (data volume is kept)
make reset     # wipe chats/settings and start fresh
```
