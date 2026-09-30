"""
title: Payroll Assistant (Trust Card)
description: Sends the question to our FastAPI backend and shows the answer with a trust card: source, freshness, owner, country/client fit, conflicts and who to ask.
version: 0.1.0
"""

# Open WebUI "pipe" function. Each consultant in CONSULTANTS (data/users.json)
# shows up as its own model in the dropdown: picking a model = who is logged in.
# The client is detected by the backend from the question, and access is checked there.
#
# Backend contract (POST {TRUST_API_URL}):
#   request:  {"question": str, "consultant_id": str, "messages": [{"role","content"}, ...]}
#   response: {"status": "answered" | "clarify" | "denied" | "not_found",
#              "answer": str (markdown, cites sources as [1], [2] in `sources` order),
#              "sources": [{"id","title","doc_type","layer","country","entity","owner",
#                           "owner_status","updated","superseded_by","overrides",
#                           "excerpt","exclude_reasons"}],
#              "conflicts": [{"a": source id, "b": source id, "topic", "a_says", "b_says"}],
#              "context": {"consultant","client","client_id","entity","country","sector"},
#              "expert": str, "model": str,
#              "confidence": optional int 0-100, overrides the computed score}
# Leave TRUST_API_URL empty to use the built-in mock response (for UI work).

import html
import json
import os
from datetime import date, datetime
from typing import Optional

import aiohttp
from pydantic import BaseModel, Field

# Mirrors data/users.json.
DEFAULT_CONSULTANTS = [
    {"id": "U-001", "name": "Emma Wouters"},
    {"id": "U-002", "name": "Lucas Verbeke"},
]

# Most specific wins (README "Precedence"): used to pick the main source on a tie.
LAYER_RANK = {"informal": 0, "legal": 1, "sector": 2, "provider": 3, "client": 4}
OFFICIAL_TYPES = {"policy", "agreement", "annex", "regulation", "procedure", "profile", "handover", "directory"}
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
            card = build_trust_card(data, consultant, self.valves.STALE_AFTER_DAYS, self.valves.FALLBACK_EXPERT)
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


# ---- trust rules (deterministic, so the card is explainable) --------------


def assess_source(s: dict, ctx: dict, stale_after_days: int) -> dict:
    """Return {'excluded': [reasons], 'checks': [(label, ok, detail)], 'penalties': [(points, why)], 'score'}."""
    excluded, checks, penalties = list(s.get("exclude_reasons") or []), [], []

    if s.get("superseded_by"):
        excluded.append(f"Replaced by {s['superseded_by']}")

    doc_country = (s.get("country") or "").upper()
    country = (ctx.get("country") or "").upper()
    if doc_country and doc_country not in ("ALL", "EU") and country and doc_country != country:
        excluded.append(f"Wrong country ({doc_country}, question is about {country})")
    elif doc_country:
        checks.append(("Country", True, doc_country if doc_country not in ("ALL", "EU") else "all countries"))
    else:
        checks.append(("Country", None, "not stated"))
        penalties.append((10, "country not stated"))

    if s.get("entity"):
        checks.append(("Applies to", True, f"{s['entity']} only"))
    if s.get("overrides"):
        checks.append(("Overrides", True, s["overrides"]))

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

    owner_status = (s.get("owner_status") or "").lower()
    if not s.get("owner") or owner_status == "none":
        checks.append(("Owner", False, "no owner"))
        penalties.append((20, "no owner"))
    elif owner_status == "left":
        checks.append(("Owner", False, f"{s['owner']} (left the company)"))
        penalties.append((20, "owner left"))
    elif owner_status == "moved_team":
        checks.append(("Owner", None, f"{s['owner']} (moved team)"))
        penalties.append((10, "owner moved team"))
    else:
        checks.append(("Owner", True, s["owner"]))

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


