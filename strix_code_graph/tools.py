"""SCIP code-graph query tools, exposed to the Strix agent via ``@function_tool``.

Each tool opens the read-only SQLite code-graph index (built once per scan — see
``bootstrap`` for the session-setup-hook build) and calls into
``query.CodeGraphIndex``.

Failure modes are deliberately soft — the agent should fall back to its normal
grep/read_file path rather than erroring:
  - No index (build skipped, unsupported language): "code graph unavailable".
  - Symbol not found: "no matches".
  - Query/read error: an ``error`` string (the tool never raises).

Structure: each tool's logic is a plain sync ``_do_*`` function (directly unit
testable); the ``@function_tool`` wrapper is a thin async delegate taking the
SDK ``RunContextWrapper``. The query logic is provider-agnostic; the wrappers
``@register_tool`` to the v1.x SDK ``@function_tool`` (the obsolete
``parallel_safe`` flag dropped; v1.x batches natively).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from agents import RunContextWrapper, function_tool

from .query import CodeGraphIndex, Location

# See bootstrap.py's comment: renamed to a "strix."-prefixed child so
# Strix's own logging setup actually attaches a handler to it.
logger = logging.getLogger(__name__.replace("strix_code_graph", "strix.code_graph", 1))

# Per-tool cap. A single graph query returning hundreds of locations is
# unhelpful; the LLM is better served by 50 and a follow-up if it needs more.
LLM_RESULT_LIMIT = 50

# code_graph_grep reads source around each hit, so its cap is tighter than the
# location-only tools: every reference shown costs (2*context+1) lines.
GREP_REF_LIMIT = 15
GREP_DEFAULT_CONTEXT = 3
GREP_MAX_CONTEXT = 10
GREP_MAX_DEF_BODY_LINES = 120


def _open_index() -> CodeGraphIndex | None:
    """Discover and open the code-graph index, or None if no index was built
    for this scan (tools then degrade to "unavailable" rather than erroring)."""
    return CodeGraphIndex.discover()


def _render_unavailable() -> dict[str, Any]:
    return {
        "output": (
            "code graph not available for this target — indexer was skipped or "
            "the language is unsupported. Use search_files / list_files instead."
        )
    }


def _render_no_matches(kind: str, query: str) -> dict[str, Any]:
    return {"output": f"no {kind} matches found for {query!r}"}


# --- query tools: sync logic (_do_*) + thin async @function_tool wrappers -----


def _do_find_definition(symbol: str) -> dict[str, Any]:
    idx = _open_index()
    if idx is None:
        return _render_unavailable()
    try:
        results = idx.find_definition(symbol, limit=LLM_RESULT_LIMIT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph_find_definition failed: %s", exc)
        return {"error": f"code graph query failed: {exc}"}
    finally:
        idx.close()

    if not results:
        return _render_no_matches("definition", symbol)
    lines = [f"{m.display_name} defined at {loc.render()}" for m, loc in results]
    return {"output": "\n".join(lines)}


def _do_find_references(symbol: str, include_definition: bool = False) -> dict[str, Any]:
    idx = _open_index()
    if idx is None:
        return _render_unavailable()
    try:
        results = idx.find_references(
            symbol, limit=LLM_RESULT_LIMIT, include_definition=include_definition,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph_find_references failed: %s", exc)
        return {"error": f"code graph query failed: {exc}"}
    finally:
        idx.close()

    if not results:
        return _render_no_matches("reference", symbol)

    by_name: dict[str, list[str]] = {}
    for match, loc in results:
        by_name.setdefault(match.display_name, []).append(loc.render())
    blocks: list[str] = []
    for name, locs in by_name.items():
        blocks.append(f"{name} — {len(locs)} occurrence(s):")
        blocks.extend(f"  {loc}" for loc in locs)
    if len(results) >= LLM_RESULT_LIMIT:
        blocks.append(f"… result capped at {LLM_RESULT_LIMIT}; narrow the symbol name.")
    return {"output": "\n".join(blocks)}


def _do_find_implementations(interface: str) -> dict[str, Any]:
    idx = _open_index()
    if idx is None:
        return _render_unavailable()
    try:
        results = idx.find_implementations(interface, limit=LLM_RESULT_LIMIT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph_find_implementations failed: %s", exc)
        return {"error": f"code graph query failed: {exc}"}
    finally:
        idx.close()

    if not results:
        return _render_no_matches("implementation", interface)
    lines = [f"{m.display_name} implements {interface} at {loc.render()}" for m, loc in results]
    if len(results) >= LLM_RESULT_LIMIT:
        lines.append(f"… result capped at {LLM_RESULT_LIMIT}; narrow the interface name.")
    return {"output": "\n".join(lines)}


def _do_list_symbols(scope: str) -> dict[str, Any]:
    idx = _open_index()
    if idx is None:
        return _render_unavailable()
    try:
        results = idx.list_symbols(scope, limit=LLM_RESULT_LIMIT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph_list_symbols failed: %s", exc)
        return {"error": f"code graph query failed: {exc}"}
    finally:
        idx.close()

    if not results:
        return _render_no_matches("symbol under", scope)
    by_file: dict[str, list[str]] = {}
    for match, loc in results:
        by_file.setdefault(loc.file, []).append(
            f"{match.display_name} (line {loc.start_line})",
        )
    blocks: list[str] = []
    for path, syms in by_file.items():
        blocks.append(f"{path}:")
        blocks.extend(f"  {s}" for s in syms)
    if len(results) >= LLM_RESULT_LIMIT:
        blocks.append(f"… result capped at {LLM_RESULT_LIMIT}; narrow the scope.")
    return {"output": "\n".join(blocks)}


def _do_get_imports(file: str) -> dict[str, Any]:
    idx = _open_index()
    if idx is None:
        return _render_unavailable()
    try:
        imports = idx.get_imports(file, limit=LLM_RESULT_LIMIT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph_get_imports failed: %s", exc)
        return {"error": f"code graph query failed: {exc}"}
    finally:
        idx.close()

    if not imports:
        return _render_no_matches("imports/references", file)
    names = sorted({m.display_name for m in imports})
    if len(names) >= LLM_RESULT_LIMIT:
        return {
            "output": (
                f"{file}: {len(names)} referenced symbols (showing first "
                f"{LLM_RESULT_LIMIT}):\n  " + "\n  ".join(names)
            )
        }
    return {"output": f"{file} references:\n  " + "\n  ".join(names)}


def _do_get_symbol_at(file: str, line: int) -> dict[str, Any]:
    idx = _open_index()
    if idx is None:
        return _render_unavailable()
    try:
        symbols = idx.get_symbol_at(file, line)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph_get_symbol_at failed: %s", exc)
        return {"error": f"code graph query failed: {exc}"}
    finally:
        idx.close()

    if not symbols:
        return _render_no_matches("symbol", f"{file}:{line}")
    names = sorted({m.display_name for m in symbols})
    return {"output": f"{file}:{line} mentions:\n  " + "\n  ".join(names)}


# --- code_graph_grep helpers --------------------------------------------------


def _workspace_root() -> Path:
    return Path(os.environ.get("STRIX_WORKSPACE_ROOT", "/workspace"))


def _read_lines(rel_path: str) -> list[str] | None:
    root = _workspace_root()
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        logger.warning("code_graph_grep: refusing path outside workspace: %s", rel_path)
        return None
    try:
        return target.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError) as exc:
        logger.debug("code_graph_grep: cannot read %s: %s", target, exc)
        return None


def _render_window(rel_path: str, lines: list[str], start_line: int, end_line: int) -> list[str]:
    lo = max(0, start_line)
    hi = min(len(lines) - 1, end_line)
    return [f"  {i + 1:>5} {lines[i]}" for i in range(lo, hi + 1)]


def _render_def_body(rel_path: str, loc: Location) -> list[str]:
    header = f"● DEFINITION  {loc.render()}"
    lines = _read_lines(rel_path)
    if lines is None:
        return [header, "    (source unavailable — read the file directly)"]
    span = loc.end_line - loc.start_line + 1
    if span > GREP_MAX_DEF_BODY_LINES:
        last = loc.start_line + GREP_MAX_DEF_BODY_LINES - 1
        body = _render_window(rel_path, lines, loc.start_line, last)
        body.append(
            f"    … definition body truncated at {GREP_MAX_DEF_BODY_LINES} lines "
            f"(spans to line {loc.end_line + 1}); read the file for the rest.",
        )
        return [header, *body]
    return [header, *_render_window(rel_path, lines, loc.start_line, loc.end_line)]


def _render_ref_window(
    rel_path: str, lines: list[str] | None, loc: Location, context: int,
) -> list[str]:
    header = f"● REFERENCE  {loc.render()}"
    if lines is None:
        return [header, "    (source unavailable — read the file directly)"]
    window = _render_window(rel_path, lines, loc.start_line - context, loc.end_line + context)
    return [header, *window]


def _do_grep(symbol: str, context: int = GREP_DEFAULT_CONTEXT) -> dict[str, Any]:
    context = max(0, min(int(context), GREP_MAX_CONTEXT))
    idx = _open_index()
    if idx is None:
        return _render_unavailable()
    try:
        defs = idx.find_definition(symbol, limit=LLM_RESULT_LIMIT)
        refs = idx.find_references(symbol, limit=GREP_REF_LIMIT, include_definition=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph_grep failed: %s", exc)
        return {"error": f"code graph query failed: {exc}"}
    finally:
        idx.close()

    if not defs and not refs:
        return _render_no_matches("symbol", symbol)

    blocks: list[str] = []
    for match, loc in defs:
        if blocks:
            blocks.append("")
        blocks.append(f"# {match.display_name}")
        blocks.extend(_render_def_body(loc.file, loc))

    if refs:
        if blocks:
            blocks.append("")
        n_refs = len({(loc.file, loc.start_line) for _, loc in refs})
        blocks.append(f"# references ({n_refs} shown, ±{context} lines)")
        file_cache: dict[str, list[str] | None] = {}
        for _match, loc in refs:
            if loc.file not in file_cache:
                file_cache[loc.file] = _read_lines(loc.file)
            blocks.extend(_render_ref_window(loc.file, file_cache[loc.file], loc, context))
        if len(refs) >= GREP_REF_LIMIT:
            blocks.append(
                f"… references capped at {GREP_REF_LIMIT}; use "
                "code_graph_find_references for the full location list.",
            )

    return {"output": "\n".join(blocks)}


# Per-site enrichment (get_symbol_at) is one extra query per reference; cap the
# references we enrich so a hot type doesn't fan out into hundreds of queries.
EVENT_FLOW_REF_LIMIT = 60


def _do_find_event_flow(message_type: str) -> dict[str, Any]:
    """Cross-service event-flow view for a shared message TYPE.

    A message-bus hop (NATS/Kafka/pub-sub) has no code edge — but the message
    TYPE is a shared symbol the graph resolves across repos, so we take that
    type's references, classify each as a likely producer or consumer (from the
    symbols co-occurring in its chunk — Publish/Marshal vs Subscribe/Unmarshal —
    see event_flow.classify_site), and group by repo. The result is an
    approximate producer→consumer map across the service boundary the static
    graph otherwise can't cross. Roles are a heuristic (no source text runner-
    side); the output tells the agent to confirm at each site.
    """
    from . import event_flow as ef

    idx = _open_index()
    if idx is None:
        return _render_unavailable()
    rec = ef.load_recognizers()
    try:
        refs = idx.find_references(
            message_type, limit=EVENT_FLOW_REF_LIMIT, include_definition=True,
        )
        # Enrich each site with the symbols sharing its chunk (the classifier's
        # primary signal). One cheap query per unique (file, line).
        sites: list[tuple[str, str, str]] = []  # (repo, role, "file:line")
        seen_chunk: dict[tuple[str, int], list[str]] = {}
        for _m, loc in refs:
            key = (loc.file, loc.start_line)
            if key not in seen_chunk:
                co = idx.get_symbol_at(loc.file, loc.start_line)
                seen_chunk[key] = [c.display_name for c in co]
            role = ef.classify_site(loc.file, seen_chunk[key], rec)
            sites.append((ef.repo_of(loc.file), role, loc.render()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph_find_event_flow failed: %s", exc)
        return {"error": f"code graph query failed: {exc}"}
    finally:
        idx.close()

    if not sites:
        return _render_no_matches("event-flow reference", message_type)

    # Group producers and consumers by repo; definitions/unknowns listed after.
    producers: dict[str, list[str]] = {}
    consumers: dict[str, list[str]] = {}
    other: dict[str, list[str]] = {}
    for repo, role, loc in sites:
        bucket = {ef.PRODUCER: producers, ef.CONSUMER: consumers}.get(role, other)
        bucket.setdefault(repo, []).append(loc)

    blocks: list[str] = [f"# event flow for {message_type!r} (shared message type)"]
    prod_repos = sorted(producers)
    cons_repos = sorted(consumers)
    if prod_repos and cons_repos:
        blocks.append(
            f"cross-service flow: {', '.join(prod_repos)} → {', '.join(cons_repos)}"
        )
    blocks.append("")
    blocks.append("## producers (construct / publish sites)")
    blocks.extend(_render_role_group(producers))
    blocks.append("")
    blocks.append("## consumers (subscribe / unmarshal sites)")
    blocks.extend(_render_role_group(consumers))
    if other:
        blocks.append("")
        blocks.append("## other references (definition / role unclear — confirm at site)")
        blocks.extend(_render_role_group(other))
    blocks.append("")
    blocks.append(
        "NOTE: roles are inferred from co-located symbols, not source — confirm "
        "producer/consumer at each site. The publish/subscribe SUBJECT itself is "
        "not a code edge and is not resolved here; this links repos via the "
        "shared message type. Tune recognizers with STRIX_EVENT_RECOGNIZERS."
    )
    if len(refs) >= EVENT_FLOW_REF_LIMIT:
        blocks.append(f"… references capped at {EVENT_FLOW_REF_LIMIT}.")
    return {"output": "\n".join(blocks)}


def _render_role_group(by_repo: dict[str, list[str]]) -> list[str]:
    if not by_repo:
        return ["  (none identified)"]
    out: list[str] = []
    for repo in sorted(by_repo):
        out.append(f"  {repo}:")
        out.extend(f"    {loc}" for loc in by_repo[repo])
    return out


@function_tool(strict_mode=False)
async def code_graph_find_definition(ctx: RunContextWrapper, symbol: str) -> dict[str, Any]:
    """Return file:line locations where ``symbol`` is defined in the indexed tree."""
    return _do_find_definition(symbol)


@function_tool(strict_mode=False)
async def code_graph_find_references(
    ctx: RunContextWrapper, symbol: str, include_definition: bool = False,
) -> dict[str, Any]:
    """Return file:line locations where ``symbol`` is referenced. The definition
    site is excluded unless ``include_definition=True``."""
    return _do_find_references(symbol, include_definition)


@function_tool(strict_mode=False)
async def code_graph_find_implementations(ctx: RunContextWrapper, interface: str) -> dict[str, Any]:
    """Return implementations / subtypes of ``interface`` (from SCIP Relationship
    records flagged is_implementation)."""
    return _do_find_implementations(interface)


@function_tool(strict_mode=False)
async def code_graph_list_symbols(ctx: RunContextWrapper, scope: str) -> dict[str, Any]:
    """List symbols defined under a file or directory path. ``scope`` is a file
    path (``src/auth/mw.ts``) or a directory prefix (``src/auth/``)."""
    return _do_list_symbols(scope)


@function_tool(strict_mode=False)
async def code_graph_get_imports(ctx: RunContextWrapper, file: str) -> dict[str, Any]:
    """Return the global symbols referenced from ``file`` — a superset of imports
    that answers "what does this file depend on"."""
    return _do_get_imports(file)


@function_tool(strict_mode=False)
async def code_graph_get_symbol_at(ctx: RunContextWrapper, file: str, line: int) -> dict[str, Any]:
    """Return the symbols mentioned in the chunk containing ``line`` in ``file``."""
    return _do_get_symbol_at(file, line)


@function_tool(strict_mode=False)
async def code_graph_grep(
    ctx: RunContextWrapper, symbol: str, context: int = GREP_DEFAULT_CONTEXT,
) -> dict[str, Any]:
    """Resolve ``symbol`` precisely via the code graph, then show its source: the
    definition's body plus each reference site with surrounding context. One call
    replaces find_references + a fan-out of read_file, with zero false hits from
    comments, strings, vendored deps, or unrelated same-named symbols.

    ``context`` is the number of lines shown above/below each reference."""
    return _do_grep(symbol, context)


@function_tool(strict_mode=False)
async def code_graph_find_event_flow(ctx: RunContextWrapper, message_type: str) -> dict[str, Any]:
    """Trace a message-bus event across service boundaries by its shared TYPE.

    A NATS/Kafka/pub-sub hop has no code edge — the producer and consumer are
    linked only by a runtime subject/topic string — so plain reference search
    can't follow it. But when both sides share a statically-typed message
    contract (a type defined in a common library, e.g. a protobuf message), that
    TYPE is a code symbol the graph resolves across every repo. Give this tool
    the message type and it returns an approximate PRODUCER → CONSUMER map across
    repos: references grouped by repo and classified by the symbols co-located at
    each site (publish/marshal → producer; subscribe/unmarshal → consumer).

    Use it when you suspect a cross-service event flow (service A emits an event
    service B acts on) and want the two ends the static call graph can't connect.
    Roles are heuristic — confirm at each site. It links via the message type,
    NOT the subject string (subjects are usually runtime values, not code). Works
    for any bus given the right recognizer tokens (STRIX_EVENT_RECOGNIZERS); the
    envelope is: a shared, statically-typed message contract crosses the boundary.
    """
    return _do_find_event_flow(message_type)


ALL_TOOLS = (
    code_graph_find_definition,
    code_graph_find_references,
    code_graph_find_implementations,
    code_graph_list_symbols,
    code_graph_get_imports,
    code_graph_get_symbol_at,
    code_graph_grep,
    code_graph_find_event_flow,
)
