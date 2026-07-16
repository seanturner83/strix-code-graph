"""Build a SCIP protobuf Index from Terraform symbols + occurrences.

Thin, consumer-agnostic wrapper over the vendored `scip_pb2` bindings (generated
from scip.proto v0.8.0 — matches the sandbox `scip` CLI). Given a set of
per-document symbol definitions and reference occurrences, emit a valid
`index.scip` that `scip expt-convert` can turn into the SQLite the code_graph
query layer reads.

SCIP symbol string scheme used for terraform (per the scip.proto grammar):

    scip-terraform . <package> <version> <descriptor>+

  package/version are placeholders ('.') — a terraform "package" isn't a
  versioned artifact. The descriptor path encodes the block, e.g. a resource
  `aws_s3_bucket.logs` becomes the namespace/term chain
  `aws_s3_bucket/logs#`. That gives stable, human-readable, cross-file-unique
  symbols so `find_definition`/`find_references` resolve across the module.

Ranges are 0-based [startLine, startChar, endChar] (three-element form, per the
proto — end line inferred == start line, which is always true for the
single-line identifier occurrences terraform-ls reports).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import scip_pb2

SCHEME = "scip-terraform"
# SymbolRole bitfield values from scip.proto
ROLE_DEFINITION = 0x1


def make_symbol(descriptor: str) -> str:
    """Global SCIP symbol string for a terraform construct.

    `descriptor` is the pre-built descriptor chain (e.g. 'aws_s3_bucket/logs#'
    or 'var/region.'). package + version are '.' placeholders."""
    return f"{SCHEME} . . . {descriptor}"


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
    idx.metadata.tool_info.name = "scip-terraform"
    idx.metadata.tool_info.version = "0.1.0"
    idx.metadata.project_root = project_root.resolve().as_uri()
    # 3 = UTF-32; terraform-ls reports UTF-16 offsets, but identifiers are ASCII
    # so 8/16/32 coincide — pick a concrete value so consumers aren't ambiguous.
    idx.metadata.text_document_encoding = scip_pb2.UTF8CodeUnitOffsetFromLineStart

    for doc in documents:
        d = idx.documents.add()
        d.relative_path = doc.relative_path
        d.language = "terraform"
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
