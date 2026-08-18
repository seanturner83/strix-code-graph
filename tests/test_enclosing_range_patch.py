"""Tests for _patch_invalid_enclosing_ranges -- the workaround for a real
scip-go bug (scip-code/scip-go#172, merged 2025-11-24): its enclosing_range
support can compute a zero-valued end position for a function, producing
an enclosing_range where the end is before the start. scip expt-convert's
own validator hard-errors on this -- and aborts the ENTIRE conversion, not
just that one occurrence -- so this clears any such malformed range before
expt-convert ever runs.
"""
from __future__ import annotations

from pathlib import Path

from strix_code_graph import scip_pb2
from strix_code_graph.indexer import _patch_invalid_enclosing_ranges


def _write_index(tmp_path: Path, *, documents: list) -> Path:
    idx = scip_pb2.Index()
    idx.documents.extend(documents)
    p = tmp_path / "go.scip"
    p.write_bytes(idx.SerializeToString())
    return p


def test_clears_4elem_enclosing_range_where_end_line_before_start(tmp_path: Path) -> None:
    # Exact shape observed live on a real repo: [87, 0, 0, 0] -- end line
    # 0 is before start line 87.
    doc = scip_pb2.Document(relative_path="pkg/thing.go")
    occ = doc.occurrences.add()
    occ.symbol = "scip-go gomod example.com/thing v0.0.0 `pkg`/Thing()."
    occ.symbol_roles = 1
    occ.enclosing_range.extend([87, 0, 0, 0])
    scip_path = _write_index(tmp_path, documents=[doc])

    patched = _patch_invalid_enclosing_ranges(scip_path)
    assert patched == 1

    idx = scip_pb2.Index()
    idx.ParseFromString(scip_path.read_bytes())
    assert list(idx.documents[0].occurrences[0].enclosing_range) == []


def test_leaves_valid_4elem_enclosing_range_alone(tmp_path: Path) -> None:
    doc = scip_pb2.Document(relative_path="pkg/thing.go")
    occ = doc.occurrences.add()
    occ.symbol = "scip-go gomod example.com/thing v0.0.0 `pkg`/Thing()."
    occ.symbol_roles = 1
    occ.enclosing_range.extend([10, 0, 20, 1])
    scip_path = _write_index(tmp_path, documents=[doc])

    assert _patch_invalid_enclosing_ranges(scip_path) == 0
    idx = scip_pb2.Index()
    idx.ParseFromString(scip_path.read_bytes())
    assert list(idx.documents[0].occurrences[0].enclosing_range) == [10, 0, 20, 1]


def test_leaves_valid_3elem_same_line_enclosing_range_alone(tmp_path: Path) -> None:
    doc = scip_pb2.Document(relative_path="pkg/thing.go")
    occ = doc.occurrences.add()
    occ.symbol = "scip-go gomod example.com/thing v0.0.0 `pkg`/Thing()."
    occ.symbol_roles = 1
    occ.enclosing_range.extend([10, 0, 5])
    scip_path = _write_index(tmp_path, documents=[doc])

    assert _patch_invalid_enclosing_ranges(scip_path) == 0


def test_clears_3elem_same_line_range_where_end_char_before_start(tmp_path: Path) -> None:
    doc = scip_pb2.Document(relative_path="pkg/thing.go")
    occ = doc.occurrences.add()
    occ.symbol = "scip-go gomod example.com/thing v0.0.0 `pkg`/Thing()."
    occ.symbol_roles = 1
    occ.enclosing_range.extend([10, 20, 5])
    scip_path = _write_index(tmp_path, documents=[doc])

    assert _patch_invalid_enclosing_ranges(scip_path) == 1


def test_ignores_occurrences_with_no_enclosing_range(tmp_path: Path) -> None:
    doc = scip_pb2.Document(relative_path="pkg/thing.go")
    occ = doc.occurrences.add()
    occ.symbol = "scip-go gomod example.com/thing v0.0.0 `pkg`/Thing()."
    occ.symbol_roles = 1
    scip_path = _write_index(tmp_path, documents=[doc])

    assert _patch_invalid_enclosing_ranges(scip_path) == 0


def test_does_not_rewrite_file_when_nothing_needed(tmp_path: Path) -> None:
    doc = scip_pb2.Document(relative_path="pkg/thing.go")
    occ = doc.occurrences.add()
    occ.symbol = "scip-go gomod example.com/thing v0.0.0 `pkg`/Thing()."
    scip_path = _write_index(tmp_path, documents=[doc])
    before = scip_path.read_bytes()

    assert _patch_invalid_enclosing_ranges(scip_path) == 0
    assert scip_path.read_bytes() == before
