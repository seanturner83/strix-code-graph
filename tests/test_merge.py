"""Tests for build-time multi-target index merge (merge_sqlite_indexes)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from strix_code_graph.indexer import merge_sqlite_indexes
from strix_code_graph.query import CodeGraphIndex

# Minimal subset of the expt-convert schema the query layer reads.
_SCHEMA = """
CREATE TABLE documents(id INTEGER PRIMARY KEY, language TEXT, relative_path TEXT,
                       position_encoding INTEGER, text TEXT);
CREATE TABLE global_symbols(id INTEGER PRIMARY KEY, symbol TEXT, display_name TEXT,
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
    """Build a one-symbol, one-document SCIP-shaped sqlite index."""
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
    conn.execute(
        "INSERT INTO defn_enclosing_ranges VALUES (1, 1, 10, 0, 12, 0)",
    )
    conn.commit()
    conn.close()


def test_single_source_is_passthrough(tmp_path: Path) -> None:
    src = tmp_path / "a" / "code_graph.sqlite"
    src.parent.mkdir()
    _make_index(src, symbol="pkg/foo.", rel_path="src/x.py")
    dest = tmp_path / "out" / "code_graph.sqlite"

    merge_sqlite_indexes([("", src)], dest)

    idx = CodeGraphIndex(dest)
    try:
        defs = idx.find_definition("foo")
        assert defs, "single-source definition should resolve"
        # bare relative path (no target prefix) for a single target
        assert defs[0][1].file == "src/x.py"
    finally:
        idx.close()


def test_merge_two_targets_prefixes_and_preserves_fks(tmp_path: Path) -> None:
    a = tmp_path / "a" / "code_graph.sqlite"
    b = tmp_path / "b" / "code_graph.sqlite"
    a.parent.mkdir()
    b.parent.mkdir()
    # Same relative path in both repos — the collision the prefix must resolve.
    _make_index(a, symbol="repo-a/auth.", rel_path="src/x.py")
    _make_index(b, symbol="repo-b/auth.", rel_path="src/x.py")
    dest = tmp_path / "out" / "code_graph.sqlite"

    merge_sqlite_indexes([("repo-a", a), ("repo-b", b)], dest)

    idx = CodeGraphIndex(dest)
    try:
        # Both documents survive, each prefixed by its target label.
        conn = sqlite3.connect(dest)
        paths = {r[0] for r in conn.execute("SELECT relative_path FROM documents")}
        conn.close()
        assert paths == {"repo-a/src/x.py", "repo-b/src/x.py"}, paths

        # FK integrity: each symbol's definition still joins to the right doc,
        # so a query resolves to the correctly-prefixed path (not a scrambled id).
        a_defs = idx.find_definition("repo-a/auth")
        b_defs = idx.find_definition("repo-b/auth")
        assert a_defs and a_defs[0][1].file == "repo-a/src/x.py"
        assert b_defs and b_defs[0][1].file == "repo-b/src/x.py"
    finally:
        idx.close()


def test_merge_skips_missing_sources(tmp_path: Path) -> None:
    a = tmp_path / "a" / "code_graph.sqlite"
    a.parent.mkdir()
    _make_index(a, symbol="pkg/foo.", rel_path="src/x.py")
    missing = tmp_path / "gone" / "code_graph.sqlite"
    dest = tmp_path / "out" / "code_graph.sqlite"

    # A target whose index never built (missing file) must not break the merge.
    merge_sqlite_indexes([("repo-a", a), ("repo-b", missing)], dest)
    assert dest.exists()


def test_merge_across_languages(tmp_path: Path) -> None:
    """A Go target and a Python target merge into one graph — the merge is
    language-agnostic (language is just a documents column)."""
    go = tmp_path / "svc-go" / "code_graph.sqlite"
    py = tmp_path / "svc-py" / "code_graph.sqlite"
    go.parent.mkdir()
    py.parent.mkdir()
    _make_index(go, symbol="svc-go/handler.", rel_path="main.go")
    _make_index(py, symbol="svc-py/handler.", rel_path="app.py")
    # mark languages distinctly
    for p, lang in ((go, "go"), (py, "python")):
        c = sqlite3.connect(p)
        c.execute("UPDATE documents SET language=?", (lang,))
        c.commit()
        c.close()
    dest = tmp_path / "out" / "code_graph.sqlite"

    merge_sqlite_indexes([("svc-go", go), ("svc-py", py)], dest)

    conn = sqlite3.connect(dest)
    rows = dict(conn.execute("SELECT relative_path, language FROM documents").fetchall())
    conn.close()
    assert rows == {"svc-go/main.go": "go", "svc-py/app.py": "python"}, rows

    idx = CodeGraphIndex(dest)
    try:
        assert idx.find_definition("svc-go/handler")[0][1].file == "svc-go/main.go"
        assert idx.find_definition("svc-py/handler")[0][1].file == "svc-py/app.py"
    finally:
        idx.close()


def _make_reference_index(path: Path, *, symbol: str, rel_path: str, role: int) -> None:
    """A one-document index that MENTIONS ``symbol`` at ``role`` but does NOT
    define it — i.e. a consumer of a symbol whose definition lives elsewhere.
    ``role`` should be a non-definition SCIP role (e.g. 8 = Read) so
    find_references picks it up."""
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO documents(id, language, relative_path, position_encoding, text) "
        "VALUES (1, 'go', ?, 0, '')",
        (rel_path,),
    )
    # Same moniker as the definition-side index — this is what makes it a
    # cross-repo reference to the SAME symbol.
    conn.execute(
        "INSERT INTO global_symbols(id, symbol, display_name, kind, documentation, relationships) "
        "VALUES (1, ?, ?, 0, '', NULL)",
        (symbol, symbol.rsplit("/", 1)[-1]),
    )
    conn.execute(
        "INSERT INTO chunks(id, document_id, chunk_index, start_line, end_line, occurrences) "
        "VALUES (1, 1, 0, 5, 5, NULL)",
    )
    conn.execute("INSERT INTO mentions(chunk_id, symbol_id, role) VALUES (1, 1, ?)", (role,))
    conn.commit()
    conn.close()


