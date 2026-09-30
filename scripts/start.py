"""Container entrypoint: sanity-check the voice config, then start Open WebUI.

If AUDIO_TTS_ENGINE=elevenlabs but the key is missing, rejected or ElevenLabs
is unreachable, fall back to the browser's built-in voices instead of leaving
a broken speaker button. Never blocks startup: any unexpected error here is
logged and Open WebUI starts anyway.
"""

import json
import os
import sys
import urllib.error
import urllib.request

ELEVENLABS_DEFAULT_MODEL = "eleven_multilingual_v2"
OPENAI_DEFAULTS = {"tts-1", "tts-1-hd", "gpt-4o-mini-tts"}


def log(msg):
    print(f"[voice] {msg}", flush=True)


def fallback(reason):
    log(f"ElevenLabs disabled, using browser voices: {reason}")
    os.environ["AUDIO_TTS_ENGINE"] = ""


def elevenlabs_voices(key):
    """Return the account's voice ids, or None if the key can't list voices."""
    base = os.environ.get("ELEVENLABS_API_BASE_URL", "https://api.elevenlabs.io").rstrip("/")
    req = urllib.request.Request(f"{base}/v1/voices", headers={"xi-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return [v["voice_id"] for v in json.load(resp).get("voices", [])]
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        # A scoped key without voices_read can still do TTS: keep it, skip the voice check.
        if e.code in (401, 403) and "missing_permissions" in body:
            log("key can't list voices (missing voices_read permission); skipping voice check")
            return None
        raise RuntimeError(f"ElevenLabs rejected the key (HTTP {e.code})")
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"can't reach ElevenLabs ({getattr(e, 'reason', e)})")


def check_voice_config():
    if os.environ.get("AUDIO_TTS_ENGINE", "").strip().lower() != "elevenlabs":
        return
    os.environ["AUDIO_TTS_ENGINE"] = "elevenlabs"

    key = os.environ.get("AUDIO_TTS_API_KEY", "").strip()
    if not key:
        return fallback("AUDIO_TTS_API_KEY is not set")

    if os.environ.get("AUDIO_TTS_MODEL", "") in ("", *OPENAI_DEFAULTS):
        os.environ["AUDIO_TTS_MODEL"] = ELEVENLABS_DEFAULT_MODEL

    try:
        voices = elevenlabs_voices(key)
    except RuntimeError as e:
        return fallback(str(e))

    if voices is not None:
        if not voices:
            return fallback("the account has no voices")
        if os.environ.get("AUDIO_TTS_VOICE") not in voices:
            log(f"voice {os.environ.get('AUDIO_TTS_VOICE')!r} not in this account, using {voices[0]}")
            os.environ["AUDIO_TTS_VOICE"] = voices[0]

    log(f"ElevenLabs on (model {os.environ['AUDIO_TTS_MODEL']}, voice {os.environ.get('AUDIO_TTS_VOICE')})")


if __name__ == "__main__":
    try:
        check_voice_config()
    except Exception as e:  # the check must never stop the app from starting
        fallback(f"unexpected error in voice check: {e!r}")
    if not os.environ.get("AUDIO_TTS_ENGINE"):
        log("text-to-speech: browser voices")
    sys.stdout.flush()
    os.chdir("/app/backend")
    os.execvp("bash", ["bash", "start.sh"])
