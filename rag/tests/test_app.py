from fastapi.testclient import TestClient

from rag_core.agent import ScriptedFakeProvider
from rag_core.app import create_app


def test_ingest_then_ask_with_ui_contract(settings):
    client = TestClient(create_app(settings, ScriptedFakeProvider()))
    assert client.get("/health").json() == {"status": "ok", "documents": 0, "chunks": 0}
    assert len(client.post("/ingest").json()["ingested"]) == 3
    assert client.get("/health").json()["documents"] == 3

    body = {"question": "overtime approval", "client": {"id": "janssens", "country": "BE"},
            "messages": [{"role": "user", "content": "overtime approval"}]}
    data = client.post("/ask", json=body).json()
    assert set(data) == {"answer", "sources", "conflicts"}
    assert "[1] timesheet_rules.pdf" in data["answer"] and "department=unknown (missing)" in data["answer"]
    source = data["sources"][0]
    assert source["id"] == "1" and source["title"] == "timesheet_rules.pdf"
    assert source["country"] is None and source["owner"] is None and source["updated"] is None
    assert "Overtime requires approval" in source["excerpt"]
    assert source["provenance"]["owner"] == "missing"
