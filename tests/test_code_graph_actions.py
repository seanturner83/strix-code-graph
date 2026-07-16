"""Unit tests for the Strix tool wrappers around the code-graph query
layer (SEC-6848 W2.2). Verifies the LLM-facing output shape and the
graceful-degradation paths.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from strix_code_graph import tools as code_graph_actions
from strix_code_graph.tools import (
    _do_find_definition,
    _do_find_implementations,
    _do_find_references,
    _do_get_imports,
    _do_get_symbol_at,
    _do_grep,
    _do_list_symbols,
)

# Reuse the synthetic-index fixture shape from test_code_graph_query.py
# (kept independent to avoid cross-test-file fixture coupling).
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
def fake_index_root(tmp_path: Path) -> Path:
    root = tmp_path / "code_graph" / "portal-api"
    root.mkdir(parents=True)
    db_path = root / "code_graph.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    auth_prefix = "scip-typescript npm portal-api HEAD src/auth/`index.ts`/"
    conn.executemany(
        "INSERT INTO documents (id, language, relative_path, position_encoding) VALUES (?,?,?,?)",
        [
            (1, "typescript", "src/auth/index.ts", "utf8"),
            (2, "typescript", "src/transfers/router.ts", "utf8"),
        ],
    )
    conn.executemany(
        "INSERT INTO global_symbols (id, symbol) VALUES (?, ?)",
        [
            (10, auth_prefix + "authorizeToParticipantAndAdminRole."),
            (11, auth_prefix + "userExists."),
        ],
    )
    conn.executemany(
        "INSERT INTO chunks (id, document_id, chunk_index, start_line, end_line, occurrences) VALUES (?,?,?,?,?,?)",
        [
            (100, 1, 0, 0, 50, b""),
            (101, 2, 0, 0, 100, b""),
        ],
    )
    # Real SCIP SymbolRole values: Definition=1, ReadAccess=8. Earlier
    # fixture used role=0 for refs, which masked the
    # find_references-returns-empty bug because the fixture lied about
    # what scip-go actually emits.
    conn.executemany(
        "INSERT INTO mentions (chunk_id, symbol_id, role) VALUES (?, ?, ?)",
        [
            (100, 10, 1),  # def in auth/index.ts
            (101, 10, 8),  # ref in transfers/router.ts (ReadAccess)
            (101, 11, 8),  # userExists also referenced there
        ],
    )
    conn.execute(
        "INSERT INTO defn_enclosing_ranges (id, document_id, symbol_id, start_line, start_char, end_line, end_char) VALUES (1, 1, 10, 12, 0, 18, 1)"
    )
    conn.commit()
    conn.close()
    return tmp_path / "code_graph"


@pytest.fixture
def patched_discover(monkeypatch: pytest.MonkeyPatch, fake_index_root: Path):
    """Make CodeGraphIndex.discover() use the fixture root."""
    from strix_code_graph.query import CodeGraphIndex
    original = CodeGraphIndex.discover

    def _discover(root=None):
        return original(fake_index_root)

    monkeypatch.setattr(CodeGraphIndex, "discover", classmethod(lambda cls, root=None: original(fake_index_root)))
    return fake_index_root


# ---------------------------------------------------------------------------
# Unavailable path: no index found
# ---------------------------------------------------------------------------


def test_find_definition_unavailable_when_no_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_graph_actions, "_open_index", lambda: None)
    result = _do_find_definition("foo")
    assert "code graph not available" in result["output"]


def test_find_references_unavailable_when_no_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_graph_actions, "_open_index", lambda: None)
    result = _do_find_references("foo")
    assert "code graph not available" in result["output"]


def test_get_imports_unavailable_when_no_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_graph_actions, "_open_index", lambda: None)
    result = _do_get_imports("src/foo.ts")
    assert "code graph not available" in result["output"]


def test_get_symbol_at_unavailable_when_no_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_graph_actions, "_open_index", lambda: None)
    result = _do_get_symbol_at("src/foo.ts", 10)
    assert "code graph not available" in result["output"]


# ---------------------------------------------------------------------------
# find_implementations (unstubbed in e6f996a — reads SCIP relationship blobs)
# ---------------------------------------------------------------------------


def test_find_implementations_unavailable_when_no_index(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no index discovered the tool degrades to the standard
    # "code graph not available" output (it no longer returns the old W2
    # "not yet supported" stub — that path was removed when the
    # relationship-blob parser landed in e6f996a).
    monkeypatch.setattr(code_graph_actions, "_open_index", lambda: None)
    result = _do_find_implementations("AnyInterface")
    assert "code graph not available" in result["output"]


def test_find_implementations_no_matches_against_fixture(patched_discover) -> None:
    # The fixture index emits no is_implementation relationship records, so
    # a real query resolves and returns "no implementation matches" rather
    # than the removed stub string. Exercises the unstubbed query path.
    result = _do_find_implementations("authorizeToParticipantAndAdminRole")
    assert "no implementation matches" in result["output"]
    assert "not yet supported" not in result["output"]


# ---------------------------------------------------------------------------
# Real queries against the fixture index
# ---------------------------------------------------------------------------


def test_find_definition_returns_location(patched_discover) -> None:
    result = _do_find_definition("authorizeToParticipantAndAdminRole")
    assert "src/auth/index.ts:12:0" in result["output"]
    assert "authorizeToParticipantAndAdminRole defined" in result["output"]


def test_list_symbols_formats_output_by_file(patched_discover) -> None:
    # Regression: the _do_list_symbols output formatter read loc.relative_path,
    # but the Location field is .file — so the tool raised
    # "'Location' object has no attribute 'relative_path'" on first real use
    # (observed live on a multi-repo scan). The query layer had the twin bug
    # (der.start_character column + Location(relative_path=) kwargs). This
    # exercises the whole _do_list_symbols path end to end.
    result = _do_list_symbols("src/auth")
    assert "error" not in result, result
    out = result["output"]
    assert "src/auth/index.ts:" in out
    assert "authorizeToParticipantAndAdminRole (line 12)" in out


def test_find_references_excludes_def(patched_discover) -> None:
    result = _do_find_references("authorizeToParticipantAndAdminRole")
    assert "src/transfers/router.ts" in result["output"]
    assert "src/auth/index.ts" not in result["output"]
    assert "occurrence" in result["output"]


def test_find_references_can_include_def(patched_discover) -> None:
    result = _do_find_references(
        "authorizeToParticipantAndAdminRole", include_definition=True
    )
    assert "src/auth/index.ts" in result["output"]
    assert "src/transfers/router.ts" in result["output"]


def test_find_references_unknown_symbol(patched_discover) -> None:
    result = _do_find_references("doesNotExistAnywhere")
    assert "no reference matches" in result["output"]


def test_get_imports_lists_referenced_symbols(patched_discover) -> None:
    result = _do_get_imports("src/transfers/router.ts")
    assert "authorizeToParticipantAndAdminRole" in result["output"]
    assert "userExists" in result["output"]


def test_get_imports_unknown_file(patched_discover) -> None:
    result = _do_get_imports("src/missing.ts")
    assert "no imports/references" in result["output"]


def test_get_symbol_at_inside_chunk(patched_discover) -> None:
    result = _do_get_symbol_at("src/transfers/router.ts", 50)
    assert "authorizeToParticipantAndAdminRole" in result["output"]
    assert "userExists" in result["output"]


def test_get_symbol_at_outside_chunk(patched_discover) -> None:
    result = _do_get_symbol_at("src/transfers/router.ts", 9999)
    assert "no symbol matches" in result["output"]


# ---------------------------------------------------------------------------
# Tool registration smoke
# ---------------------------------------------------------------------------


def test_find_references_at_actions_layer_returns_refs_for_real_scip_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration-shaped regression: drive `_do_find_references`
    through the tool-action entry point against a SQLite that mirrors
    what scip-go actually emits (Definition=1, ReadAccess=8). Earlier
    versions silently returned the "no reference matches found" output
    on every Go bundle because the query layer filtered on role=0;
    this test catches that recurrence at the layer the agent invokes.
    """
    db_root = tmp_path / "code_graph" / "target"
    db_root.mkdir(parents=True)
    db = db_root / "code_graph.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO documents (id, relative_path) VALUES (1, 'a.go')")
    conn.execute(
        "INSERT INTO global_symbols (id, symbol) VALUES "
        "(1, 'scip-go gomod github.com/seedcx/x HEAD `pkg/api/v1`/Metadata.')"
    )
    conn.executemany(
        "INSERT INTO chunks (id, document_id, chunk_index, start_line, end_line, occurrences) "
        "VALUES (?,?,?,?,?,?)",
        [(i, 1, i, i * 10, i * 10 + 5, b"") for i in range(1, 4)],
    )
    conn.executemany(
        "INSERT INTO mentions (chunk_id, symbol_id, role) VALUES (?,?,?)",
        [
            (1, 1, 1),  # Definition — not in find_references output by default
            (2, 1, 8),  # ReadAccess — must be returned
            (3, 1, 4),  # WriteAccess — must be returned
        ],
    )
    conn.commit()
    conn.close()

    from strix_code_graph.query import CodeGraphIndex

    monkeypatch.setattr(
        CodeGraphIndex, "discover",
        classmethod(lambda cls, root=None: CodeGraphIndex(db)),
    )

    result = _do_find_references("Metadata")
    assert "no reference matches found" not in result.get("output", ""), result
    assert "code graph not available" not in result.get("output", ""), result
    # Both the Read and Write ref lines should be in the output (20-25
    # and 30-35 respectively), keyed on chunk start_line.
    assert "20" in result["output"], result["output"]
    assert "30" in result["output"], result["output"]
    # The definition (chunk start_line=10) should NOT appear by default.
    lines = [ln for ln in result["output"].split("\n") if ln.strip().startswith("a.go:")]
    line_numbers = {int(ln.split(":")[1].split("-")[0]) for ln in lines}
    assert 10 not in line_numbers, f"definition leaked into refs: {result['output']}"


