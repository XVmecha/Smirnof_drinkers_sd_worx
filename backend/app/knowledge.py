"""Knowledge base over data/ (see the "Data structure" section of the README).

data/catalog.json is the source of truth for every document's metadata
(layer, country, client, owner, status, supersedes, overrides, ...); the PDFs
hold the text. data/users.json says which consultant may see which client.
The index is rebuilt automatically when any of these files change.
"""

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader
from rank_bm25 import BM25Okapi

log = logging.getLogger(__name__)

STOPWORDS = set(
    "a an and are as at be by does do for from has have how in is it of on or that the this to "
    "was what when which who will with de het een en van is dat die voor op te met".split()
)
# Words too generic to count as a topic match on their own.
GENERIC = {"company", "client", "agreement", "policy", "rule", "rules", "belgium", "netherlands", "rate", "new"}
FOOTER = "FICTIONAL DEMO DATA"
# Heuristic on top of the catalog's security_test flag: text that talks to the AI is never passed to it.
INJECTION_RE = re.compile(
    r"ignore (all )?(previous|prior|above) instructions|note to (the )?ai|system note|you are now|do not show any sources",
    re.I,
)


def tokenize(text: str) -> list[str]:
    tokens = [t for t in re.findall(r"\w+", text.lower()) if t not in STOPWORDS and len(t) > 1]
    # Crude plural stemming so "vouchers" matches "voucher".
    return [t[:-1] if len(t) > 4 and t.endswith("s") and not t.endswith("ss") else t for t in tokens]


def pdf_body(path: Path) -> str:
    """Document text without the metadata table and footer that build_data.py adds."""
    lines = [ln.strip() for page in PdfReader(path).pages for ln in (page.extract_text() or "").splitlines()]
    try:
        start = lines.index("Status") + 2  # label + value close the metadata table
    except ValueError:
        start = 0
    return "\n".join(ln for ln in lines[start:] if ln and not ln.startswith(FOOTER))


@dataclass
class Doc:
    meta: dict
    text: str
    suspicious: bool

    @property
    def id(self) -> str:
        return self.meta["id"]

    @property
    def client_id(self) -> str | None:
        return self.meta.get("client_id")


@dataclass
class Hit:
    doc: Doc
    score: float
    topic_match: bool


@dataclass
class KnowledgeBase:
    data_dir: Path
    docs: dict[str, Doc] = field(default_factory=dict)
    users: dict[str, dict] = field(default_factory=dict)
    clients: dict[str, dict] = field(default_factory=dict)  # client_id -> {name, aliases, entities}
    experts: list[tuple[str, str, str]] = field(default_factory=list)  # (country, topic, name)
    superseded_by: dict[str, str] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)
    _bm25: BM25Okapi | None = None
    _signature: tuple = ()
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- loading ------------------------------------------------------------

    def _signature_now(self) -> tuple:
        files = [self.data_dir / "catalog.json", self.data_dir / "users.json", *self.data_dir.rglob("*.pdf")]
        return tuple(sorted((str(p), p.stat().st_mtime_ns) for p in files if p.exists()))

    def refresh(self) -> None:
        signature = self._signature_now()
        with self._lock:
            if signature == self._signature:
                return
            self._load()
            self._signature = signature

    def _load(self) -> None:
        catalog = json.loads((self.data_dir / "catalog.json").read_text(encoding="utf-8"))
        users = json.loads((self.data_dir / "users.json").read_text(encoding="utf-8"))
        root = self.data_dir.resolve()

        docs = {}
        for meta in catalog.get("documents", []):
            path = (self.data_dir / meta["path"]).resolve()
            if not path.is_relative_to(root) or not path.exists():  # never read outside data/
                log.warning("skipping %s: missing or outside data/", meta.get("id"))
                continue
            try:
                text = pdf_body(path)
            except Exception as e:  # one broken PDF must not take the index down
                log.warning("skipping %s: %s", meta["id"], e)
                continue
            suspicious = bool(meta.get("security_test")) or bool(INJECTION_RE.search(text))
            docs[meta["id"]] = Doc(meta=meta, text=text, suspicious=suspicious)

        self.docs = docs
        self.users = {u["id"]: u for u in users.get("consultants", [])}
        self.superseded_by = {d.meta["supersedes"]: d.id for d in docs.values() if d.meta.get("supersedes")}
        self.clients = self._parse_clients(docs)
        self.experts = self._parse_experts(docs)

        self._order = [d.id for d in docs.values() if d.meta.get("layer") != "directory"]
        corpus = [
            tokenize(" ".join([docs[i].meta.get("title", ""), " ".join(docs[i].meta.get("topics", [])), docs[i].text]))
            for i in self._order
        ]
        self._bm25 = BM25Okapi(corpus) if corpus else None
        log.info("indexed %d documents, %d consultants, %d clients", len(docs), len(self.users), len(self.clients))

    @staticmethod
    def _parse_clients(docs: dict[str, Doc]) -> dict[str, dict]:
        """Client names, aliases and legal entities, from the client profile documents."""
        clients = {}
        for d in docs.values():
            cid = d.client_id
            if not cid:
                continue
            c = clients.setdefault(cid, {"name": cid, "aliases": {cid.lower()}, "entities": []})
            if d.meta.get("source_type") == "profile":
                c["name"] = d.meta["title"].split(" - ", 1)[-1]
                c["aliases"].add(c["name"].lower())
                for country, entity, sector in re.findall(
                    r"Entity (\w{2}): (.+?) \(.*?\), sector ([^,]+),", d.text
                ):
                    c["entities"].append({"country": country, "name": entity, "sector": sector.strip()})
                    c["aliases"].add(entity.lower())
            # Folder slug: clients/CL-10045_brouwerij-delta/... -> "brouwerij delta"
            slug = d.meta["path"].split("/")[1].split("_", 1)[-1]
            c["aliases"].add(slug.replace("-", " ").lower())
            c["aliases"].add(slug.split("-")[0].lower())
        return clients

    @staticmethod
    def _parse_experts(docs: dict[str, Doc]) -> list[tuple[str, str, str]]:
        experts = []
        for d in docs.values():
            if d.meta.get("layer") == "directory":
                experts += [
                    (c, topic.strip(), name.strip())
                    for c, topic, name in re.findall(r"^(\w{2}) - (.+?): (.+)$", d.text, re.M)
                ]
        return experts

    # ---- queries ------------------------------------------------------------

    def search(self, query: str, allowed: set[str], k: int) -> list[Hit]:
        self.refresh()
        tokens = tokenize(query)
        if not self._bm25 or not tokens:
            return []
        wanted = set(tokens) - GENERIC
        hits = []
        for doc_id, score in zip(self._order, self._bm25.get_scores(tokens)):
            if doc_id in allowed and score > 0:
                doc = self.docs[doc_id]
                topic_words = set(tokenize(" ".join(doc.meta.get("topics", []))))
                hits.append(Hit(doc=doc, score=float(score), topic_match=bool(wanted & topic_words)))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def expert_for(self, country: str | None, query: str) -> str | None:
        """Best matching expert from the directory, or None ("no named expert yet")."""
        words = set(tokenize(query))
        best, best_overlap = None, 0
        for c, topic, name in self.experts:
            if country and c != country:
                continue
            overlap = len(words & set(tokenize(topic)))
            if overlap > best_overlap:
                best, best_overlap = (topic, name), overlap
        if not best or best[1].lower().startswith("no named expert"):
            return None
        return f"{best[1]} ({best[0]})"
