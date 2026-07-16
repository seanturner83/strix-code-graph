"""Unit tests for the code_graph SQLite query layer (SEC-6848 W2.1).

Most tests run against a small in-memory schema replica so they're
hermetic and fast. One integration test runs against the real portal-api
SCIP→SQLite index at /tmp/portal-api.sqlite when present (W1 smoke
artefact) — that's the "this actually works on a real ZH repo" guard.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from strix_code_graph.query import (
    ROLE_DEFINITION,
    CodeGraphIndex,
    Location,
)

# Real SCIP SymbolRole values (scip.proto): bit 0 = Definition, bit 3 =
# ReadAccess. Tests must populate mentions with the bit-flag values
# scip-go actually emits, otherwise the find_references bitfield filter
# is never exercised against realistic data.
ROLE_READ_ACCESS = 8
ROLE_WRITE_ACCESS = 4


# ---------------------------------------------------------------------------
# Fixture: tiny synthetic SCIP-shaped SQLite
# ---------------------------------------------------------------------------


SCHEMA_SQL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY,
    language TEXT,
    relative_path TEXT NOT NULL UNIQUE,
    position_encoding TEXT,
    text TEXT
);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    occurrences BLOB NOT NULL
);
CREATE TABLE global_symbols (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL UNIQUE,
    display_name TEXT,
    kind INTEGER,
    documentation TEXT,
    signature BLOB,
    enclosing_symbol TEXT,
    relationships BLOB
);
CREATE TABLE mentions (
    chunk_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    role INTEGER NOT NULL,
    PRIMARY KEY (chunk_id, symbol_id, role)
);
CREATE TABLE defn_enclosing_ranges (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    start_char INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    end_char INTEGER NOT NULL
);
"""


