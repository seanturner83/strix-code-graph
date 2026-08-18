"""Tests for _patch_missing_symbol_information -- the workaround for a real,
documented scip-python bug (sourcegraph/scip-python#223, filed 2026-07-29,
open/unfixed as of 0.6.6): SymbolInformation is missing for @dataclass class
symbols and many stdlib symbols. scip expt-convert's own validator hard-errors
-- and aborts the ENTIRE conversion, not just that one symbol -- on any
definition-role occurrence whose symbol has no SymbolInformation entry, so
this patches minimal placeholders in before expt-convert ever runs.
"""
from __future__ import annotations

from pathlib import Path

from strix_code_graph import scip_pb2
from strix_code_graph.indexer import _ROLE_DEFINITION, _patch_missing_symbol_information


def _write_index(tmp_path: Path, *, documents: list) -> Path:
    idx = scip_pb2.Index()
    idx.documents.extend(documents)
    p = tmp_path / "py.scip"
    p.write_bytes(idx.SerializeToString())
    return p


def _doc_with_occurrence(relative_path: str, symbol: str, *, is_definition: bool) -> scip_pb2.Document:
    doc = scip_pb2.Document(relative_path=relative_path)
    occ = doc.occurrences.add()
    occ.symbol = symbol
    occ.symbol_roles = _ROLE_DEFINITION if is_definition else 0
    return doc


def test_patches_definition_occurrence_missing_symbol_information(tmp_path: Path) -> None:
    doc = _doc_with_occurrence("pkg/mod.py", "scip-python python . . BudgetExceeded#", is_definition=True)
    scip_path = _write_index(tmp_path, documents=[doc])

    patched = _patch_missing_symbol_information(scip_path)
    assert patched == 1

    idx = scip_pb2.Index()
    idx.ParseFromString(scip_path.read_bytes())
    assert {si.symbol for si in idx.documents[0].symbols} == {"scip-python python . . BudgetExceeded#"}


def test_leaves_already_known_symbol_alone(tmp_path: Path) -> None:
    doc = _doc_with_occurrence("pkg/mod.py", "scip-python python . . Known#", is_definition=True)
    si = doc.symbols.add()
    si.symbol = "scip-python python . . Known#"
    scip_path = _write_index(tmp_path, documents=[doc])

    patched = _patch_missing_symbol_information(scip_path)
    assert patched == 0

    idx = scip_pb2.Index()
    idx.ParseFromString(scip_path.read_bytes())
    assert len(idx.documents[0].symbols) == 1


def test_ignores_reference_only_occurrences(tmp_path: Path) -> None:
    # A reference occurrence (no ROLE_DEFINITION bit) pointing at a symbol
    # this repo doesn't define is normal and expected (e.g. a stdlib type)
    # -- must NOT be treated as needing a placeholder.
    doc = _doc_with_occurrence("pkg/mod.py", "scip-python python . . SomeStdlibThing#", is_definition=False)
    scip_path = _write_index(tmp_path, documents=[doc])

    patched = _patch_missing_symbol_information(scip_path)
    assert patched == 0


def test_does_not_rewrite_file_when_nothing_needed(tmp_path: Path) -> None:
    doc = _doc_with_occurrence("pkg/mod.py", "scip-python python . . Known#", is_definition=False)
    scip_path = _write_index(tmp_path, documents=[doc])
    before = scip_path.read_bytes()
    before_mtime = scip_path.stat().st_mtime_ns

    assert _patch_missing_symbol_information(scip_path) == 0
    assert scip_path.read_bytes() == before
    assert scip_path.stat().st_mtime_ns == before_mtime


def test_patches_across_multiple_documents_independently(tmp_path: Path) -> None:
    doc_a = _doc_with_occurrence("a.py", "scip-python python . . A#", is_definition=True)
    doc_b = _doc_with_occurrence("b.py", "scip-python python . . B#", is_definition=True)
    scip_path = _write_index(tmp_path, documents=[doc_a, doc_b])

    assert _patch_missing_symbol_information(scip_path) == 2

    idx = scip_pb2.Index()
    idx.ParseFromString(scip_path.read_bytes())
    assert {si.symbol for si in idx.documents[0].symbols} == {"scip-python python . . A#"}
    assert {si.symbol for si in idx.documents[1].symbols} == {"scip-python python . . B#"}


def test_dedupes_repeated_definition_occurrences_of_the_same_symbol(tmp_path: Path) -> None:
    # e.g. a symbol with multiple definition-role occurrences in one file
    # (forward decl + real def) must only get ONE placeholder, not one per
    # occurrence -- SymbolInformation.symbol has its own UNIQUE constraint
    # downstream in the real scip-CLI-generated schema.
    doc = scip_pb2.Document(relative_path="pkg/mod.py")
    for _ in range(3):
        occ = doc.occurrences.add()
        occ.symbol = "scip-python python . . Repeated#"
        occ.symbol_roles = _ROLE_DEFINITION
    scip_path = _write_index(tmp_path, documents=[doc])

    assert _patch_missing_symbol_information(scip_path) == 1
