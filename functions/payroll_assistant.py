"""
title: Payroll Assistant (Trust Card)
description: Sends the question to our FastAPI backend and shows a trust card: the documents the rules selected, their facts, and the AI's ranking and notes.
version: 0.2.0
"""

# Open WebUI "pipe" function. Each consultant in CONSULTANTS (data/users.json)
# shows up as its own model in the dropdown: picking a model = who is logged in.
# The client is detected by the backend from the question, and access is checked there.
#
# The rules decide, the AI explains: the backend's code picks the documents and computes
# their facts ("flags"); the AI only ranks them and notes what stands out. It never answers
# the question itself.
#
# Backend contract (POST {TRUST_API_URL}):
#   request:  {"question": str, "consultant_id": str, "messages": [{"role","content"}, ...]}
#   response: {"status": "answered" | "clarify" | "denied" | "not_found",
#              "answer": str (spoken summary, cites sources as [1], [2] in `sources` order),
#              "sources": [{catalog fields as stored: "id","title","layer","country","domain",
#                           "client_id","entity","source_type","version","status","owner",
#                           "owner_status","last_updated","supersedes","overrides";
#                           "group": "ranking" | "other_country" | "suspicious",
#                           "flags": [str], "note": str (the AI's), "excerpt": str}],
#              "notes": [str], "shown": [ids given to the AI],
#              "context": {"consultant","client","client_id","entity","country","sector"},
#              "expert": str, "model": str}
# Leave TRUST_API_URL empty to use the built-in mock response (for UI work).

import html
import json
import os

import aiohttp
from pydantic import BaseModel, Field

# Mirrors data/users.json.
DEFAULT_CONSULTANTS = [
    {"id": "U-001", "name": "Emma Wouters"},
    {"id": "U-002", "name": "Lucas Verbeke"},
]

# Flags computed by the backend's code, in words. Links carry a document id after the colon.
FLAG_WORDS = {"supersedes": "replaces {}", "superseded_by": "replaced by {}", "overrides": "overrides {}",
              "overridden_by": "overridden by {}", "owner_left": "owner left", "owner_moved": "owner moved team",
              "no_owner": "no owner", "draft": "draft", "informal": "informal", "stale": "older than 2 years"}