@pytest.fixture
def synthetic_index(tmp_path: Path) -> Path:
    """Hand-build a tiny portal-api-shaped index covering:
      - src/auth/index.ts defines  `authorizeToParticipantAndAdminRole`
      - src/transfers/router.ts and src/api-keys/apiKeysRouter.ts
        reference it
      - src/auth/index.ts also has an exact-name-but-different-symbol
        match  `softAuthorizeToParticipantAndAdminRole`
        (LIKE pattern collision case)
    """
    db_path = tmp_path / "index.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)

    # Documents
    conn.executemany(
        "INSERT INTO documents (id, language, relative_path, position_encoding) VALUES (?,?,?,?)",
        [
            (1, "typescript", "src/auth/index.ts", "utf8"),
            (2, "typescript", "src/transfers/router.ts", "utf8"),
            (3, "typescript", "src/api-keys/apiKeysRouter.ts", "utf8"),
            (4, "typescript", "src/withdrawal-requests/withdrawal-request-router.ts", "utf8"),
        ],
    )

    # Global symbols (use real SCIP moniker shape).
    auth_index = "scip-typescript npm portal-api HEAD src/auth/`index.ts`/"
    conn.executemany(
        "INSERT INTO global_symbols (id, symbol) VALUES (?,?)",
        [
            (10, auth_index + "authorizeToParticipantAndAdminRole."),
            (11, auth_index + "softAuthorizeToParticipantAndAdminRole."),
            (12, auth_index + "userExists."),  # unrelated bystander
        ],
    )

    # Chunks — one per file.
    conn.executemany(
        "INSERT INTO chunks (id, document_id, chunk_index, start_line, end_line, occurrences) VALUES (?,?,?,?,?,?)",
        [
            (100, 1, 0, 0, 50, b""),     # auth/index.ts:0-50
            (101, 2, 0, 0, 100, b""),    # transfers/router.ts:0-100
            (102, 2, 1, 200, 250, b""),  # transfers/router.ts:200-250
            (103, 3, 0, 0, 80, b""),     # api-keys/apiKeysRouter.ts:0-80
            (104, 4, 0, 50, 120, b""),   # withdrawal-request-router.ts:50-120
        ],
    )

    # Mentions: symbol 10 def in chunk 100, refs in chunks 101/102/103/104.
    # Symbol 11 def + 1 ref to differentiate from the LIKE-collision.
    conn.executemany(
        "INSERT INTO mentions (chunk_id, symbol_id, role) VALUES (?,?,?)",
        [
            (100, 10, ROLE_DEFINITION),
            (101, 10, ROLE_READ_ACCESS),
            (102, 10, ROLE_READ_ACCESS),
            (103, 10, ROLE_READ_ACCESS),
            (104, 10, ROLE_READ_ACCESS),
            (100, 11, ROLE_DEFINITION),
            (101, 11, ROLE_READ_ACCESS),
            (103, 12, ROLE_READ_ACCESS),  # unrelated userExists also in apiKeysRouter
        ],
    )

    # Precise def range for symbol 10 only (re-exports often lack one).
    conn.execute(
        "INSERT INTO defn_enclosing_ranges "
        "(id, document_id, symbol_id, start_line, start_char, end_line, end_char) "
        "VALUES (1, 1, 10, 12, 0, 18, 1)"
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _names_of(matches) -> list[str]:
    return [m.display_name for m in matches]


# ---------------------------------------------------------------------------
# find_definition
# ---------------------------------------------------------------------------


def test_find_definition_uses_precise_range_when_available(synthetic_index: Path) -> None:
    idx = CodeGraphIndex(synthetic_index)
    try:
        results = idx.find_definition("authorizeToParticipantAndAdminRole")
        # Exact match first (over the LIKE-collision softAuthorize...).
        assert len(results) >= 1
        match, loc = results[0]
        assert match.display_name == "authorizeToParticipantAndAdminRole"
        # defn_enclosing_ranges row says lines 12-18, char 0-1.
        assert loc.file == "src/auth/index.ts"
        assert loc.start_line == 12
        assert loc.start_char == 0
        assert loc.end_line == 18
    finally:
        idx.close()


def test_find_definition_falls_back_to_chunk_range(synthetic_index: Path) -> None:
    # softAuthorize symbol has no defn_enclosing_ranges row, so the
    # fallback path should produce chunk-level location.
    idx = CodeGraphIndex(synthetic_index)
    try:
        results = idx.find_definition("softAuthorizeToParticipantAndAdminRole")
        assert len(results) == 1
        match, loc = results[0]
        assert match.display_name == "softAuthorizeToParticipantAndAdminRole"
        assert loc.file == "src/auth/index.ts"
        assert loc.start_char is None  # no precise range
        # chunk 100 ranged 0-50
        assert (loc.start_line, loc.end_line) == (0, 50)
    finally:
        idx.close()


def test_find_definition_unknown_symbol_returns_empty(synthetic_index: Path) -> None:
    idx = CodeGraphIndex(synthetic_index)
    try:
        assert idx.find_definition("nonexistentThing") == []
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# list_symbols
# ---------------------------------------------------------------------------


def test_list_symbols_returns_definitions_under_scope(synthetic_index: Path) -> None:
    # Regression: list_symbols selected der.start_character/end_character (the
    # columns are start_char/end_char) and built Location(relative_path=,
    # start_character=) (the fields are file=, start_char=). Both are only hit
    # by list_symbols / find_implementations, so a real scan crashed with
    # "no such column: der.start_character" then TypeError on Location. This
    # exercises the whole path against a real defn_enclosing_ranges row.
    idx = CodeGraphIndex(synthetic_index)
    try:
        syms = idx.list_symbols("src/auth")
        names = {m.display_name for m, _ in syms}
        assert "authorizeToParticipantAndAdminRole" in names
        # Location is well-formed: right file + precise range from the fixture.
        loc = next(loc for m, loc in syms if m.display_name == "authorizeToParticipantAndAdminRole")
        assert loc.file == "src/auth/index.ts"
        assert loc.start_line == 12
        assert loc.start_char == 0
    finally:
        idx.close()


def test_list_symbols_empty_scope_returns_empty(synthetic_index: Path) -> None:
    idx = CodeGraphIndex(synthetic_index)
    try:
        assert idx.list_symbols("") == []
        assert idx.list_symbols("no/such/dir") == []
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# find_references
# ---------------------------------------------------------------------------


def test_find_references_excludes_definition_by_default(synthetic_index: Path) -> None:
    idx = CodeGraphIndex(synthetic_index)
    try:
        results = idx.find_references("authorizeToParticipantAndAdminRole")
        # 4 references across 4 files; def site (auth/index.ts) excluded.
        files = [loc.file for _match, loc in results if _match.display_name == "authorizeToParticipantAndAdminRole"]
        assert "src/auth/index.ts" not in files
        assert "src/transfers/router.ts" in files
        assert "src/api-keys/apiKeysRouter.ts" in files
        assert "src/withdrawal-requests/withdrawal-request-router.ts" in files
    finally:
        idx.close()


def test_find_references_can_include_definition(synthetic_index: Path) -> None:
    idx = CodeGraphIndex(synthetic_index)
    try:
        results = idx.find_references(
            "authorizeToParticipantAndAdminRole", include_definition=True
        )
        files = [loc.file for _m, loc in results if _m.display_name == "authorizeToParticipantAndAdminRole"]
        assert "src/auth/index.ts" in files
    finally:
        idx.close()


def test_find_references_partial_match_still_returned_after_exact(
    synthetic_index: Path,
) -> None:
    # Querying for the short token "authorizeToParticipantAndAdminRole"
    # matches BOTH symbols via LIKE; exact-display-name match should win
    # and be enumerated first.
    idx = CodeGraphIndex(synthetic_index)
    try:
        results = idx.find_references("authorizeToParticipantAndAdminRole")
        assert len(results) > 0
        # First result must be the exact-name symbol.
        assert results[0][0].display_name == "authorizeToParticipantAndAdminRole"
    finally:
        idx.close()


def test_find_references_limit_enforced(synthetic_index: Path) -> None:
    idx = CodeGraphIndex(synthetic_index)
    try:
        results = idx.find_references("authorizeToParticipantAndAdminRole", limit=2)
        assert len(results) <= 2
    finally:
        idx.close()


def test_find_references_treats_all_non_definition_roles_as_refs(tmp_path: Path) -> None:
    # Regression: scip-go emits Read=8 / Write=4 / Import=2; an earlier
    # version of the query layer matched on `role = 0` and silently
    # returned zero refs for every Go bundle. Verify each non-zero
    # non-Definition role is returned as a reference.
    db = tmp_path / "bitfield.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO documents (id, relative_path) VALUES (1, 'a.go')")
    conn.execute("INSERT INTO global_symbols (id, symbol) VALUES (1, 'pkg/Foo#')")
    conn.executemany(
        "INSERT INTO chunks (id, document_id, chunk_index, start_line, end_line, occurrences) "
        "VALUES (?,?,?,?,?,?)",
        [(i, 1, i, i * 10, i * 10 + 5, b"") for i in range(1, 5)],
    )
    conn.executemany(
        "INSERT INTO mentions (chunk_id, symbol_id, role) VALUES (?,?,?)",
        [
            (1, 1, ROLE_DEFINITION),  # 1: should NOT be returned by default
            (2, 1, 2),                # Import: should be returned
            (3, 1, ROLE_WRITE_ACCESS),  # 4: should be returned
            (4, 1, ROLE_READ_ACCESS),   # 8: should be returned
        ],
    )
    conn.commit()
    conn.close()

    idx = CodeGraphIndex(db)
    try:
        refs = idx.find_references("Foo")
        assert {loc.start_line for _, loc in refs} == {20, 30, 40}
        with_def = idx.find_references("Foo", include_definition=True)
        assert {loc.start_line for _, loc in with_def} == {10, 20, 30, 40}
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# find_implementations (W2 stub)
# ---------------------------------------------------------------------------


