"""Terraform → SCIP indexer, driven by terraform-ls over LSP.

Standard SCIP-indexer shape (per Sourcegraph's "writing an indexer" docs): use a
language server for the semantic resolution, traverse its symbols, convert to
SCIP. terraform-ls (HashiCorp, official) supplies textDocument/documentSymbol +
references; this module walks every *.tf under the target, turns each symbol's
declaration into a SCIP definition occurrence and each reference into a
reference occurrence, and emits one index.scip.

Decoupled from strix (view-to-OSS): the only strix coupling is that
strix.tools.code_graph.indexer calls `index(target, out_dir)` here. Everything
else depends on terraform-ls + the vendored scip_pb2.

Scope note: this is a REFERENCE graph (definitions + uses of
resources/modules/variables/outputs/locals/data), not a full semantic SCIP with
provider-schema type info. That's exactly what the code_graph consumer needs —
"is this construct referenced anywhere / where's it defined" for reachability
and guard grounding on IaC findings.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .emit import Document, Occurrence, build_index, make_symbol, write_index
from .lsp_client import LSPClient, LSPError

logger = logging.getLogger(__name__)

# LSP SymbolKind → terraform construct → SCIP descriptor suffix. terraform-ls
# maps HCL blocks onto LSP kinds; we translate the kind + name into a stable
# descriptor. Anything unmapped falls back to a namespace descriptor ('/').
# (LSP SymbolKind ints: 5=Class, 6=Method, 8=Field, 13=Variable, 23=Struct, ...)
_KIND_SUFFIX = {
    23: "#",   # Struct  → resource / data block  → type descriptor
    5: "#",    # Class   → module block
    13: ".",   # Variable→ variable / local        → term descriptor
    8: ".",    # Field   → output / attribute
}


def _descriptor(name: str, kind: int) -> str:
    """Build a SCIP descriptor chain for a terraform symbol name + LSP kind.

    terraform-ls documentSymbol names look like `resource "aws_s3_bucket" "logs"`
    or `variable "region"` or `module "vpc"`. Normalise to a dotted path so the
    symbol is stable + unique across files."""
    parts = [p.strip('"') for p in name.split() if p.strip('"')]
    suffix = _KIND_SUFFIX.get(kind, "/")
    # join the block-type + labels into a namespaced path, terminal descriptor
    # carries the suffix; e.g. ['resource','aws_s3_bucket','logs'] →
    # 'resource/aws_s3_bucket/logs#'
    if not parts:
        return "unknown/"
    chain = "/".join(parts[:-1])
    tail = parts[-1] + suffix
    return f"{chain}/{tail}" if chain else tail


def _flatten_symbols(syms: list[dict], out: list[dict]) -> None:
    """documentSymbol can be hierarchical (DocumentSymbol) or flat
    (SymbolInformation). Flatten either into a list of {name,kind,range}."""
    for s in syms:
        if "location" in s:  # SymbolInformation
            rng = s["location"]["range"]
            out.append({"name": s["name"], "kind": s.get("kind", 0), "range": rng})
        else:                # DocumentSymbol (hierarchical)
            rng = s.get("selectionRange") or s.get("range")
            if rng:
                out.append({"name": s["name"], "kind": s.get("kind", 0), "range": rng})
            if s.get("children"):
                _flatten_symbols(s["children"], out)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return path.name


def index(target: Path, out_dir: Path, *,
          server_cmd: list[str] | None = None) -> Path | None:
    """Build a SCIP index for the terraform under `target`. Returns the path to
    the written index.scip, or None if there's no terraform / no server."""
    target = Path(target)
    tf_files = sorted(target.rglob("*.tf"))
    if not tf_files:
        return None
    cmd = server_cmd or ["terraform-ls", "serve"]

    documents: list[Document] = []
    try:
        with LSPClient(cmd, target) as lsp:
            for tf in tf_files:
                try:
                    text = tf.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                lsp.did_open(tf, text)
                rel = _rel(tf, target)
                doc = Document(relative_path=rel)
                flat: list[dict] = []
                try:
                    _flatten_symbols(lsp.document_symbol(tf), flat)
                except LSPError as e:
                    logger.warning("scip-terraform: documentSymbol failed on %s: %s", rel, e)
                    continue
                for sym in flat:
                    descriptor = _descriptor(sym["name"], sym["kind"])
                    symbol = make_symbol(descriptor)
                    r = sym["range"]
                    sl, sc = r["start"]["line"], r["start"]["character"]
                    ec = r["end"]["character"] if r["end"]["line"] == sl else sc + 1
                    # definition occurrence
                    doc.occurrences.append(Occurrence(
                        symbol=symbol, start_line=sl, start_char=sc,
                        end_char=ec, is_definition=True))
                    doc.defined_symbols.add(symbol)
                    # references across the workspace
                    try:
                        for ref in lsp.references(tf, sl, sc):
                            rr = ref.get("range") or {}
                            rsl = rr.get("start", {}).get("line")
                            rsc = rr.get("start", {}).get("character")
                            rec = rr.get("end", {}).get("character", (rsc or 0) + 1)
                            ref_uri = ref.get("uri", "")
                            ref_path = _uri_to_path(ref_uri)
                            if rsl is None or ref_path is None:
                                continue
                            _add_reference(documents, doc, target, ref_path,
                                           symbol, rsl, rsc, rec)
                    except LSPError as e:
                        logger.debug("scip-terraform: references failed for %s: %s",
                                     descriptor, e)
                documents.append(doc)
    except LSPError as e:
        logger.warning("scip-terraform: language server unavailable (%s); no index", e)
        return None

    if not any(d.occurrences for d in documents):
        return None
    idx = build_index(_dedup_docs(documents), project_root=target)
    return write_index(idx, out_dir / "tf.scip")


def _uri_to_path(uri: str) -> Path | None:
    if not uri.startswith("file://"):
        return None
    from urllib.parse import unquote, urlparse
    return Path(unquote(urlparse(uri).path))


def _add_reference(documents: list[Document], current: Document, root: Path,
                   ref_path: Path, symbol: str,
                   line: int, char: int, end_char: int) -> None:
    """Attach a reference occurrence to the right document (may be a different
    .tf file than where the symbol is defined)."""
    rel = _rel(ref_path, root)
    if rel == current.relative_path:
        target_doc = current
    else:
        target_doc = next((d for d in documents if d.relative_path == rel), None)
        if target_doc is None:
            target_doc = Document(relative_path=rel)
            documents.append(target_doc)
    target_doc.occurrences.append(Occurrence(
        symbol=symbol, start_line=line, start_char=char,
        end_char=end_char, is_definition=False))


def _dedup_docs(documents: list[Document]) -> list[Document]:
    """Merge documents that share a relative_path (a ref may create a stub doc
    before the file's own definitions are walked)."""
    by_path: dict[str, Document] = {}
    for d in documents:
        cur = by_path.get(d.relative_path)
        if cur is None:
            by_path[d.relative_path] = d
        else:
            cur.occurrences.extend(d.occurrences)
            cur.defined_symbols |= d.defined_symbols
    return list(by_path.values())