WARN_FLAGS = {"superseded_by", "overridden_by", "owner_left", "owner_moved", "no_owner", "draft", "informal", "stale"}


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
        FALLBACK_EXPERT: str = "Knowledge Team"
        CONSULTANTS: str = Field(
            default=json.dumps(DEFAULT_CONSULTANTS),
            description="JSON list of consultants (data/users.json); each becomes a model in the dropdown.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ---- Open WebUI hooks -------------------------------------------------

    def pipes(self):
        return [{"id": c["id"].lower(), "name": f"Payroll Assistant · {c['name']}"} for c in self._consultants()]

    async def pipe(self, body: dict, __event_emitter__=None) -> str:
        consultant = self._consultant_for_model(body.get("model", ""))
        messages = [
            {"role": m.get("role"), "content": _text(m.get("content"))}
            for m in body.get("messages", [])
            if m.get("role") in ("user", "assistant")
        ]
        question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

        await _emit(__event_emitter__, "status", {"description": "Checking sources…", "done": False})
        try:
            if self.valves.TRUST_API_URL:
                data = await self._call_backend(question, consultant, messages)
            else:
                data = _mock_response(consultant)
        except BackendError as e:
            await _emit(__event_emitter__, "status", {"description": "Backend error", "done": True})
            return f"⚠️ The knowledge backend returned an error: {e}"
        except Exception as e:
            await _emit(__event_emitter__, "status", {"description": "Backend unreachable", "done": True})
            return f"⚠️ Could not reach the knowledge backend: `{type(e).__name__}`. Is the FastAPI service running?"

        status = data.get("status", "answered")
        if status == "clarify":  # just a question back, nothing to vouch for
            await _emit(__event_emitter__, "status", {"description": "Need more context", "done": True})
            return data.get("answer") or "Which client is this about?"

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

        if status == "denied":
            card = build_denied_card(data, consultant)
        else:
            card = build_trust_card(data, consultant, self.valves.FALLBACK_EXPERT)
        await _emit(__event_emitter__, "embeds", {"embeds": [card]})
        await _emit(__event_emitter__, "status", {"description": "Sources checked", "done": True})
        return data.get("answer") or "No answer returned."

    # ---- helpers ----------------------------------------------------------

    def _consultants(self) -> list[dict]:
        try:
            consultants = json.loads(self.valves.CONSULTANTS)
            if isinstance(consultants, list) and all("id" in c and "name" in c for c in consultants):
                return consultants
        except (ValueError, TypeError):
            pass
        return DEFAULT_CONSULTANTS

    def _consultant_for_model(self, model_id: str) -> dict:
        sub_id = model_id.split(".", 1)[1] if "." in model_id else ""
        return next((c for c in self._consultants() if c["id"].lower() == sub_id.lower()), self._consultants()[0])

    async def _call_backend(self, question: str, consultant: dict, messages: list) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.valves.TRUST_API_TOKEN:
            headers["Authorization"] = f"Bearer {self.valves.TRUST_API_TOKEN}"
        timeout = aiohttp.ClientTimeout(total=self.valves.TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self.valves.TRUST_API_URL,
                json={"question": question, "consultant_id": consultant["id"], "messages": messages},
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    try:
                        detail = (await resp.json()).get("detail")
                    except (aiohttp.ContentTypeError, ValueError):
                        detail = None
                    raise BackendError(f"HTTP {resp.status}" + (f": {detail}" if isinstance(detail, str) else ""))
                return await resp.json()


# ---- trust card --------------------------------------------------------------


def build_trust_card(data: dict, consultant: dict, fallback_expert: str) -> str:
    ctx = data.get("context") or {}
    sources = data.get("sources") or []
    e = html.escape

    scope = " · ".join(filter(None, [ctx.get("entity") or ctx.get("client"), ctx.get("country"), ctx.get("sector")]))
    parts = [f"<div class='card'><div class='head'><div><div class='k'>Trust card · {e(consultant.get('name', ''))}</div>"
             f"<div class='client'>{e(scope or 'General question')}</div></div>"
             f"<div class='badge'>{len(data.get('shown') or sources)} documents checked</div></div>",
             "<div class='why'><span class='flag'>Labels</span> are facts computed by rules from the document catalog. "
             "<span class='ai'>Order and notes</span> are the AI's suggestion. The AI does not answer the question.</div>"]

    n = 0
    for group, heading in (("ranking", "Ranked by relevance"), ("other_country", "Other country or entity")):
        items = [s for s in sources if s.get("group") == group]
        if items:
            rows = []
            for s in items:
                n += 1
                rows.append(_doc_row(n, s))
            parts.append(f"<div class='sec'><div class='k'>{heading}</div><ol>{''.join(rows)}</ol></div>")

    suspicious = [s for s in sources if s.get("group") == "suspicious"]
    if suspicious:
        rows = "".join(f"<li><span class='x'>⚠</span> {_title(s)} <span class='ai'>{e(s.get('note') or '')}</span></li>"
                       for s in suspicious)
        parts.append(f"<div class='sec'><div class='k warn'>Suspicious, not ranked</div><ul>{rows}</ul></div>")

    notes = data.get("notes") or []
    if notes:
        parts.append("<div class='sec'><div class='k'>Across documents</div>"
                     + "".join(f"<div class='ai'>{e(x)}</div>" for x in notes) + "</div>")

    expert = data.get("expert") or fallback_expert
    parts.append(f"<div class='sec ask'><div class='k'>Not sure? Ask</div><div>🙋 {e(expert)}</div></div>")
    if data.get("model"):
        parts.append(f"<div class='foot muted'>Ranked by {e(str(data['model']))}</div>")
    parts.append("</div>")
    return _page("".join(parts))


def _doc_row(n: int, s: dict) -> str:
    e = html.escape
    chips = "".join(f"<span class='chip{' w' if f.split(':')[0] in WARN_FLAGS else ''}'>{e(_flag_words(f))}</span>"
                    for f in s.get("flags") or [])
    owner = s.get("owner") or "no owner"
    if s.get("owner_status") in ("left", "moved_team"):
        owner += f" ({s['owner_status'].replace('_', ' ')})"
    meta = " · ".join(filter(None, [s.get("id"), s.get("layer"), s.get("source_type"), s.get("status"), s.get("entity"),
                                    s.get("country"), owner, f"updated {s['last_updated']}" if s.get("last_updated") else ""]))
    note = f"<div class='ai'>{e(s['note'])}</div>" if s.get("note") else ""
    return f"<li value='{n}'><div class='doc'>{_title(s)} {chips}</div>{note}<div class='muted small'>{e(meta)}</div></li>"


def _flag_words(flag: str) -> str:
    name, _, target = flag.partition(":")
    return FLAG_WORDS.get(name, name).format(target)


def build_denied_card(data: dict, consultant: dict) -> str:
    e = html.escape
    return _page(
        f"<div class='card'><div class='head'><div><div class='k'>Access check · {e(consultant.get('name', ''))}</div>"
        f"<div class='client'>Not assigned to this client</div></div><div class='badge low'>DENIED</div></div>"
        f"<div class='why'>Checked against the consultant's client list before any search. No documents were "
        f"read and nothing about the client was sent to the AI.</div></div>"
    )


# ---- rendering helpers ----------------------------------------------------


def _title(s: dict) -> str:
    title = html.escape(s.get("title") or str(s.get("id") or "Untitled"))
    url = s.get("url")
    return f"<a href='{html.escape(url)}' target='_blank' rel='noopener noreferrer'>{title}</a>" if _safe_url(url) else title


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
.badge{background:var(--soft);color:var(--muted)}ol{margin:6px 0 0;padding-left:22px}ol li{margin:8px 0}.small{font-size:12px}
.chip,.flag{display:inline-block;font-size:11px;font-weight:600;padding:1px 7px;border-radius:999px;margin:0 2px;
background:var(--soft);border:1px solid var(--line);color:var(--muted);vertical-align:1px}
.chip.w{background:var(--md);color:var(--warn);border-color:transparent}.ai{font-style:italic;margin:2px 0}
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


def _safe_url(url) -> bool:
    return isinstance(url, str) and url.startswith(("https://", "http://"))


def _mock_response(consultant: dict) -> dict:
    """Stand-in for the FastAPI backend (README demo scenario 1) so the UI works without it."""
    def src(id, title, group, flags, note, **meta):
        return {"id": id, "title": title, "group": group, "flags": flags, "note": note, **meta}
    return {
        "status": "answered",
        "answer": ("I found seven documents for Brouwerij Delta NV and one for the Netherlands. The client agreement is "
                   "ranked first [1]: it names this entity and overrides the Belgian legal rule [2]. Note that a Teams "
                   "chat [4] still mentions 125%, which comes from the outdated 2023 rules [5]."),
        "sources": [
            src("CL-10045-BE-001", "Company Agreement Overtime - Brouwerij Delta NV (BE)", "ranking", ["overrides:BE-TIME-001"],
                "Names Brouwerij Delta NV; states 175% for weekend overtime and overrides BE-TIME-001.",
                layer="client", source_type="agreement", status="active", entity="Brouwerij Delta NV", country="BE",
                owner="Nina Claes (Account Team Antwerp)", owner_status="active", last_updated="2026-04-02"),
            src("BE-TIME-001", "Overtime Rules Belgium (legal summary)", "ranking",
                ["supersedes:BE-TIME-002", "overridden_by:CL-10045-BE-001"],
                "Current Belgian legal rule (150% for Saturday), overridden for this entity by CL-10045-BE-001.",
                layer="legal", source_type="policy", status="active", country="BE",
                owner="Sarah Janssens (Payroll Compliance BE)", owner_status="active", last_updated="2026-06-15"),
            src("CL-10045-HANDOVER", "Portfolio Handover Note - Brouwerij Delta", "ranking", ["owner_moved"],
                "Background note pointing to a special client weekend rate; the owner has moved team.",
                layer="client", source_type="handover", status="active", country="ALL",
                owner="Pieter Lambrecht", owner_status="moved_team", last_updated="2026-09-01"),
            src("INF-TEAMS-001", "Teams chat export - #payroll-be-questions", "ranking", ["no_owner", "informal"],
                "Informal chat stating 125% for Saturday; this matches the superseded BE-TIME-002.",
                layer="informal", source_type="chat", status="informal", country="BE", owner_status="none",
                last_updated="2025-11-20"),
            src("BE-TIME-002", "Overtime Rules Belgium (legal summary)", "ranking",
                ["superseded_by:BE-TIME-001", "owner_left", "stale"],
                "Superseded by BE-TIME-001 and the owner has left; states 125% for Saturday.",
                layer="legal", source_type="policy", status="active", country="BE", owner="Tom Peeters",
                owner_status="left", last_updated="2023-02-10"),
            src("CL-10045-NL-001", "Company Agreement Overtime - Brouwerij Delta B.V. (NL)", "other_country",
                ["overrides:NL-TIME-001"], "Agreement for the Dutch entity (150% for Saturday); does not cover Antwerp.",
                layer="client", source_type="agreement", status="active", entity="Brouwerij Delta B.V.", country="NL",
                owner="Sanne Bakker (HR NL)", owner_status="active", last_updated="2026-02-15"),
        ],
        "notes": ["Three Saturday rates appear (175%, 150%, 125%); the override and supersedes links explain all of them "
                  "except the informal chat."],
        "shown": ["CL-10045-BE-001", "BE-TIME-001", "CL-10045-HANDOVER", "INF-TEAMS-001", "BE-TIME-002", "CL-10045-NL-001",
                  "BE-SEC-118", "CL-10045-PROFILE"],
        "context": {"consultant": consultant.get("name"), "client": "Brouwerij Delta", "client_id": "CL-10045",
                    "entity": "Brouwerij Delta NV", "country": "BE", "sector": "PC 118"},
        "expert": "Sarah Janssens (Overtime and working time)",
        "model": "mock",
    }