def test_find_implementations_returns_empty_in_w2(synthetic_index: Path) -> None:
    idx = CodeGraphIndex(synthetic_index)
    try:
        assert idx.find_implementations("AnyInterface") == []
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# get_imports
# ---------------------------------------------------------------------------


def test_get_imports_lists_referenced_symbols_in_file(synthetic_index: Path) -> None:
    idx = CodeGraphIndex(synthetic_index)
    try:
        imports = idx.get_imports("src/api-keys/apiKeysRouter.ts")
        names = _names_of(imports)
        assert "authorizeToParticipantAndAdminRole" in names
        assert "userExists" in names
        # The def-only symbol shouldn't appear (no references in this file).
        assert "softAuthorizeToParticipantAndAdminRole" not in names
    finally:
        idx.close()


def test_get_imports_empty_for_unknown_file(synthetic_index: Path) -> None:
    idx = CodeGraphIndex(synthetic_index)
    try:
        assert idx.get_imports("nonexistent.ts") == []
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# get_symbol_at
# ---------------------------------------------------------------------------


def test_get_symbol_at_line_in_chunk_range(synthetic_index: Path) -> None:
    # transfers/router.ts has chunks 0-100 and 200-250. Line 50 → first
    # chunk → should see authorizeToParticipantAndAdminRole (ref) and
    # softAuthorize... (ref).
    idx = CodeGraphIndex(synthetic_index)
    try:
        symbols = idx.get_symbol_at("src/transfers/router.ts", 50)
        names = _names_of(symbols)
        assert "authorizeToParticipantAndAdminRole" in names
        assert "softAuthorizeToParticipantAndAdminRole" in names
    finally:
        idx.close()