def test_merge_dedupes_shared_symbol_and_resolves_cross_repo(tmp_path: Path) -> None:
    """The core cross-repo case: a symbol DEFINED in one target and REFERENCED
    in another shares one SCIP moniker, so it appears in BOTH source indexes.

    The merge must (1) NOT crash on the UNIQUE(symbol) constraint, and (2) dedupe
    to a single canonical symbol row so find_references spans repos — a reference
    in the consumer resolves against the definition in the shared lib. This is
    the mechanism that unifies the graph; a naive id-offset append breaks both.
    """
    # The SAME symbol, but keyed with DIFFERENT version tokens — exactly the
    # real fleet case: the library indexed as a working tree gets a commit-hash
    # pseudo-version; the consumer pins a released version. Normalization must
    # unify them.
    # The DESCRIPTOR (backtick path + symbol) is identical; the leading
    # "gomod <module> <version>" differs on BOTH axes — the two real fractures
    # seen on a 57-repo scan:
    #   - version: a working-tree checkout gets a commit-hash pseudo-version, a
    #     consumer pins a release.
    #   - module: scip-go mis-attributes a cross-module ref to the REFERENCING
    #     repo's module path, not the dep's.
    desc = "`github.com/org/shared/pkg`/CreatePartialEntity()."
    def_sym = f"scip-go gomod github.com/org/shared 4c2fdaa43a2e {desc}"
    ref_sym = f"scip-go gomod github.com/org/consumer v1.2002.0 {desc}"  # wrong module + diff version
    lib = tmp_path / "shared" / "code_graph.sqlite"
    consumer = tmp_path / "consumer" / "code_graph.sqlite"
    lib.parent.mkdir()
    consumer.parent.mkdir()
    # Definition lives in the shared lib (role 1 = Definition, set by _make_index).
    _make_index(lib, symbol=def_sym, rel_path="pkg/entity.go")
    # The consumer only READs it (role 8), same descriptor, mis-attributed module.
    _make_reference_index(consumer, symbol=ref_sym, rel_path="internal/use.go", role=8)
    dest = tmp_path / "out" / "code_graph.sqlite"

    # Must not raise (previously: UNIQUE constraint failed: global_symbols.symbol).
    merge_sqlite_indexes([("shared", lib), ("consumer", consumer)], dest)

    conn = sqlite3.connect(dest)
    # Descriptor-keyed dedup collapses both (version + module) variants to ONE row.
    n_sym = conn.execute("SELECT count(*) FROM global_symbols").fetchone()[0]
    assert n_sym == 1, f"same descriptor must dedupe to one row, got {n_sym}"
    # Both targets' documents survive, prefixed.
    paths = {r[0] for r in conn.execute("SELECT relative_path FROM documents")}
    assert paths == {"shared/pkg/entity.go", "consumer/internal/use.go"}, paths
    conn.close()

    idx = CodeGraphIndex(dest)
    try:
        # find_references resolves the consumer's use of a symbol DEFINED in the
        # shared lib — the cross-repo edge. Without dedup+remap the consumer's
        # mention would point at a split symbol id and never surface here.
        refs = idx.find_references("CreatePartialEntity")
        files = {loc.file for _, loc in refs}
        assert "consumer/internal/use.go" in files, f"cross-repo reference missing: {files}"
        # And the definition still resolves to the shared lib.
        defs = idx.find_definition("CreatePartialEntity")
        assert defs and defs[0][1].file == "shared/pkg/entity.go", defs
    finally:
        idx.close()


