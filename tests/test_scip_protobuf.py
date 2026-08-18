"""Tests for scip_protobuf -- the buf-build-driven Protobuf/Buf SCIP indexer.

Two layers: fine-grained unit tests for the descriptor-walk helpers (no
buf/subprocess dependency), and a real end-to-end test against an actual
`buf build` invocation (skipped if the buf CLI isn't installed, matching
test_indexer_env_scrub.py's rust-analyzer/cargo skip convention).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2

from strix_code_graph import indexer
from strix_code_graph.scip_protobuf.emit import Document, make_symbol
from strix_code_graph.scip_protobuf.indexer import (
    _find_buf_root,
    _location_map,
    _ref_descriptor,
    _span_to_range,
    _walk_file,
)
from strix_code_graph.scip_protobuf.indexer import (
    index as proto_index,
)


def test_make_symbol_uses_a_real_scheme_token_not_a_dot_placeholder() -> None:
    """Unlike scip_terraform/scip_k8s (`.` in the scheme slot), protobuf
    monikers need a REAL package-scheme token so the parent indexer's
    merge_sqlite_indexes (_normalize_moniker_version) treats them as
    safe-to-merge-across-repos package monikers, not label-qualified
    locals -- see emit.py's module docstring."""
    sym = make_symbol("assets/asset/v1/Asset#")
    parts = sym.split(" ")
    assert len(parts) == 5
    assert parts[1] == "proto"


def test_ref_descriptor_strips_leading_dot_and_converts_dots_to_slashes() -> None:
    # protobuf's own fully-qualified type_name/input_type/output_type form.
    assert _ref_descriptor(".testpkg.v1.Status") == "testpkg/v1/Status#"


def test_span_to_range_handles_3_and_4_element_spans() -> None:
    assert _span_to_range([1, 2, 3]) == (1, 2, 3)
    assert _span_to_range([1, 2, 1, 5]) == (1, 2, 5)


def test_span_to_range_skips_unexpected_multiline_span() -> None:
    # A name/type-reference location should always be single-line; degrade
    # rather than emit a wrong range if that assumption is ever violated.
    assert _span_to_range([1, 2, 3, 5]) is None


def test_span_to_range_none_for_missing_span() -> None:
    assert _span_to_range(None) is None
    assert _span_to_range([]) is None


def test_find_buf_root_at_target_itself(tmp_path: Path) -> None:
    (tmp_path / "buf.yaml").write_text("version: v1\n")
    assert _find_buf_root(tmp_path) == tmp_path


def test_find_buf_root_descends_when_not_at_target_root(tmp_path: Path) -> None:
    # e.g. assets-messages: buf.work.yaml at the repo root -- but a repo
    # that nests its schema under api/ with NO workspace file at the root
    # needs this, since buf itself never searches descendant directories.
    nested = tmp_path / "api"
    nested.mkdir()
    (nested / "buf.yaml").write_text("version: v1\n")
    assert _find_buf_root(tmp_path) == nested


def test_find_buf_root_none_when_nothing_found(tmp_path: Path) -> None:
    assert _find_buf_root(tmp_path) is None


