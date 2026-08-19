"""Tests for bootstrap._maybe_merge_corpus_graph -- unions a pre-fetched,
corpus-wide merged index (STRIX_CORPUS_GRAPH_PATH, a local path the wrapping
CI workflow downloads from S3 before invoking Strix) into a session's own
fresh graph, so cross-repo edges resolve against the whole org corpus, not
just this scan's own target(s). This addon stays AWS-free -- it only ever
reads a local path, never touches S3 itself.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from strix_code_graph import bootstrap

# Same minimal expt-convert schema subset as test_merge.py.
_SCHEMA = """
CREATE TABLE documents(id INTEGER PRIMARY KEY, language TEXT, relative_path TEXT,
                       position_encoding INTEGER, text TEXT);
CREATE TABLE global_symbols(id INTEGER PRIMARY KEY, symbol TEXT UNIQUE, display_name TEXT,
                            kind INTEGER, documentation TEXT, relationships BLOB);
CREATE TABLE chunks(id INTEGER PRIMARY KEY, document_id INTEGER, chunk_index INTEGER,
                    start_line INTEGER, end_line INTEGER, occurrences BLOB);
CREATE TABLE mentions(chunk_id INTEGER, symbol_id INTEGER, role INTEGER,
                      UNIQUE(chunk_id, symbol_id, role));
CREATE TABLE defn_enclosing_ranges(symbol_id INTEGER, document_id INTEGER,
                                   start_line INTEGER, start_char INTEGER,
                                   end_line INTEGER, end_char INTEGER);