def test_merge_keeps_distinct_descriptors_apart(tmp_path: Path) -> None:
    """Two DIFFERENT types that merely share a display name (e.g. a shared proto
    `.../v1`/BalanceEvent and a service's own `.../models`/BalanceEvent) must NOT
    collapse — descriptor-keyed dedup normalizes module+version but keys on the
    package PATH, so different paths stay separate. Guards the module-blanking
    from over-merging."""
    proto = tmp_path / "shared" / "code_graph.sqlite"
    local = tmp_path / "svc" / "code_graph.sqlite"
    proto.parent.mkdir()
    local.parent.mkdir()
    _make_index(
        proto,
        symbol="scip-go gomod github.com/org/msgs v1.5.0 `github.com/org/msgs/pkg/v1`/BalanceEvent#",
        rel_path="pkg/v1/balances.pb.go",
    )
    _make_index(
        local,
        symbol="scip-go gomod github.com/org/svc abcdef `github.com/org/svc/internal/models`/BalanceEvent#",
        rel_path="internal/models/balance.go",
    )
    dest = tmp_path / "out" / "code_graph.sqlite"
    merge_sqlite_indexes([("shared", proto), ("svc", local)], dest)

    conn = sqlite3.connect(dest)
    n = conn.execute("SELECT count(*) FROM global_symbols").fetchone()[0]
    conn.close()
    assert n == 2, f"distinct-descriptor same-name types must stay separate, got {n}"


def test_normalize_moniker_qualifies_local_monikers_by_source_label() -> None:
    """SCIP's own "local N" numbering restarts per document/index -- it is
    unique only WITHIN its own originating index, never globally. Two
    entirely unrelated locally-scoped symbols in different targets routinely
    share the identical literal moniker (both emit ``local 0``). The dedup
    key must NOT treat these as the same symbol, unlike a real package
    moniker (still label-agnostic -- that's the cross-repo edge this
    function exists to create in the first place)."""
    from strix_code_graph.indexer import _normalize_moniker_version

    assert _normalize_moniker_version("local 0", "repo-a") != _normalize_moniker_version(
        "local 0", "repo-b",
    )
    assert _normalize_moniker_version("local 0", "repo-a") == _normalize_moniker_version(
        "local 0", "repo-a",
    )
    pkg = "scip-go gomod github.com/org/x v1.0.0 `github.com/org/x/pkg`/Foo()."
    assert _normalize_moniker_version(pkg, "repo-a") == _normalize_moniker_version(pkg, "repo-b")


def test_merge_keeps_unrelated_local_symbols_from_different_repos_apart(
    tmp_path: Path,
) -> None:
    """End-to-end: two UNRELATED repos each define their own locally-scoped
    symbol using SCIP's generic ``local 0`` moniker. Before qualifying local
    monikers by source label, the merge's cross-repo dedup wrongly treated
    the second repo's local 0 as "the same symbol" as the first's and
    collapsed them -- silently wrong graph edges (a reference in repo B
    would incorrectly resolve to repo A's unrelated local), not just a
    missed merge."""
    a = tmp_path / "a" / "code_graph.sqlite"
    b = tmp_path / "b" / "code_graph.sqlite"
    a.parent.mkdir()
    b.parent.mkdir()
    # Same literal (unqualified) moniker AND same display name in both --
    # exactly the case that used to collapse.
    _make_index(a, symbol="local 0", rel_path="internal/a.go")
    _make_index(b, symbol="local 0", rel_path="internal/b.py")
    dest = tmp_path / "out" / "code_graph.sqlite"

    merge_sqlite_indexes([("repo-a", a), ("repo-b", b)], dest)

    conn = sqlite3.connect(dest)
    n_gs = conn.execute("SELECT count(*) FROM global_symbols").fetchone()[0]
    conn.close()
    assert n_gs == 2, f"unrelated locals sharing a generic moniker must stay apart, got {n_gs}"

    idx = CodeGraphIndex(dest)
    try:
        # Each local's own definition resolves to ITS OWN file only.
        defs = idx.find_definition("local")
        files = {loc.file for _, loc in defs}
        assert files == {"repo-a/internal/a.go", "repo-b/internal/b.py"}, files
    finally:
        idx.close()


