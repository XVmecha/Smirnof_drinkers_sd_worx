"""
title: Payroll Assistant (Trust Card)
description: Sends the question to our FastAPI backend and shows the answer with a trust card: source, freshness, owner, country/client fit, conflicts and who to ask.
version: 0.1.0
"""

# Open WebUI "pipe" function. Each client in CLIENTS shows up as its own model
# in the model dropdown, so picking a model = picking the client you work for.
#
# Backend contract (POST {TRUST_API_URL}):
#   request:  {"question": str, "client": {"id","name","country","joint_committee"},
#              "messages": [{"role","content"}, ...]}
#   response: {"answer": str (markdown, cite sources as [1], [2] in `sources` order),
#              "sources": [{"id","title","doc_type","country","joint_committees",
#                           "owner","expert","updated","superseded_by","url","excerpt"}],
#              "conflicts": [{"a": source id, "b": source id, "topic",
#                             "a_says", "b_says"}],
#              "confidence": optional int 0-100, overrides the computed score}
# Leave TRUST_API_URL empty to use the built-in mock response (for UI work).

import html
import json
import os
from datetime import date, datetime
from typing import Optional

import aiohttp
from pydantic import BaseModel, Field

DEFAULT_CLIENTS = [
    {"id": "janssens", "name": "Brouwerij Janssens NV", "country": "BE", "joint_committee": "118"},
    {"id": "devries", "name": "De Vries Logistiek BV", "country": "NL", "joint_committee": ""},
    {"id": "peeters", "name": "Peeters Retail BV", "country": "BE", "joint_committee": "201"},
]

OFFICIAL_TYPES = {"policy", "annex", "regulation", "procedure", "directory"}
INFORMAL_TYPES = {"chat", "email", "note"}
UNREVIEWED_TYPES = {"draft", "faq"}


class BackendError(Exception):
    pass