def test_all_tools_exposed_with_expected_names() -> None:
    """ALL_TOOLS carries every code-graph tool under its SDK name — the set the
    addon hands to register_agent_tools."""
    from strix_code_graph.tools import ALL_TOOLS

    names = {t.name for t in ALL_TOOLS}
    expected = {
        "code_graph_find_definition",
        "code_graph_find_references",
        "code_graph_find_implementations",
        "code_graph_list_symbols",
        "code_graph_get_imports",
        "code_graph_get_symbol_at",
        "code_graph_grep",
        "code_graph_find_event_flow",
    }
    assert names == expected, f"tool set mismatch: {names ^ expected}"


# ---------------------------------------------------------------------------
# _do_grep — precise symbol resolution + source context
# ---------------------------------------------------------------------------


@pytest.fixture
def grep_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fixture index whose definition/reference ranges point at REAL
    source files on disk, so _do_grep can read windows from them.
    Sets STRIX_WORKSPACE_ROOT to the checkout root.

    Layout (1-based for humans; SCIP stores 0-based lines):
      pkg/auth/check.go   — defines validateWithdrawal (lines 5-9, 0-based 4-8)
      pkg/api/handler.go  — references it at line 11 (0-based 10)
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pkg" / "auth").mkdir(parents=True)
    (ws / "pkg" / "api").mkdir(parents=True)
    (ws / "pkg" / "auth" / "check.go").write_text(
        "package auth\n"            # line 1 (idx 0)
        "\n"                         # 2
        "import \"errors\"\n"        # 3
        "\n"                         # 4
        "func validateWithdrawal(r *Req) error {\n"   # 5 (idx 4) DEF start
        "\tif r.Amount <= 0 {\n"     # 6
        "\t\treturn errors.New(\"bad amount\")\n"      # 7
        "\t}\n"                      # 8
        "\treturn nil\n"             # 9 (idx 8) DEF end
        "}\n",                       # 10
        encoding="utf-8",
    )
    (ws / "pkg" / "api" / "handler.go").write_text(
        "package api\n"             # 1
        "\n"                         # 2
        "func Handle(req *Req) {\n"  # 3
        "\tamt := parse(req)\n"      # 4
        "\t_ = amt\n"                # 5
        "\t// gate\n"                # 6
        "\tif err := validateWithdrawal(req); err != nil {\n"  # 7 ... but ref at idx 10 below
        "\t\treturn\n"               # 8
        "\t}\n"                      # 9
        "\tprocess(req)\n"           # 10
        "\tvalidateWithdrawal(req)\n"  # 11 (idx 10) REF
        "}\n",                       # 12
        encoding="utf-8",
    )

    db_root = tmp_path / "code_graph" / "target"
    db_root.mkdir(parents=True)
    db = db_root / "code_graph.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA_SQL)
    conn.executemany(
        "INSERT INTO documents (id, relative_path) VALUES (?,?)",
        [(1, "pkg/auth/check.go"), (2, "pkg/api/handler.go")],
    )
    conn.execute(
        "INSERT INTO global_symbols (id, symbol) VALUES "
        "(1, 'scip-go gomod x HEAD `pkg/auth`/validateWithdrawal.')"
    )
    # chunk covering the ref line in handler.go (0-based line 10)
    conn.execute(
        "INSERT INTO chunks (id, document_id, chunk_index, start_line, end_line, occurrences) "
        "VALUES (200, 2, 0, 10, 10, x'')"
    )
    conn.execute(
        "INSERT INTO mentions (chunk_id, symbol_id, role) VALUES (200, 1, 8)"
    )
    # definition enclosing range: check.go lines 4-8 (0-based)
    conn.execute(
        "INSERT INTO defn_enclosing_ranges "
        "(id, document_id, symbol_id, start_line, start_char, end_line, end_char) "
        "VALUES (1, 1, 1, 4, 0, 8, 1)"
    )
    conn.commit()
    conn.close()

    from strix_code_graph.query import CodeGraphIndex

    monkeypatch.setattr(
        CodeGraphIndex, "discover",
        classmethod(lambda cls, root=None: CodeGraphIndex(db)),
    )
    monkeypatch.setenv("STRIX_WORKSPACE_ROOT", str(ws))
    return ws