def test_get_symbol_at_line_outside_chunks_returns_empty(synthetic_index: Path) -> None:
    idx = CodeGraphIndex(synthetic_index)
    try:
        assert idx.get_symbol_at("src/transfers/router.ts", 9999) == []
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# Discovery + missing-index handling
# ---------------------------------------------------------------------------


def test_discover_returns_none_when_no_indexes(tmp_path: Path) -> None:
    root = tmp_path / "code_graph"
    root.mkdir()
    assert CodeGraphIndex.discover(root) is None


def test_discover_returns_none_when_root_missing(tmp_path: Path) -> None:
    assert CodeGraphIndex.discover(tmp_path / "nope") is None


def test_discover_picks_most_recent_index(tmp_path: Path, synthetic_index: Path) -> None:
    root = tmp_path / "code_graph"
    (root / "older").mkdir(parents=True)
    (root / "newer").mkdir(parents=True)
    import shutil
    older = root / "older" / "code_graph.sqlite"
    newer = root / "newer" / "code_graph.sqlite"
    shutil.copy2(synthetic_index, older)
    shutil.copy2(synthetic_index, newer)
    # Force older to be older.
    import os
    import time
    os.utime(older, (time.time() - 60, time.time() - 60))

    idx = CodeGraphIndex.discover(root)
    try:
        assert idx is not None
        assert idx.sqlite_path == newer
    finally:
        if idx is not None:
            idx.close()


def test_open_missing_index_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        CodeGraphIndex(tmp_path / "does-not-exist.sqlite")


# ---------------------------------------------------------------------------
# Location rendering
# ---------------------------------------------------------------------------


def test_location_render_precise() -> None:
    loc = Location(file="src/foo.ts", start_line=12, end_line=18, start_char=0, end_char=1)
    assert loc.render() == "src/foo.ts:12:0"


def test_location_render_single_line() -> None:
    loc = Location(file="src/foo.ts", start_line=42, end_line=42)
    assert loc.render() == "src/foo.ts:42"


def test_location_render_range() -> None:
    loc = Location(file="src/foo.ts", start_line=10, end_line=50)
    assert loc.render() == "src/foo.ts:10-50"


# ---------------------------------------------------------------------------
# Integration: real portal-api index from W1 smoke (only if present)
# ---------------------------------------------------------------------------


REAL_PORTAL_API_INDEX = Path("/tmp/portal-api.sqlite")


@pytest.mark.skipif(
    not REAL_PORTAL_API_INDEX.exists(),
    reason="W1 smoke artefact /tmp/portal-api.sqlite not present",
)
def test_real_portal_api_find_references_for_appsec693_middleware() -> None:
    """End-to-end on real portal-api index: find_references for the
    middleware matt's APPSEC-693 fix uses. Same query the LLM would have
    needed to discover the structural pattern in the trade-api scan."""
    idx = CodeGraphIndex(REAL_PORTAL_API_INDEX)
    try:
        results = idx.find_references("authorizeToParticipantAndAdminRole")
        # 14 reference occurrences (chunked) across ~8 files in the W1
        # smoke; we want at least the participant-router.ts mount points
        # to come back.
        files = {loc.file for m, loc in results
                 if m.display_name == "authorizeToParticipantAndAdminRole"}
        assert "src/participants/participant-router.ts" in files
        assert "src/transfers/router.ts" in files
        assert "src/withdrawal-requests/withdrawal-request-router.ts" in files
        # Exclude def site by default.
        assert "src/auth/index.ts" not in files
    finally:
        idx.close()