"""


def _make_index(path: Path, *, symbol: str, rel_path: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO documents(id, language, relative_path, position_encoding, text) "
        "VALUES (1, 'python', ?, 0, '')",
        (rel_path,),
    )
    conn.execute(
        "INSERT INTO global_symbols(id, symbol, display_name, kind, documentation, relationships) "
        "VALUES (1, ?, ?, 0, '', NULL)",
        (symbol, symbol.rsplit("/", 1)[-1]),
    )
    conn.execute(
        "INSERT INTO chunks(id, document_id, chunk_index, start_line, end_line, occurrences) "
        "VALUES (1, 1, 0, 10, 12, NULL)",
    )
    conn.execute("INSERT INTO mentions(chunk_id, symbol_id, role) VALUES (1, 1, 1)")
    conn.execute("INSERT INTO defn_enclosing_ranges VALUES (1, 1, 10, 0, 12, 0)")
    conn.commit()
    conn.close()


def test_noop_when_env_var_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRIX_CORPUS_GRAPH_PATH", raising=False)
    session_sqlite = tmp_path / "code_graph.sqlite"
    _make_index(session_sqlite, symbol="scip-python python . . Own#", rel_path="own.py")
    before = session_sqlite.read_bytes()

    bootstrap._maybe_merge_corpus_graph(session_sqlite)
    assert session_sqlite.read_bytes() == before


def test_noop_when_path_set_but_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRIX_CORPUS_GRAPH_PATH", str(tmp_path / "does-not-exist.sqlite"))
    session_sqlite = tmp_path / "code_graph.sqlite"
    _make_index(session_sqlite, symbol="scip-python python . . Own#", rel_path="own.py")
    before = session_sqlite.read_bytes()

    bootstrap._maybe_merge_corpus_graph(session_sqlite)
    assert session_sqlite.read_bytes() == before


def test_merges_corpus_graph_in_place_keeping_session_paths_bare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_sqlite = tmp_path / "session" / "code_graph.sqlite"
    _make_index(session_sqlite, symbol="scip-python python . . Own#", rel_path="own.py")

    corpus_sqlite = tmp_path / "corpus.sqlite"
    _make_index(corpus_sqlite, symbol="scip-go gomod github.com/seedcx/other v1.0.0 `pkg`/Other#",
                rel_path="other.go")
    monkeypatch.setenv("STRIX_CORPUS_GRAPH_PATH", str(corpus_sqlite))

    bootstrap._maybe_merge_corpus_graph(session_sqlite)

    conn = sqlite3.connect(session_sqlite)
    paths = {row[0] for row in conn.execute("SELECT relative_path FROM documents")}
    conn.close()
    assert paths == {"own.py", "corpus/other.go"}


def test_never_raises_on_a_corrupt_corpus_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_sqlite = tmp_path / "code_graph.sqlite"
    _make_index(session_sqlite, symbol="scip-python python . . Own#", rel_path="own.py")
    before = session_sqlite.read_bytes()

    corpus_sqlite = tmp_path / "corpus.sqlite"
    corpus_sqlite.write_bytes(b"not a real sqlite file")
    monkeypatch.setenv("STRIX_CORPUS_GRAPH_PATH", str(corpus_sqlite))

    bootstrap._maybe_merge_corpus_graph(session_sqlite)  # must not raise
    assert session_sqlite.read_bytes() == before


# -- _adopt_corpus_graph_wholesale --------------------------------------------


def test_adopt_returns_false_when_no_corpus_path_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STRIX_CORPUS_GRAPH_PATH", raising=False)
    assert bootstrap._adopt_corpus_graph_wholesale() is False


def test_adopt_returns_false_when_corpus_path_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRIX_CORPUS_GRAPH_PATH", str(tmp_path / "does-not-exist.sqlite"))
    assert bootstrap._adopt_corpus_graph_wholesale() is False


def test_adopt_copies_corpus_graph_to_resolved_out_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus_sqlite = tmp_path / "corpus.sqlite"
    _make_index(corpus_sqlite, symbol="scip-go gomod github.com/seedcx/other v1.0.0 `pkg`/Other#",
                rel_path="other.go")
    monkeypatch.setenv("STRIX_CORPUS_GRAPH_PATH", str(corpus_sqlite))

    out_dir = tmp_path / "persist"
    monkeypatch.setenv("STRIX_CODE_GRAPH_PERSIST_DIR", str(out_dir))
    monkeypatch.delenv("STRIX_CODE_GRAPH_DIR", raising=False)

    assert bootstrap._adopt_corpus_graph_wholesale() is True
    final_sqlite = out_dir / "target" / "code_graph.sqlite"
    assert final_sqlite.read_bytes() == corpus_sqlite.read_bytes()


def test_adopt_falls_back_to_false_on_copy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus_sqlite = tmp_path / "corpus.sqlite"
    _make_index(corpus_sqlite, symbol="scip-go gomod github.com/seedcx/other v1.0.0 `pkg`/Other#",
                rel_path="other.go")
    monkeypatch.setenv("STRIX_CORPUS_GRAPH_PATH", str(corpus_sqlite))

    def _boom(*a: object, **kw: object) -> None:
        raise OSError("disk full")
    monkeypatch.setattr(bootstrap.shutil, "copyfile", _boom)

    assert bootstrap._adopt_corpus_graph_wholesale() is False  # must not raise


# -- _build_and_copy_out short-circuit -----------------------------------------


async def test_build_and_copy_out_skips_sandbox_entirely_when_corpus_graph_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus_sqlite = tmp_path / "corpus.sqlite"
    _make_index(corpus_sqlite, symbol="scip-go gomod github.com/seedcx/other v1.0.0 `pkg`/Other#",
                rel_path="other.go")
    monkeypatch.setenv("STRIX_CORPUS_GRAPH_PATH", str(corpus_sqlite))
    out_dir = tmp_path / "persist"
    monkeypatch.setenv("STRIX_CODE_GRAPH_PERSIST_DIR", str(out_dir))
    monkeypatch.delenv("STRIX_CODE_GRAPH_DIR", raising=False)

    class _BoomSession:
        async def exec(self, *a: object, **kw: object) -> None:
            raise AssertionError("must not touch the sandbox when adopting the corpus graph")

        async def read(self, *a: object, **kw: object) -> None:
            raise AssertionError("must not touch the sandbox when adopting the corpus graph")

    await bootstrap._build_and_copy_out(_BoomSession(), ["repo-a", "repo-b"])

    final_sqlite = out_dir / "target" / "code_graph.sqlite"
    assert final_sqlite.read_bytes() == corpus_sqlite.read_bytes()