def test_walk_file_emits_definitions_and_references_from_synthetic_descriptor() -> None:
    """Hand-built FileDescriptorProto + SourceCodeInfo -- exercises the
    path-encoding logic directly without needing buf at all. Covers the
    field-number shift (enum_type is field 5 on FileDescriptorProto but
    field 4 on DescriptorProto) via a message with a nested enum."""
    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = "pkg/thing.proto"
    file_proto.package = "pkg"

    msg = file_proto.message_type.add()
    msg.name = "Thing"
    f = msg.field.add()
    f.name = "status"
    f.number = 1
    f.type = descriptor_pb2.FieldDescriptorProto.TYPE_ENUM
    f.type_name = ".pkg.Thing.Status"
    # nested enum -- DescriptorProto.enum_type is field 4, NOT 5.
    nested_enum = msg.enum_type.add()
    nested_enum.name = "Status"
    val = nested_enum.value.add()
    val.name = "ACTIVE"
    val.number = 0

    # path encoding: message_type=4 idx=0 -> [4,0]; its field=2 idx=0 ->
    # [4,0,2,0]; nested enum_type=4 idx=0 -> [4,0,4,0]; enum value=2 idx=0
    # -> [4,0,4,0,2,0]. name field=1 on every descriptor type; type_name
    # field=6 on FieldDescriptorProto.
    def _loc(path, span):
        loc = file_proto.source_code_info.location.add()
        loc.path.extend(path)
        loc.span.extend(span)

    _loc([4, 0, 1], [1, 8, 13])            # message Thing's own name
    _loc([4, 0, 2, 0, 1], [2, 9, 15])       # field status's own name
    _loc([4, 0, 2, 0, 6], [2, 18, 24])      # field status's type reference
    _loc([4, 0, 4, 0, 1], [5, 7, 13])       # nested enum Status's name
    _loc([4, 0, 4, 0, 2, 0, 1], [6, 2, 8])  # enum value ACTIVE's name

    doc = Document(relative_path=file_proto.name)
    _walk_file(file_proto, _location_map(file_proto), doc)

    assert doc.defined_symbols == {
        make_symbol("pkg/Thing#"),
        make_symbol("pkg/Thing#status."),
        make_symbol("pkg/Thing/Status#"),
        make_symbol("pkg/Thing/Status#ACTIVE."),
    }
    ref_occs = [o for o in doc.occurrences if not o.is_definition]
    assert len(ref_occs) == 1
    # .pkg.Thing.Status -- a nested enum's fully-qualified name correctly
    # includes its parent message, so the reference resolves to the SAME
    # descriptor the nested enum itself was defined under above.
    assert ref_occs[0].symbol == make_symbol("pkg/Thing/Status#")
    assert (ref_occs[0].start_line, ref_occs[0].start_char, ref_occs[0].end_char) == (2, 18, 24)


def test_walk_file_skips_string_fields_with_no_type_name() -> None:
    """A plain scalar field (string/int/etc.) has type_name == '' -- must
    not emit a reference occurrence pointing at an empty descriptor."""
    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = "pkg/thing.proto"
    file_proto.package = "pkg"
    msg = file_proto.message_type.add()
    msg.name = "Thing"
    f = msg.field.add()
    f.name = "name"
    f.number = 1
    f.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING

    doc = Document(relative_path=file_proto.name)
    _walk_file(file_proto, _location_map(file_proto), doc)

    assert not any(not o.is_definition for o in doc.occurrences)


def test_index_returns_none_when_no_proto_files(tmp_path: Path) -> None:
    assert proto_index(tmp_path, tmp_path) is None


def test_index_returns_none_when_no_buf_root_found(tmp_path: Path) -> None:
    (tmp_path / "schema.proto").write_text('syntax = "proto3";\n')
    assert proto_index(tmp_path, tmp_path) is None


def test_index_via_real_buf_build(tmp_path: Path) -> None:
    """End-to-end: a real, small buf module built via the actual buf CLI,
    parsed, walked, and emitted as a real SCIP index."""
    if not indexer._binary_exists("buf"):
        pytest.skip("buf CLI not available in this environment")

    (tmp_path / "buf.yaml").write_text("version: v1\n")
    pkg_dir = tmp_path / "testpkg" / "v1"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "asset.proto").write_text(
        'syntax = "proto3";\n\n'
        "package testpkg.v1;\n\n"
        "enum Status {\n"
        "  STATUS_UNSPECIFIED = 0;\n"
        "  STATUS_ACTIVE = 1;\n"
        "}\n\n"
        "message Asset {\n"
        "  string name = 1;\n"
        "  Status status = 2;\n"
        "}\n\n"
        "message AssetRequest {\n"
        "  string asset_id = 1;\n"
        "}\n\n"
        "service AssetService {\n"
        "  rpc GetAsset(AssetRequest) returns (Asset);\n"
        "}\n",
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = proto_index(tmp_path, out_dir)
    assert result is not None
    assert result.exists()
    assert result.name == "proto.scip"

    from strix_code_graph.scip_protobuf import scip_pb2
    idx = scip_pb2.Index()
    idx.ParseFromString(result.read_bytes())
    assert len(idx.documents) == 1
    doc = idx.documents[0]
    assert doc.relative_path == "testpkg/v1/asset.proto"
    all_symbols = {si.symbol for si in doc.symbols}
    assert make_symbol("testpkg/v1/Asset#") in all_symbols
    assert make_symbol("testpkg/v1/AssetService#GetAsset().") in all_symbols
    ref_symbols = {o.symbol for o in doc.occurrences if not (o.symbol_roles & 0x1)}
    assert make_symbol("testpkg/v1/Status#") in ref_symbols
    assert make_symbol("testpkg/v1/Asset#") in ref_symbols  # GetAsset's return type
