import json

from rag_core.ingest import ingest, load_index


def test_ingest_all_then_skip_and_persist(settings):
    docs = {}
    first = ingest(settings, docs)
    assert sorted(first.ingested) == ["hr/vakantiebeleid.pdf", "paie_primes.pdf", "timesheet_rules.pdf"]
    assert first.errors == {}

    second = ingest(settings, docs)
    assert second.ingested == [] and sorted(second.skipped) == sorted(first.ingested)

    reloaded = load_index(settings.index_path)
    assert reloaded.keys() == docs.keys()
    assert ingest(settings, reloaded).skipped == sorted(first.ingested)  # hash survives a reload


def test_sidecar_change_reingests_only_that_document(settings, docs_dir):
    docs = {}
    ingest(settings, docs)
    sidecar = docs_dir / "hr" / "vakantiebeleid.pdf.json"
    sidecar.write_text(json.dumps({**json.loads(sidecar.read_text()), "owner": "HR Benelux"}))

    report = ingest(settings, docs)
    assert report.ingested == ["hr/vakantiebeleid.pdf"] and len(report.skipped) == 2
    assert next(d for d in docs.values() if d.filename == "vakantiebeleid.pdf").metadata.owner == "HR Benelux"


def test_deleted_file_is_removed(settings, docs_dir):
    docs = {}
    ingest(settings, docs)
    (docs_dir / "timesheet_rules.pdf").unlink()
    assert ingest(settings, docs).removed == ["timesheet_rules.pdf"]
    assert all(d.filename != "timesheet_rules.pdf" for d in docs.values())


def test_metadata_precedence_and_provenance(settings):
    docs = {}
    ingest(settings, docs)
    by_name = {d.filename: d for d in docs.values()}

    full = by_name["vakantiebeleid.pdf"]
    assert full.metadata.model_dump() == {"language": "nl", "country": "BE", "department": "HR", "owner": "HR Belgium",
                                          "created_at": "2023-01-10", "updated_at": "2025-06-01"}
    assert set(full.provenance.values()) == {"sidecar"}

    partial = by_name["paie_primes.pdf"]
    assert partial.metadata.country == "FR"
    assert partial.metadata.department is None  # "Finance" is not a valid department: dropped, never guessed
    assert partial.metadata.owner is None  # blank counts as missing
    assert partial.provenance == {"language": "missing", "country": "sidecar", "department": "missing",
                                  "owner": "missing", "created_at": "missing", "updated_at": "missing"}

    none = by_name["timesheet_rules.pdf"]
    assert all(v is None for v in none.metadata.model_dump().values())
    assert set(none.provenance.values()) == {"missing"}
    assert len(full.pages) == 2 and "20 wettelijke vakantiedagen" in full.pages[0]
