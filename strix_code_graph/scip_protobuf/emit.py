"""Build a SCIP protobuf Index from Protobuf/Buf schema symbols + occurrences.

Thin wrapper over the vendored `scip_pb2` bindings (shared with
scip_terraform/scip_k8s, generated from scip.proto — matches the sandbox
`scip` CLI + protobuf runtime). Given per-document message/field/enum/
service/rpc definitions and cross-file/cross-repo type-reference
occurrences, emit a valid `index.scip` that `scip expt-convert` turns into
the SQLite the code_graph query layer reads.

SCIP symbol string scheme used for protobuf (per the scip.proto grammar):

    scip-proto proto . . <descriptor>+

Unlike scip_terraform/scip_k8s (which use `.` placeholders for BOTH the
scheme and package/version slots, since those constructs have no real
package identity), this uses a REAL scheme token ("proto", not ".") in the
package-manager-scheme slot. `_normalize_moniker_version` in the parent
indexer's merge_sqlite_indexes treats any moniker whose scheme slot is a
recognised package scheme as safe to blank-and-merge across repos on the
descriptor alone — protobuf's fully-qualified name already has no
version-drift problem (unlike a Go module pinning different versions), so
opting into that treatment for free is exactly what makes a message
defined in one repo and referenced in another resolve as the same symbol.
Package + version themselves stay `.` placeholders (protobuf doesn't have
a separate package-name-vs-descriptor split the way `scip-go gomod ...`
does; the descriptor's own fully-qualified path is the whole identity).

Descriptor format encodes the fully-qualified proto name with SCIP suffix
conventions: message `assets.asset.v1.Asset` -> `assets/asset/v1/Asset#`;
its field `name` -> `assets/asset/v1/Asset#name.`; service `AssetService`
-> `assets/asset/v1/AssetService#`; rpc `GetAsset` ->
`assets/asset/v1/AssetService#GetAsset().`; enum `Status` ->
`assets/asset/v1/Status#`; enum value `ACTIVE` ->
`assets/asset/v1/Status#ACTIVE.`.

Ranges are 0-based [startLine, startChar, endChar] (three-element form).
Every name/type-reference SourceCodeInfo location addresses a single
identifier token, so the single-line assumption this shares with
scip_terraform/scip_k8s is safe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import scip_pb2

SCHEME = "scip-proto"
PKG_SCHEME = "proto"
# SymbolRole bitfield values from scip.proto
ROLE_DEFINITION = 0x1


def make_symbol(descriptor: str) -> str:
    """Global SCIP symbol string for a protobuf construct.

    `descriptor` is the pre-built descriptor chain (e.g.
    'assets/asset/v1/Asset#' or 'assets/asset/v1/Asset#name.'). Package +
    version are '.' placeholders; the scheme slot is the real "proto"
    token (see module docstring) so cross-repo dedup treats this as a
    package-style moniker."""
    return f"{SCHEME} {PKG_SCHEME} . . {descriptor}"


@dataclass
class Occurrence:
    """One symbol occurrence in a document."""
    symbol: str
    start_line: int
    start_char: int
    end_char: int
    is_definition: bool = False


@dataclass
class Document:
    relative_path: str
    occurrences: list[Occurrence] = field(default_factory=list)
    # symbols defined in this document (SymbolInformation entries)
    defined_symbols: set[str] = field(default_factory=set)


def build_index(documents: list[Document], *, project_root: Path) -> scip_pb2.Index:
    """Assemble a SCIP Index message from the collected documents."""
    idx = scip_pb2.Index()
    idx.metadata.version = scip_pb2.UnspecifiedProtocolVersion
    idx.metadata.tool_info.name = "scip-protobuf"
    idx.metadata.tool_info.version = "0.1.0"
    idx.metadata.project_root = project_root.resolve().as_uri()
    idx.metadata.text_document_encoding = scip_pb2.UTF8CodeUnitOffsetFromLineStart

    for doc in documents:
        d = idx.documents.add()
        d.relative_path = doc.relative_path
        d.language = "proto"
        for occ in doc.occurrences:
            o = d.occurrences.add()
            o.range.extend([occ.start_line, occ.start_char, occ.end_char])
            o.symbol = occ.symbol
            if occ.is_definition:
                o.symbol_roles = ROLE_DEFINITION
        for sym in sorted(doc.defined_symbols):
            si = d.symbols.add()
            si.symbol = sym
    return idx


def write_index(index: scip_pb2.Index, out_path: Path) -> Path:
    out_path.write_bytes(index.SerializeToString())
    return out_path
