"""scip-protobuf: a Protobuf/Buf schema -> SCIP indexer, driven by `buf build`.

Pipeline:
  1. `buf build -o - --as-file-descriptor-set --exclude-imports` against the
     target's own buf module (searching for buf.yaml/buf.work.yaml the same
     way buf itself does -- CWD or the first one found by descending into
     the target, since buf doesn't search descendant directories on its
     own). buf still resolves all deps (BSR modules, buf.lock) to build/
     validate the schema; --exclude-imports just means the returned
     FileDescriptorSet.file list contains ONLY the target module's own
     files -- no local-vs-imported file-detection heuristic needed, every
     file in the result is local.
  2. parse the binary FileDescriptorSet.
  3. walk each file's message/enum/service declarations (recursing into
     nested messages), using SourceCodeInfo.location's `path` (a
     field-number path into the descriptor tree) + `span` to emit a
     precise definition occurrence at each construct's own NAME span, and
     a reference occurrence at every field's type_name / method's
     input_type/output_type span -- these are ALREADY fully-qualified by
     protobuf's own compiler using full import context, so no resolution
     logic is needed, and they're emitted UNCONDITIONALLY (even pointing
     at a type this repo doesn't define, e.g. another repo's schema or
     google.protobuf.*) -- that's what lets the merge's cross-repo
     descriptor-keyed dedup (see emit.py's module docstring) unify a
     reference-only row here with the definition row in whichever repo
     actually defines it.
  4. emit one SCIP index; `scip expt-convert` (called by the parent
     indexer) folds it into the queryable SQLite.

Scope note: reference graph (definitions + type references), not a fully
general protobuf compiler frontend -- matches scip_terraform/scip_k8s's
own stated scope. Skipped: import-statement-level occurrences (no clean
symbol target) and RPC streaming-flag/extension symbols (no graph value).
oneof/map fields are walked as regular fields -- their type_name
resolution is identical either way.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from google.protobuf import descriptor_pb2

from .emit import Document, Occurrence, build_index, make_symbol, write_index

logger = logging.getLogger(__name__)

_BUILD_TIMEOUT_S = 120
_TYPE_MESSAGE = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
_TYPE_ENUM = descriptor_pb2.FieldDescriptorProto.TYPE_ENUM


def _find_buf_root(target: Path) -> Path | None:
    """buf.yaml/buf.work.yaml at target itself, else the first one found
    by descending into target -- buf searches CWD + ancestors on its own,
    never descendants, so a repo that nests its schema under e.g. api/
    with no workspace file at the root needs this to find anything."""
    for name in ("buf.work.yaml", "buf.yaml"):
        if (target / name).exists():
            return target
    candidates = sorted(target.rglob("buf.work.yaml")) + sorted(target.rglob("buf.yaml"))
    return candidates[0].parent if candidates else None


def _run_buf_build(buf_root: Path) -> bytes | None:
    cmd = ["buf", "build", "-o", "-", "--as-file-descriptor-set", "--exclude-imports"]
    try:
        proc = subprocess.run(
            cmd, cwd=buf_root, capture_output=True, timeout=_BUILD_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("scip-protobuf: buf build failed for %s: %s", buf_root, exc)
        return None
    if proc.returncode != 0:
        logger.warning(
            "scip-protobuf: buf build returned %d for %s: %s",
            proc.returncode, buf_root,
            (proc.stderr or b"").decode("utf-8", "replace").strip()[:300],
        )
        return None
    return proc.stdout


def _location_map(file_proto) -> dict[tuple[int, ...], list[int]]:
    """path tuple -> span for one file's source_code_info -- computed once
    per file so the walk below is plain dict lookups, not a linear scan
    per construct."""
    return {tuple(loc.path): list(loc.span) for loc in file_proto.source_code_info.location}


def _span_to_range(span: list[int] | None) -> tuple[int, int, int] | None:
    """(start_line, start_char, end_char) from a 3- or 4-elem span. Every
    name/type-reference location addresses a single identifier token, so
    a 4-elem span with differing start/end line would be unexpected --
    degrade (skip this one occurrence) rather than emit a wrong range."""
    if not span:
        return None
    if len(span) == 3:
        return span[0], span[1], span[2]
    if len(span) == 4:
        start_line, start_char, end_line, end_char = span
        if start_line != end_line:
            logger.debug("scip-protobuf: unexpected multi-line name/type span %s; skipping", span)
            return None
        return start_line, start_char, end_char
    return None


def _emit_definition(doc: Document, symbol: str, locs: dict, path: tuple[int, ...]) -> None:
    rng = _span_to_range(locs.get(path))
    if rng is None:
        return
    sl, sc, ec = rng
    doc.occurrences.append(Occurrence(symbol=symbol, start_line=sl, start_char=sc,
                                       end_char=ec, is_definition=True))
    doc.defined_symbols.add(symbol)


def _emit_reference(doc: Document, symbol: str, locs: dict, path: tuple[int, ...]) -> None:
    rng = _span_to_range(locs.get(path))
    if rng is None:
        return
    sl, sc, ec = rng
    doc.occurrences.append(Occurrence(symbol=symbol, start_line=sl, start_char=sc,
                                       end_char=ec, is_definition=False))


def _ref_descriptor(fully_qualified_type_name: str) -> str:
    """'.assets.asset.v1.Money' (protobuf's own leading-dot fully-qualified
    form) -> 'assets/asset/v1/Money#'."""
    return fully_qualified_type_name.lstrip(".").replace(".", "/") + "#"


def _walk_message(msg, path: tuple[int, ...], name_parts: list[str],
                   locs: dict, doc: Document) -> None:
    name_parts = [*name_parts, msg.name]
    descriptor = "/".join(name_parts) + "#"
    _emit_definition(doc, make_symbol(descriptor), locs, path + (1,))

    for i, f in enumerate(msg.field):
        field_path = path + (2, i)
        _emit_definition(doc, make_symbol(f"{descriptor}{f.name}."), locs, field_path + (1,))
        if f.type in (_TYPE_MESSAGE, _TYPE_ENUM) and f.type_name:
            ref_symbol = make_symbol(_ref_descriptor(f.type_name))
            _emit_reference(doc, ref_symbol, locs, field_path + (6,))

    for i, nested in enumerate(msg.nested_type):
        _walk_message(nested, path + (3, i), name_parts, locs, doc)
    for i, enum in enumerate(msg.enum_type):
        _walk_enum(enum, path + (4, i), name_parts, locs, doc)


def _walk_enum(enum, path: tuple[int, ...], name_parts: list[str],
               locs: dict, doc: Document) -> None:
    name_parts = [*name_parts, enum.name]
    descriptor = "/".join(name_parts) + "#"
    _emit_definition(doc, make_symbol(descriptor), locs, path + (1,))
    for i, v in enumerate(enum.value):
        _emit_definition(doc, make_symbol(f"{descriptor}{v.name}."), locs, path + (2, i, 1))


def _walk_service(svc, path: tuple[int, ...], name_parts: list[str],
                   locs: dict, doc: Document) -> None:
    name_parts = [*name_parts, svc.name]
    descriptor = "/".join(name_parts) + "#"
    _emit_definition(doc, make_symbol(descriptor), locs, path + (1,))
    for i, m in enumerate(svc.method):
        method_path = path + (2, i)
        _emit_definition(doc, make_symbol(f"{descriptor}{m.name}()."), locs, method_path + (1,))
        for type_field, type_name in ((2, m.input_type), (3, m.output_type)):
            if type_name:
                ref_symbol = make_symbol(_ref_descriptor(type_name))
                _emit_reference(doc, ref_symbol, locs, method_path + (type_field,))


def _walk_file(file_proto, locs: dict, doc: Document) -> None:
    root_parts = file_proto.package.split(".") if file_proto.package else []
    for i, msg in enumerate(file_proto.message_type):
        _walk_message(msg, (4, i), root_parts, locs, doc)
    for i, enum in enumerate(file_proto.enum_type):
        _walk_enum(enum, (5, i), root_parts, locs, doc)
    for i, svc in enumerate(file_proto.service):
        _walk_service(svc, (6, i), root_parts, locs, doc)


def index(target: Path, out_dir: Path) -> Path | None:
    """Build a SCIP index for the protobuf schema under `target`. Returns
    the path to the written index.scip, or None if there's no protobuf /
    buf is unavailable / the build failed."""
    target = Path(target)
    if not any(target.rglob("*.proto")):
        return None
    buf_root = _find_buf_root(target)
    if buf_root is None:
        return None
    stdout = _run_buf_build(buf_root)
    if stdout is None:
        return None

    fds = descriptor_pb2.FileDescriptorSet()
    try:
        fds.ParseFromString(stdout)
    except Exception as exc:  # noqa: BLE001 -- malformed output must degrade, not crash the leg
        logger.warning("scip-protobuf: failed to parse buf build output for %s: %s", target, exc)
        return None

    documents: list[Document] = []
    for file_proto in fds.file:
        doc = Document(relative_path=file_proto.name)
        _walk_file(file_proto, _location_map(file_proto), doc)
        if doc.occurrences:
            documents.append(doc)

    if not documents:
        return None
    idx = build_index(documents, project_root=target)
    return write_index(idx, out_dir / "proto.scip")