def test_merge_does_not_crash_when_a_source_has_two_symbols_sharing_a_dedup_key(
    tmp_path: Path,
) -> None:
    """Live-observed merging 5 real repos: a single source can itself
    contain two DISTINCT symbol_ids whose monikers normalize to the same
    dedup key (e.g. two version variants of the same descriptor -- neither
    is filtered against the OTHER within one source's own insert, so both
    land in dest and the subsequent nkey-join fans a single mention out to
    every matching dest row). If those two source symbols are each
    mentioned with the same role in the same chunk, the fan-out can
    reproduce the identical (chunk_id, symbol_id, role) triple twice, which
    the real scip-CLI-generated schema enforces as UNIQUE. Must not raise.
    """
    a = tmp_path / "a" / "code_graph.sqlite"
    a.parent.mkdir()
    conn = sqlite3.connect(a)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO documents(id, language, relative_path, position_encoding, text) "
        "VALUES (1, 'go', 'main.go', 0, '')",
    )
    # Same descriptor, different version tokens -- normalizes to one nkey.
    sym_1 = "scip-go gomod github.com/org/x 4c2fdaa43a2e `github.com/org/x/pkg`/Foo()."
    sym_2 = "scip-go gomod github.com/org/x v1.0.0 `github.com/org/x/pkg`/Foo()."
    conn.execute(
        "INSERT INTO global_symbols(id, symbol, display_name, kind, documentation, relationships) "
        "VALUES (1, ?, 'Foo', 0, '', NULL)",
        (sym_1,),
    )
    conn.execute(
        "INSERT INTO global_symbols(id, symbol, display_name, kind, documentation, relationships) "
        "VALUES (2, ?, 'Foo', 0, '', NULL)",
        (sym_2,),
    )
    conn.execute(
        "INSERT INTO chunks(id, document_id, chunk_index, start_line, end_line, occurrences) "
        "VALUES (1, 1, 0, 10, 12, NULL)",
    )
    conn.execute("INSERT INTO mentions(chunk_id, symbol_id, role) VALUES (1, 1, 8)")
    conn.execute("INSERT INTO mentions(chunk_id, symbol_id, role) VALUES (1, 2, 8)")
    conn.commit()
    conn.close()

    # A second, unrelated source -- multi-source is what exercises the
    # dedup/remap path (a single source is a plain passthrough copy).
    b = tmp_path / "b" / "code_graph.sqlite"
    b.parent.mkdir()
    _make_index(b, symbol="repo-b/bar.", rel_path="bar.py")

    dest = tmp_path / "out" / "code_graph.sqlite"
    merge_sqlite_indexes([("a", a), ("b", b)], dest)  # previously: sqlite3.IntegrityError

    conn = sqlite3.connect(dest)
    # No duplicate (chunk_id, symbol_id, role) triple survived -- if there
    # were one, the table's own UNIQUE constraint would already have raised
    # above; this just double-checks the row count is sane.
    n_mentions = conn.execute("SELECT count(*) FROM mentions").fetchone()[0]
    conn.close()
    assert n_mentions > 0


def test_merge_cli_argv(tmp_path: Path) -> None:
    """The merge CLI takes <dest> then (label, src) pairs as argv — the shape
    bootstrap invokes via session.exec(shell=False)."""
    from strix_code_graph.merge import main

    a = tmp_path / "a" / "code_graph.sqlite"
    b = tmp_path / "b" / "code_graph.sqlite"
    a.parent.mkdir()
    b.parent.mkdir()
    _make_index(a, symbol="repo-a/x.", rel_path="a.py")
    _make_index(b, symbol="repo-b/y.", rel_path="b.py")
    dest = tmp_path / "out.sqlite"

    rc = main([str(dest), "repo-a", str(a), "repo-b", str(b)])
    assert rc == 0
    assert dest.exists()

    conn = sqlite3.connect(dest)
    paths = {r[0] for r in conn.execute("SELECT relative_path FROM documents")}
    conn.close()
    assert paths == {"repo-a/a.py", "repo-b/b.py"}


def test_merge_cli_all_sources_missing(tmp_path: Path) -> None:
    from strix_code_graph.merge import main
    rc = main([str(tmp_path / "out.sqlite"), "x", str(tmp_path / "nope.sqlite")])
    assert rc == 1  # nothing to merge → non-zero, no dest