class Pipe:
    class Valves(BaseModel):
        TRUST_API_URL: str = Field(
            default=os.environ.get("TRUST_API_URL", ""),
            description="FastAPI endpoint, e.g. http://host.docker.internal:8000/ask. Empty = mock data.",
        )
        TRUST_API_TOKEN: str = Field(
            default=os.environ.get("TRUST_API_TOKEN", ""),
            description="Optional bearer token sent to the backend.",
        )
        TIMEOUT_SECONDS: int = 60
        STALE_AFTER_DAYS: int = 365
        FALLBACK_EXPERT: str = "Payroll Knowledge Desk"
        CLIENTS: str = Field(
            default=json.dumps(DEFAULT_CLIENTS),
            description="JSON list of clients; each becomes a model in the dropdown.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ---- Open WebUI hooks -------------------------------------------------

    def pipes(self):
        return [
            {"id": c["id"], "name": f"Payroll Assistant · {c['name']} ({c['country']})"}
            for c in self._clients()
        ]

    async def pipe(self, body: dict, __event_emitter__=None) -> str:
        client = self._client_for_model(body.get("model", ""))
        messages = [
            {"role": m.get("role"), "content": _text(m.get("content"))}
            for m in body.get("messages", [])
            if m.get("role") in ("user", "assistant")
        ]
        question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

        await _emit(__event_emitter__, "status", {"description": "Checking sources…", "done": False})
        try:
            if self.valves.TRUST_API_URL:
                data = await self._call_backend(question, client, messages)
            else:
                data = _mock_response(client)
        except BackendError as e:
            await _emit(__event_emitter__, "status", {"description": "Backend error", "done": True})
            return f"⚠️ The knowledge backend returned an error: {e}"
        except Exception as e:
            await _emit(__event_emitter__, "status", {"description": "Backend unreachable", "done": True})
            return f"⚠️ Could not reach the knowledge backend: `{type(e).__name__}`. Is the FastAPI service running?"

        sources = data.get("sources") or []
        for s in sources:
            await _emit(
                __event_emitter__,
                "source",
                {
                    "source": {"name": s.get("title") or s.get("id"), **({"url": s["url"]} if _safe_url(s.get("url")) else {})},
                    "document": [s.get("excerpt") or ""],
                    "metadata": [{"source": s.get("title") or s.get("id"), "name": s.get("title") or s.get("id")}],
                },
            )

        card = build_trust_card(data, client, self.valves.STALE_AFTER_DAYS, self.valves.FALLBACK_EXPERT)
        await _emit(__event_emitter__, "embeds", {"embeds": [card]})
        await _emit(__event_emitter__, "status", {"description": "Sources checked", "done": True})
        return data.get("answer") or "No answer returned."

    # ---- helpers ----------------------------------------------------------

    def _clients(self) -> list[dict]:
        try:
            clients = json.loads(self.valves.CLIENTS)
            if isinstance(clients, list) and all("id" in c for c in clients):
                return clients
        except (ValueError, TypeError):
            pass
        return DEFAULT_CLIENTS

    def _client_for_model(self, model_id: str) -> dict:
        sub_id = model_id.split(".", 1)[1] if "." in model_id else ""
        return next((c for c in self._clients() if c["id"] == sub_id), self._clients()[0])

    async def _call_backend(self, question: str, client: dict, messages: list) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.valves.TRUST_API_TOKEN:
            headers["Authorization"] = f"Bearer {self.valves.TRUST_API_TOKEN}"
        timeout = aiohttp.ClientTimeout(total=self.valves.TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self.valves.TRUST_API_URL,
                json={"question": question, "client": client, "messages": messages},
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    try:
                        detail = (await resp.json()).get("detail")
                    except (aiohttp.ContentTypeError, ValueError):
                        detail = None
                    raise BackendError(f"HTTP {resp.status}" + (f": {detail}" if isinstance(detail, str) else ""))
                return await resp.json()


# ---- trust rules (deterministic, so the card is explainable) --------------


def assess_source(s: dict, client: dict, stale_after_days: int) -> dict:
    """Return {'excluded': [reasons], 'checks': [(label, ok, detail)], 'penalties': [(points, why)], 'score'}."""
    excluded, checks, penalties = [], [], []

    if s.get("superseded_by"):
        excluded.append(f"Replaced by {s['superseded_by']}")

    doc_country = (s.get("country") or "").upper()
    client_country = (client.get("country") or "").upper()
    if doc_country and doc_country not in ("ALL", "EU") and client_country and doc_country != client_country:
        excluded.append(f"Wrong country ({doc_country}, client is {client_country})")
    elif doc_country:
        checks.append(("Country", True, doc_country if doc_country not in ("ALL", "EU") else f"{doc_country} countries"))
    else:
        checks.append(("Country", None, "not stated"))
        penalties.append((10, "country not stated"))

    jcs = _as_list(s.get("joint_committees"))
    client_jc = client.get("joint_committee") or ""
    if jcs and "ALL" not in [j.upper() for j in jcs] and client_jc and client_jc not in jcs:
        excluded.append(f"Not for joint committee {client_jc} (covers {', '.join(jcs)})")
    elif jcs and client_jc:
        checks.append(("Client scope", True, f"JC {client_jc}"))

    updated = _parse_date(s.get("updated"))
    if updated is None:
        checks.append(("Freshness", None, "no last-updated date"))
        penalties.append((20, "no last-updated date"))
    else:
        age = (date.today() - updated).days
        stale = age > stale_after_days
        checks.append(("Freshness", not stale, f"updated {updated.isoformat()} ({_age(age)} ago)"))
        if stale:
            penalties.append((30, f"older than {stale_after_days} days"))

    if s.get("owner"):
        checks.append(("Owner", True, s["owner"]))
    else:
        checks.append(("Owner", False, "no owner"))
        penalties.append((20, "no owner"))

    doc_type = (s.get("doc_type") or "").lower()
    if doc_type in INFORMAL_TYPES:
        checks.append(("Type", False, f"informal ({doc_type})"))
        penalties.append((30, f"informal {doc_type}"))
    elif doc_type in UNREVIEWED_TYPES:
        checks.append(("Type", False, f"{doc_type}, not reviewed"))
        penalties.append((20, f"{doc_type}, not reviewed"))
    elif doc_type in OFFICIAL_TYPES:
        checks.append(("Type", True, doc_type))
    elif doc_type:
        checks.append(("Type", None, doc_type))

    score = max(0, 100 - sum(p for p, _ in penalties))
    return {"excluded": excluded, "checks": checks, "penalties": penalties, "score": score}


def build_trust_card(data: dict, client: dict, stale_after_days: int, fallback_expert: str) -> str:
    sources = data.get("sources") or []
    by_id = {}
    for i, s in enumerate(sources, start=1):
        s = {**s, "_n": i, "_a": assess_source(s, client, stale_after_days)}
        by_id[str(s.get("id") or i)] = s
    considered = [s for s in by_id.values() if not s["_a"]["excluded"]]
    excluded = [s for s in by_id.values() if s["_a"]["excluded"]]
    # Only conflicts between sources that both apply count; excluded ones are explained separately.
    applicable = {k for k, s in by_id.items() if not s["_a"]["excluded"]}
    conflicts = [c for c in (data.get("conflicts") or []) if str(c.get("a")) in applicable and str(c.get("b")) in applicable]

    best = max(considered, key=lambda s: s["_a"]["score"], default=None)
    reasons = []
    if best is None:
        confidence = 0
        reasons.append("no applicable source found")
    else:
        confidence = best["_a"]["score"]
        reasons += [f"−{p} {why}" for p, why in best["_a"]["penalties"]]
        best_id = str(best.get("id") or best["_n"])
        if any(best_id in (str(c.get("a")), str(c.get("b"))) for c in conflicts):
            confidence = max(0, confidence - 30)
            reasons.append("−30 another source disagrees")
    if isinstance(data.get("confidence"), (int, float)):
        confidence = int(data["confidence"])

    level = "high" if confidence >= 75 else "medium" if confidence >= 50 else "low"
    e = html.escape

    parts = [f"<div class='card'><div class='head'><div><div class='k'>Trust card</div>"
             f"<div class='client'>{e(client.get('name', ''))} · {e(client.get('country', ''))}"
             f"{' · JC ' + e(client['joint_committee']) if client.get('joint_committee') else ''}</div></div>"
             f"<div class='badge {level}'>{level.upper()} · {confidence}/100</div></div>"]
    if reasons:
        parts.append(f"<div class='why'>{e(' · '.join(reasons))}</div>")

    if best:
        rows = "".join(
            f"<tr><td>{_icon(ok)}</td><th>{e(label)}</th><td>{e(detail)}</td></tr>"
            for label, ok, detail in best["_a"]["checks"]
        )
        parts.append(f"<div class='sec'><div class='k'>Main source</div>"
                     f"<div class='doc'>[{best['_n']}] {_title(best)}</div><table>{rows}</table></div>")

    if conflicts:
        items = []
        for c in conflicts:
            a, b = by_id[str(c["a"])], by_id[str(c["b"])]
            winner = a if a["_a"]["score"] >= b["_a"]["score"] else b
            items.append(
                f"<div class='conf'><div class='k warn'>⚠ Conflict{': ' + e(c['topic']) if c.get('topic') else ''}</div>"
                f"<div class='cols'>{_side(a, c.get('a_says'))}{_side(b, c.get('b_says'))}</div>"
                f"<div class='hint'>→ More reliable: <b>{e(winner.get('title') or '')}</b> "
                f"({winner['_a']['score']}/100)</div></div>"
            )
        parts.append("<div class='sec'>" + "".join(items) + "</div>")

    if excluded:
        rows = "".join(
            f"<li><span class='x'>✗</span> [{s['_n']}] {_title(s)} <span class='muted'>— {e('; '.join(s['_a']['excluded']))}</span></li>"
            for s in excluded
        )
        parts.append(f"<details class='sec' open><summary class='k'>Found but excluded ({len(excluded)})</summary><ul>{rows}</ul></details>")

    if level != "high" or conflicts:
        expert = (best or {}).get("expert") or (best or {}).get("owner") or fallback_expert
        parts.append(f"<div class='sec ask'><div class='k'>Not sure? Ask</div><div>🙋 {e(expert)}</div></div>")

    if data.get("model"):
        parts.append(f"<div class='foot muted'>Answered by {e(str(data['model']))}</div>")

    parts.append("</div>")
    return _page("".join(parts))


# ---- rendering helpers ----------------------------------------------------


def _title(s: dict) -> str:
    title = html.escape(s.get("title") or str(s.get("id") or "Untitled"))
    url = s.get("url")
    return f"<a href='{html.escape(url)}' target='_blank' rel='noopener noreferrer'>{title}</a>" if _safe_url(url) else title


def _side(s: dict, says: Optional[str]) -> str:
    meta = " · ".join(filter(None, [s.get("doc_type"), s.get("owner") or "no owner", s.get("updated")]))
    return (f"<div class='side'><div class='doc'>[{s['_n']}] {_title(s)}</div>"
            f"<blockquote>{html.escape(says or '')}</blockquote><div class='muted'>{html.escape(meta)} · {s['_a']['score']}/100</div></div>")


def _icon(ok) -> str:
    return "<span class='ok'>✓</span>" if ok is True else "<span class='x'>✗</span>" if ok is False else "<span class='muted'>?</span>"


def _page(body: str) -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><style>
:root{--bg:#fff;--fg:#1f2328;--muted:#656d76;--line:#d8dee4;--soft:#f6f8fa;--ok:#1a7f37;--bad:#cf222e;--warn:#9a6700;
--hi:#dafbe1;--md:#fff8c5;--lo:#ffebe9}
@media (prefers-color-scheme:dark){:root{--bg:#171717;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--soft:#212121;
--ok:#3fb950;--bad:#f85149;--warn:#d29922;--hi:#12351d;--md:#3b2e05;--lo:#42181a}}
*{box-sizing:border-box}body{margin:0;background:transparent;color:var(--fg);font:14px/1.45 system-ui,-apple-system,sans-serif}
.card{border:1px solid var(--line);border-radius:14px;padding:14px 16px;background:var(--bg)}
.head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}
.client{font-weight:600}.badge{padding:4px 10px;border-radius:999px;font-weight:700;font-size:12px}
.high{background:var(--hi);color:var(--ok)}.medium{background:var(--md);color:var(--warn)}.low{background:var(--lo);color:var(--bad)}
.why{color:var(--muted);font-size:12px;margin-top:6px}.sec{border-top:1px solid var(--line);margin-top:12px;padding-top:10px}
.doc{font-weight:600;margin:2px 0 6px}table{border-collapse:collapse}td,th{padding:2px 8px 2px 0;text-align:left;vertical-align:top}
th{font-weight:500;color:var(--muted);white-space:nowrap}.ok{color:var(--ok)}.x{color:var(--bad)}.muted{color:var(--muted)}
.warn{color:var(--warn)}.cols{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:6px}
@media (max-width:560px){.cols{grid-template-columns:1fr}}
.side{background:var(--soft);border-radius:10px;padding:8px 10px}blockquote{margin:4px 0;padding-left:8px;border-left:3px solid var(--warn)}
.hint{margin-top:6px}ul{margin:6px 0 0;padding-left:0;list-style:none}li{margin:3px 0}summary{cursor:pointer}
a{color:inherit}.foot{font-size:11px;margin-top:10px}.ask div:last-child{font-weight:600;margin-top:2px}
</style></head><body>""" + body + """<script>
const post=()=>parent.postMessage({type:'iframe:height',height:document.documentElement.scrollHeight},'*');
new ResizeObserver(post).observe(document.body);addEventListener('load',post);document.querySelectorAll('details').forEach(d=>d.addEventListener('toggle',post));
</script></body></html>"""


# ---- small utils ----------------------------------------------------------


async def _emit(emitter, type_: str, data: dict):
    if emitter:
        await emitter({"type": type_, "data": data})


def _text(content) -> str:
    if isinstance(content, list):  # multimodal message parts
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def _as_list(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).replace(";", ",").split(",") if x.strip()]


def _parse_date(v) -> Optional[date]:
    try:
        return datetime.fromisoformat(str(v)[:10]).date() if v else None
    except ValueError:
        return None


def _age(days: int) -> str:
    if days < 60:
        return f"{days} days"
    if days < 730:
        return f"{days // 30} months"
    return f"{days // 365} years"


def _safe_url(url) -> bool:
    return isinstance(url, str) and url.startswith(("https://", "http://"))


def _mock_response(client: dict) -> dict:
    """Stand-in for the FastAPI backend so the UI can be built and demoed without it."""
    return {
        "answer": (
            "Yes. Since 1 January 2026 the voluntary overtime cap for Belgian employees is **180 hours per year** "
            "[1], and joint committee 118 follows the national rule without a sector-specific limit [2].\n\n"
            "Note: a Teams message still mentions 120 hours [3]; that figure comes from the 2023 policy, which has since been replaced [4]."
        ),
        "sources": [
            {"id": "be-ot-2026", "title": "BE Overtime Policy 2026", "doc_type": "policy", "country": "BE",
             "joint_committees": "ALL", "owner": "Payroll BE Team", "expert": "An Claes (Payroll BE Team)",
             "updated": "2026-07-15", "excerpt": "The voluntary overtime cap is 180 hours per calendar year."},
            {"id": "jc118-annex", "title": "Joint Committee 118 Annex", "doc_type": "annex", "country": "BE",
             "joint_committees": "118", "owner": "Sector Desk Food", "updated": "2026-03-02",
             "excerpt": "JC 118 applies the national overtime rules; no sector-specific cap."},
            {"id": "teams-ot", "title": "Teams export: #payroll-be (12 Mar 2025)", "doc_type": "chat", "country": "BE",
             "owner": "", "updated": "2025-03-12", "excerpt": "overtime cap is still 120h, don't change the configs"},
            {"id": "be-ot-2023", "title": "BE Overtime Policy 2023", "doc_type": "policy", "country": "BE",
             "owner": "Payroll BE Team", "updated": "2023-02-10", "superseded_by": "BE Overtime Policy 2026",
             "excerpt": "The voluntary overtime cap is 120 hours per calendar year."},
            {"id": "nl-ot", "title": "NL Overtime Guidelines", "doc_type": "policy", "country": "NL",
             "owner": "Payroll NL Team", "updated": "2026-05-20", "excerpt": "Overtime is governed by the applicable CAO."},
        ],
        "conflicts": [
            {"a": "be-ot-2026", "b": "teams-ot", "topic": "overtime cap",
             "a_says": "cap of 180 hours per calendar year", "b_says": "overtime cap is still 120h"},
        ],
    }
