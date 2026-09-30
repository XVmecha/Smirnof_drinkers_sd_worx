from rag_core.search import CHUNK_CHARS, chunk, tokenize


def test_tokenize_is_case_and_accent_insensitive():
    assert tokenize("Congé annuel, PRIME de fin d'année!") == ["conge", "annuel", "prime", "de", "fin", "d", "annee"]


def test_chunk_short_and_empty():
    assert chunk("hello world") == ["hello world"]
    assert chunk("  \n ") == []


def test_chunk_long_text_splits_at_whitespace_with_overlap():
    text = " ".join(f"word{i}" for i in range(2000))
    pieces = chunk(text)
    assert len(pieces) > 1
    assert all(len(p) <= CHUNK_CHARS for p in pieces)
    assert all(not p.startswith("ord") for p in pieces)  # no mid-word cuts
    assert all(p.split()[-1] in q for p, q in zip(pieces, pieces[1:]))  # overlap
    assert set(text.split()) <= set(" ".join(pieces).split())  # nothing lost


def test_search_returns_metadata_and_passages_without_scores(index):
    result = index.search("hoeveel vakantiedagen heb ik")
    top = result["documents"][0]
    assert top["filename"] == "vakantiebeleid.pdf"
    assert set(top) == {"doc_id", "filename", "metadata", "provenance", "passages"}
    assert top["metadata"]["country"] == "BE" and top["provenance"]["country"] == "sidecar"
    assert all("vakantiedagen" in p["text"] for p in top["passages"])
    assert {p["page"] for p in top["passages"]} == {1, 2}


def test_search_is_accent_insensitive(index):
    top = index.search("prime de fin d'année")["documents"][0]
    assert top["filename"] == "paie_primes.pdf" and top["metadata"]["department"] is None


def test_search_no_match(index):
    assert index.search("zzzz qqqq")["documents"] == []


def test_read_pages_caps_and_ignores_out_of_range(index):
    doc_id = index.search("vakantiedagen")["documents"][0]["doc_id"]
    result = index.read_pages(doc_id, [2, 1, 9, 3, 4, 5, 6, 7])
    assert [p["page"] for p in result["pages"]] == [1, 2] and result["page_count"] == 2
    assert "error" in index.read_pages("nope", [1])


def test_run_tool_handles_bad_input(index):
    assert "error" in index.run_tool("read_pages", {"doc_id": "x", "pages": 1})
    assert "error" in index.run_tool("nonexistent", {})
