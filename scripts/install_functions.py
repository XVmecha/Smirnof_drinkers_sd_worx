"""Install/update every function in /app/hackathon/functions into Open WebUI and activate it.

Runs inside the open-webui container (see `make functions`). Only works with
WEBUI_AUTH=False: it signs in as Open WebUI's built-in no-auth admin.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://localhost:8080/api/v1"
FUNCTIONS_DIR = Path("/app/hackathon/functions")


def call(method, path, token=None, body=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or "null")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        sys.exit(f"{method} {path} failed: {e.code} {e.read().decode()[:300]}")


def docstring_field(source, key):
    for line in source.splitlines()[:10]:
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return ""


def main():
    token = call("POST", "/auths/signin", body={"email": "admin@localhost", "password": "admin"})["token"]
    # Look up existing ids from the list: GET /functions/id/{id} answers 401, not 404, for a missing id.
    installed = {f["id"] for f in call("GET", "/functions/", token) or []}

    for path in sorted(FUNCTIONS_DIR.glob("*.py")):
        fid = path.stem
        content = path.read_text()
        form = {
            "id": fid,
            "name": docstring_field(content, "title") or fid,
            "content": content,
            "meta": {"description": docstring_field(content, "description")},
        }
        existing = fid in installed
        fn = call("POST", f"/functions/id/{fid}/update" if existing else "/functions/create", token, form)
        if not fn.get("is_active"):
            fn = call("POST", f"/functions/id/{fid}/toggle", token)
        print(f"{'updated' if existing else 'installed'}: {fid} (active={fn.get('is_active')})")


if __name__ == "__main__":
    main()
