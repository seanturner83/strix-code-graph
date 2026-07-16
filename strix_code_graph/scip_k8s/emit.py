"""Build a SCIP protobuf Index from Kubernetes objects + cross-object references.

Thin wrapper over the vendored `scip_pb2` bindings (shared with scip_terraform,
generated from scip.proto — gencode 6.31.1, matches the sandbox `scip` CLI +
protobuf runtime). Given per-document object definitions and cross-object
reference occurrences, emit a valid `index.scip` that `scip expt-convert` turns
into the SQLite the code_graph query layer reads.

SCIP symbol string scheme for k8s (per the scip.proto grammar):

    scip-k8s . <package> <version> <descriptor>+

  package/version are '.' placeholders — a k8s object isn't a versioned
  artifact. The descriptor encodes namespace + kind + name so a
  ServiceAccount `foo` in namespace `bar` is `bar/ServiceAccount/foo#`. That
  gives stable, cross-file-unique symbols so a `serviceAccountName: foo`
  reference in a Deployment resolves to the SA's definition via
  `find_references`.

Ranges are 0-based [startLine, startChar, endChar] (three-element form). k8s
identifiers are ASCII so UTF-8/16/32 offsets coincide.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import scip_pb2

SCHEME = "scip-k8s"
# SymbolRole bitfield values from scip.proto
ROLE_DEFINITION = 0x1


def make_symbol(descriptor: str) -> str:
    """Global SCIP symbol string for a k8s object.

    `descriptor` is the pre-built chain (e.g. 'default/ServiceAccount/foo#').
    package + version are '.' placeholders."""
    return f"{SCHEME} . . . {descriptor}"


@dataclass
class Occurrence:
    """One symbol occurrence in a document (a rendered manifest)."""
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
    idx.metadata.tool_info.name = "scip-k8s"
    idx.metadata.tool_info.version = "0.1.0"
    idx.metadata.project_root = project_root.resolve().as_uri()
    idx.metadata.text_document_encoding = scip_pb2.UTF8CodeUnitOffsetFromLineStart

    for doc in documents:
        d = idx.documents.add()
        d.relative_path = doc.relative_path
        d.language = "yaml"
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