def test_grep_unavailable_when_no_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_graph_actions, "_open_index", lambda: None)
    result = _do_grep("foo")
    assert "code graph not available" in result["output"]


def test_grep_shows_definition_body(grep_workspace) -> None:
    result = _do_grep("validateWithdrawal")
    out = result["output"]
    # The full def body (lines 5-9, displayed 1-based) must be inlined.
    assert "● DEFINITION" in out
    assert "func validateWithdrawal(r *Req) error {" in out
    assert "return errors.New(\"bad amount\")" in out
    assert "return nil" in out
    # 1-based line numbers shown.
    assert "5 func validateWithdrawal" in out


def test_grep_shows_reference_with_context(grep_workspace) -> None:
    result = _do_grep("validateWithdrawal", context=2)
    out = result["output"]
    assert "● REFERENCE" in out
    # ref is at 1-based line 11; with ±2 context we see 9-12.
    assert "validateWithdrawal(req)" in out
    assert "process(req)" in out      # line 10, within ±2
    assert "11 \tvalidateWithdrawal(req)" in out


def test_grep_context_is_clamped(grep_workspace) -> None:
    # Asking for a huge context must not blow past GREP_MAX_CONTEXT, and
    # must not error reading near file boundaries.
    result = _do_grep("validateWithdrawal", context=9999)
    assert "● REFERENCE" in result["output"]


def test_grep_unknown_symbol(grep_workspace) -> None:
    result = _do_grep("doesNotExistAnywhere")
    assert "no symbol matches" in result["output"]


def test_grep_degrades_when_source_missing(grep_workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point the workspace somewhere with no source files: the graph still
    # resolves the symbol, but every hit shows the bare location + a
    # "source unavailable" note rather than erroring.
    monkeypatch.setenv("STRIX_WORKSPACE_ROOT", "/nonexistent-workspace-xyz")
    result = _do_grep("validateWithdrawal")
    out = result["output"]
    assert "● DEFINITION" in out
    assert "source unavailable" in out


def test_grep_refuses_path_traversal(grep_workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    # A malformed SCIP relative_path that escapes the workspace must be
    # refused by _read_lines (returns None -> "source unavailable"),
    # never read from outside the checkout.
    from strix_code_graph import tools as cga

    assert cga._read_lines("../../../../etc/passwd") is None