def build_trust_card(data: dict, consultant: dict, stale_after_days: int, fallback_expert: str) -> str:
    ctx = data.get("context") or {}
    sources = data.get("sources") or []
    by_id = {}
    for i, s in enumerate(sources, start=1):
        s = {**s, "_n": i, "_a": assess_source(s, ctx, stale_after_days)}
        by_id[str(s.get("id") or i)] = s
    considered = [s for s in by_id.values() if not s["_a"]["excluded"]]
    excluded = [s for s in by_id.values() if s["_a"]["excluded"]]
    # Only conflicts between sources that both apply count; excluded ones are explained separately.
    applicable = {k for k, s in by_id.items() if not s["_a"]["excluded"]}
    conflicts = [c for c in (data.get("conflicts") or []) if str(c.get("a")) in applicable and str(c.get("b")) in applicable]

    # Highest trust wins; on a tie the most specific layer (client > provider > sector > legal).
    best = max(considered, key=lambda s: (s["_a"]["score"], LAYER_RANK.get(s.get("layer"), 0)), default=None)
    reasons = []
    if best is None:
        confidence = 0
        reasons.append("no applicable source found")
    else:
        confidence = best["_a"]["score"]
        reasons += [f"−{p} {why}" for p, why in best["_a"]["penalties"]]
        best_id = str(best.get("id") or best["_n"])
        rivals = [by_id[str(c["b"] if str(c.get("a")) == best_id else c["a"])]
                  for c in conflicts if best_id in (str(c.get("a")), str(c.get("b")))]
        if rivals:
            # A much weaker source (e.g. a chat message) disagreeing costs less than a comparable one.
            strongest = max(r["_a"]["score"] for r in rivals)
            if best["_a"]["score"] - strongest >= 30:
                confidence = max(0, confidence - 10)
                reasons.append("−10 a less reliable source disagrees")
            else:
                confidence = max(0, confidence - 30)
                reasons.append("−30 another reliable source disagrees")
    if isinstance(data.get("confidence"), (int, float)):
        confidence = int(data["confidence"])

    level = "high" if confidence >= 75 else "medium" if confidence >= 50 else "low"
    e = html.escape

    scope = " · ".join(filter(None, [ctx.get("entity") or ctx.get("client"), ctx.get("country"), ctx.get("sector")]))
    parts = [f"<div class='card'><div class='head'><div><div class='k'>Trust card · {e(consultant.get('name', ''))}</div>"
             f"<div class='client'>{e(scope or 'General question')}</div></div>"
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
        expert = data.get("expert") or fallback_expert
        parts.append(f"<div class='sec ask'><div class='k'>Not sure? Ask</div><div>🙋 {e(expert)}</div></div>")

    if data.get("model"):
        parts.append(f"<div class='foot muted'>Answered by {e(str(data['model']))}</div>")

    parts.append("</div>")
    return _page("".join(parts))


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


def _mock_response(consultant: dict) -> dict:
    """Stand-in for the FastAPI backend (README demo scenario 1) so the UI works without it."""
    return {
        "status": "answered",
        "answer": (
            "Pay the 4 hours of Saturday overtime at **175%** of the hourly rate: Brouwerij Delta NV's company "
            "agreement overrides the Belgian legal rate of 150% for this entity [1][2]. The handover note flags this "
            "special weekend rate too [3].\n\nA Teams chat still says 125% [5]; that figure comes from the outdated "
            "2023 rules (v2) [4] and should not be used."
        ),
        "sources": [
            {"id": "CL-10045-BE-001", "title": "Company Agreement Overtime - Brouwerij Delta NV (BE) (v1)",
             "doc_type": "agreement", "layer": "client", "country": "BE", "entity": "Brouwerij Delta NV",
             "owner": "Nina Claes (Account Team Antwerp)", "owner_status": "active", "updated": "2026-04-02",
             "overrides": "Overtime Rules Belgium (legal summary) (v3)",
             "excerpt": "Weekend overtime (Saturday and Sunday) is paid at 175% of the hourly rate."},
            {"id": "BE-TIME-001", "title": "Overtime Rules Belgium (legal summary) (v3)", "doc_type": "policy",
             "layer": "legal", "country": "BE", "owner": "Sarah Janssens (Payroll Compliance BE)",
             "owner_status": "active", "updated": "2026-06-15",
             "excerpt": "Overtime on weekdays and Saturdays is paid at 150% of the hourly rate."},
            {"id": "CL-10045-HANDOVER", "title": "Portfolio Handover Note - Brouwerij Delta (v1)", "doc_type": "handover",
             "layer": "client", "country": "ALL", "owner": "Pieter Lambrecht", "owner_status": "moved_team",
             "updated": "2026-09-01",
             "excerpt": "Watch out: weekend overtime in Belgium has a special client rate - see the BE agreement."},
            {"id": "BE-TIME-002", "title": "Overtime Rules Belgium (legal summary) (v2)", "doc_type": "policy",
             "layer": "legal", "country": "BE", "owner": "Tom Peeters", "owner_status": "left", "updated": "2023-02-10",
             "superseded_by": "Overtime Rules Belgium (legal summary) (v3)",
             "excerpt": "Overtime on weekdays and Saturdays is paid at 125% of the hourly rate."},
            {"id": "INF-TEAMS-001", "title": "Teams chat export - #payroll-be-questions", "doc_type": "chat",
             "layer": "informal", "country": "BE", "owner": "", "owner_status": "none", "updated": "2025-11-20",
             "excerpt": "Lucas: Saturday overtime for BE clients is still 125%, I checked last year."},
            {"id": "CL-10045-NL-001", "title": "Company Agreement Overtime - Brouwerij Delta B.V. (NL) (v1)",
             "doc_type": "agreement", "layer": "client", "country": "NL", "entity": "Brouwerij Delta B.V.",
             "owner": "Sanne Bakker (HR NL)", "owner_status": "active", "updated": "2026-02-15",
             "excerpt": "Saturday overtime is paid at 150% of the hourly rate."},
        ],
        "conflicts": [
            {"a": "CL-10045-BE-001", "b": "INF-TEAMS-001", "topic": "Saturday overtime rate",
             "a_says": "Weekend overtime ... is paid at 175%", "b_says": "Saturday overtime for BE clients is still 125%"},
        ],
        "context": {"consultant": consultant.get("name"), "client": "Brouwerij Delta", "client_id": "CL-10045",
                    "entity": "Brouwerij Delta NV", "country": "BE", "sector": "PC 118"},
        "expert": "Sarah Janssens (Overtime and working time)",
        "model": "mock",
    }
