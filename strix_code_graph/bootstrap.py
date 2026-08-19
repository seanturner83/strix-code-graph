"""Addon entry point: register the code-graph tools and arrange for the SCIP
index to be built once per scan.

Design (see project scope): the addon touches Strix core through published
extension seams only.

  1. Tools — registered via ``strix.agents.factory.register_agent_tools``, the
     same public hook ``register_backend`` uses. Zero core patch.

  2. Index build — the toolchain, the target source, and therefore the built
     SQLite index all live INSIDE the sandbox, but v1.x runs ``@function_tool``
     tools RUNNER-side. So the index is built in-sandbox (``session.exec`` the
     indexer), then the single SQLite file is copied OUT to a runner-local dir
     (``session.read``), where the query tools open it locally.

     WHEN the build runs is resolved two ways, most-preferred first:
       (a) HOOK — if a future Strix exposes a post-session-ready callback
           (``register_session_setup``), we register the build into it so the
           index is ready before the agent's first turn.
       (b) LAZY — otherwise the index is built on the first code-graph tool
           call, guarded so it runs at most once per session.

Enable with ``STRIX_CODE_GRAPH=1``. Off by default — importing the addon does
nothing until ``register()`` is called and the flag is set, so core Strix is
untouched unless a deployment opts in (and ships the SCIP sandbox image).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from . import tools

# Strix's own logging setup (strix/telemetry/logging.py setup_scan_logging)
# only attaches its file/stream handlers to loggers named "strix" or
# "openai.agents" (and their children) -- a third-party logger name like the
# bare module __name__ ("strix_code_graph.bootstrap") is a SEPARATE
# top-level namespace, never reached. Live-observed: every logger.info/
# warning call in this addon was silently swallowed during a real Strix
# scan (nothing in strix.log, nothing on stdout) -- Python's logging
# "handler of last resort" only surfaces WARNING+ to stderr when no
# handler is attached anywhere in a logger's propagation chain, and INFO
# never reaches even that. Renaming to a "strix."-prefixed child makes it
# propagate INTO Strix's own "strix" logger (which has the real handlers),
# without this addon needing to configure any handler itself. Every module
# in this package does the same __name__ -> "strix." + __name__ mapping.
logger = logging.getLogger(__name__.replace("strix_code_graph", "strix.code_graph", 1))

# Where the sandbox-built SQLite index is copied out to on the runner. The
# query tools read this via query.CodeGraphIndex.discover().
#
# Consumers set STRIX_CODE_GRAPH_PERSIST_DIR (the strix-scan pipeline's existing,
# untouched contract — its harvest step relocates <dir>/target/ to the run's
# code_graph/ for S3 + the enrich step). STRIX_CODE_GRAPH_DIR is accepted as an
# alias for standalone/local use. First set wins; else a private tempdir.
_ENV_DIRS = ("STRIX_CODE_GRAPH_PERSIST_DIR", "STRIX_CODE_GRAPH_DIR")
_ENV_DIR = _ENV_DIRS[0]  # canonical name we (re-)export for the query tools
# In-sandbox path the indexer writes to. MUST live under the workspace root:
# the SDK's session.read() only permits reads inside /workspace
# (InvalidManifestPathError otherwise), and copy-out reads the built sqlite back
# through it. Hidden dir so it doesn't clutter the target tree the agent sees.
_SANDBOX_INDEX_DIR = "/workspace/.strix-code-graph"
_SANDBOX_SQLITE = f"{_SANDBOX_INDEX_DIR}/code_graph.sqlite"


def enabled() -> bool:
    """True unless the addon is explicitly switched OFF (``STRIX_CODE_GRAPH``
    falsy). Default-ON: this addon ships only in images built to run it, and its
    sole consumers are our own scan pipelines — an opt-OUT is the right default
    for us, and it removes the need to thread an enable flag through every
    caller. Set STRIX_CODE_GRAPH=0/false/no/off to disable (e.g. local dev)."""
    value = os.environ.get("STRIX_CODE_GRAPH", "1").strip().lower()
    return value not in {"0", "false", "no", "off", ""}


def register() -> bool:
    """Register the code-graph tools with Strix and wire the index build.

    Idempotent and safe to call unconditionally at startup: returns False
    (a no-op) when the addon is disabled or the host Strix lacks the tool
    registration hook. Returns True when the tools were registered.
    """
    if not enabled():
        logger.debug("strix-code-graph: disabled (set STRIX_CODE_GRAPH=1 to enable)")
        return False

    try:
        from strix.agents.factory import register_agent_tools
    except Exception as exc:  # noqa: BLE001 — host without the hook: degrade, don't crash
        logger.warning(
            "strix-code-graph: host Strix has no register_agent_tools (%s); skipping", exc,
        )
        return False

    register_agent_tools(*tools.ALL_TOOLS)
    logger.info("strix-code-graph: registered %d code-graph tools", len(tools.ALL_TOOLS))
    _wire_index_build()
    return True


# --- index build wiring ------------------------------------------------------


def _wire_index_build() -> None:
    """Register the index build into the host's post-session-ready hook.

    The build needs the sandbox session (to exec the indexer in-container and
    copy the SQLite out), and only the runner's scan loop holds it — a
    runner-side ``@function_tool`` gets a ``RunContextWrapper``, not the
    session. So there is no in-tool "lazy build" path: without a session-setup
    hook the addon simply builds no index and the tools report "unavailable"
    (a clean degrade). ``register_session_setup`` is a small, separate upstream
    hook (proposed alongside this addon) that mirrors ``register_backend`` /
    ``register_agent_tools``.
    """
    try:
        from strix.runtime.session_manager import register_session_setup
    except Exception:  # noqa: BLE001 — hook not present on this Strix
        logger.warning(
            "strix-code-graph: host Strix has no register_session_setup hook — tools "
            "are registered but no index will be built (they will report unavailable). "
            "Upgrade to a Strix that exposes the hook to enable indexing.",
        )
        return

    register_session_setup(_on_session_ready)
    logger.info("strix-code-graph: registered session-setup hook for index build")


async def _on_session_ready(session: Any, scan_config: dict[str, Any] | None = None) -> None:
    """Hook callback: build + copy out the index right after the sandbox is
    ready, before the agent's first turn."""
    await _build_and_copy_out(session, _target_subdirs(scan_config))


def _target_subdirs(scan_config: dict[str, Any] | None) -> list[str]:
    """Workspace subdirs to index, one per target.

    A Strix scan materialises EVERY target into the SAME sandbox at
    ``/workspace/<workspace_subdir>`` (bare ``/workspace`` for a single
    subdir-less target) — targets are not dispatched separately. So we index
    each target subtree independently (its own keyed index, matching the
    fork's per-target hook) rather than indexing ``/workspace`` as one blob,
    which would scramble cross-repo relative paths. Returns subdir names
    (``""`` = bare ``/workspace``); de-duplicated, order-preserving.
    """
    targets = (scan_config or {}).get("targets") or []
    subdirs: list[str] = []
    for t in targets:
        # Only code targets have an indexable tree; URLs/IPs don't.
        if t.get("type") not in {"local_code", "repository"}:
            continue
        sub = (t.get("details") or {}).get("workspace_subdir") or ""
        if sub not in subdirs:
            subdirs.append(sub)
    return subdirs or [""]  # fall back to bare /workspace


def _resolve_out_dir() -> Path:
    """The runner-local dir the query tools read from. Resolves from the
    first env name a consumer set (STRIX_CODE_GRAPH_PERSIST_DIR — the
    pipeline's contract — then the _DIR alias), else a private tempdir.
    Re-exports ALL names so the query tools (and any downstream) discover
    the same dir regardless of which they read."""
    configured = next((os.environ[n] for n in _ENV_DIRS if os.environ.get(n)), None)
    out_dir = Path(configured or tempfile.mkdtemp(prefix="strix-code-graph-"))
    for n in _ENV_DIRS:
        os.environ[n] = str(out_dir)
    return out_dir


def _corpus_graph_path() -> Path | None:
    """The pre-fetched, corpus-wide merged index the wrapping CI workflow
    downloaded to a local path, if configured and present. See
    _adopt_corpus_graph_wholesale's docstring for why this addon never
    fetches it itself (stays AWS-free, same posture as cache.py)."""
    corpus_path = os.environ.get("STRIX_CORPUS_GRAPH_PATH", "").strip()
    if not corpus_path:
        return None
    p = Path(corpus_path)
    return p if p.is_file() else None


def _adopt_corpus_graph_wholesale() -> bool:
    """If a pre-built, corpus-wide merged index is available, adopt it
    DIRECTLY as this session's whole code graph instead of building one
    from scratch inside the sandbox. Returns True if adopted (the caller
    skips its own build entirely); False to fall through to the local
    per-target build (no corpus graph configured, or adoption failed).

    Why: a domain-scan's own local build loops over EVERY mounted target —
    30+ domain/context repos PLUS the ~900-repo corpus-grep tree, which the
    per-target indexer also treats as a scan target. Live-observed: this
    consistently exceeds the 30-minute build timeout and degrades to
    "unavailable" in EVERY domain-scan run observed (connect, connamara),
    regardless of whether a corpus-wide graph was ALSO being merged in on
    top of it — the local build itself never actually completes. The
    corpus-wide graph (build_corpus_graph.py, refreshed on its own
    schedule) already covers every one of those repos; rebuilding them
    here, inside a live, credential-boundary-capped scan session, is
    redundant work that was never completing anyway. A plain local file
    copy — no sandbox interaction at all — which also skips the toolchain-
    install disk cost (~5GB measured: Go/Rust/Node/Terraform/buf, purely
    to index repos the corpus-wide graph already has).
    """
    corpus_sqlite = _corpus_graph_path()
    if corpus_sqlite is None:
        return False
    try:
        out_dir = _resolve_out_dir()
        (out_dir / "target").mkdir(parents=True, exist_ok=True)
        final_sqlite = out_dir / "target" / "code_graph.sqlite"
        shutil.copyfile(corpus_sqlite, final_sqlite)
    except Exception as exc:  # noqa: BLE001 — fall through to local build on any failure
        logger.warning(
            "strix-code-graph: corpus-wide graph adoption failed (%s); "
            "falling back to local per-target build", exc,
        )
        return False
    else:
        logger.info(
            "strix-code-graph: adopted corpus-wide graph wholesale (%s, %d bytes) "
            "at %s -- skipping local per-target build entirely",
            corpus_sqlite, final_sqlite.stat().st_size, final_sqlite,
        )
        return True


async def _build_and_copy_out(session: Any, subdirs: list[str]) -> None:
    """Build one SCIP index per target subtree inside the sandbox, then copy
    each SQLite file out to a runner-local dir the query tools can open. Never
    raises — a code-graph build failure must degrade to "unavailable", not
    break the scan. A single target's failure doesn't stop the others.

    Skips ALL of this — no sandbox interaction, no toolchain installs — when
    a corpus-wide graph is available; see _adopt_corpus_graph_wholesale.
    """
    if _adopt_corpus_graph_wholesale():
        return

    ws_root = os.environ.get("STRIX_WORKSPACE_ROOT", "/workspace")
    langs = os.environ.get("STRIX_CODE_GRAPH_LANGS", "").strip()
    # In-sandbox path of a warm Go module cache for offline scip-go resolution
    # of private deps (see indexer._index_go). Threaded as a CLI arg because
    # session.exec has no env= kwarg. Absent = network-free repos only.
    gomodcache = os.environ.get("STRIX_GO_MODCACHE", "").strip()

    # Build one index per target subtree, then merge into a SINGLE DB
    # (merge_sqlite_indexes prefixes each target's paths so repos never collide;
    # one target → straight copy). Every sandbox command is an ARGV list via
    # session.exec(..., shell=False) — no shell string, no nested `python3 -c`,
    # so a target label with a quote or shell metachar can't break or inject.
    try:
        merge_argv: list[str] = ["python3", "-m", "strix_code_graph.merge", _SANDBOX_SQLITE]
        any_built = False
        for sub in subdirs:
            key = sub.replace("/", "_") or "_root"
            target_path = f"{ws_root}/{sub}" if sub else ws_root
            sandbox_dir = f"{_SANDBOX_INDEX_DIR}/{key}"
            sandbox_sqlite = f"{sandbox_dir}/code_graph.sqlite"
            indexer_argv = [
                "python3", "-m", "strix_code_graph.indexer",
                "--target", target_path, "--out-dir", sandbox_dir,
            ]
            if langs:
                indexer_argv += ["--langs", langs]
            if gomodcache:
                indexer_argv += ["--gomodcache", gomodcache]
            await session.exec(*indexer_argv, shell=False, timeout=1800)
            # The indexer exits 0 even on "nothing to index"; success = the
            # sqlite appeared. Probe with a plain argv test.
            probe = await session.exec("test", "-f", sandbox_sqlite, shell=False, timeout=30)
            if getattr(probe, "exit_code", 1) == 0:
                merge_argv += [sub, sandbox_sqlite]  # sub is "" for bare /workspace
                any_built = True
            else:
                logger.info(
                    "strix-code-graph: no index for %r (unsupported language / empty tree)",
                    target_path,
                )

        if not any_built:
            logger.info("strix-code-graph: no target produced an index — tools report unavailable")
            return

        await session.exec(*merge_argv, shell=False, timeout=300)
        probe = await session.exec("test", "-f", _SANDBOX_SQLITE, shell=False, timeout=30)
        if getattr(probe, "exit_code", 1) != 0:
            logger.warning("strix-code-graph: merge produced no index; unavailable")
            return

        out_dir = _resolve_out_dir()
        # Write to <dir>/target/ — the layout the strix-scan pipeline's harvest
        # + enrich steps expect (code_graph/target/code_graph.sqlite), matching
        # the prior fork-carried indexer. (Was <dir>/index/, which no consumer
        # looked in → index never persisted.)
        (out_dir / "target").mkdir(parents=True, exist_ok=True)
        data = await _read_all(session, _SANDBOX_SQLITE)
        if data is None:
            logger.warning("strix-code-graph: merged index built but copy-out failed; unavailable")
            return
        final_sqlite = out_dir / "target" / "code_graph.sqlite"
        final_sqlite.write_bytes(data)
        logger.info(
            "strix-code-graph: unified index ready (%d target(s), %d bytes) at %s",
            len(subdirs), len(data), final_sqlite,
        )
        _maybe_merge_corpus_graph(final_sqlite)
    except Exception as exc:  # noqa: BLE001 — never let code-graph break a scan
        logger.warning("strix-code-graph: index build/merge/copy-out failed (%s); unavailable", exc)


def _maybe_merge_corpus_graph(session_sqlite: Path) -> None:
    """If STRIX_CORPUS_GRAPH_PATH points at a pre-fetched, corpus-wide merged
    index, UNION it into this session's own fresh graph in place — so
    find_definition/find_references resolve cross-repo edges against the
    WHOLE org corpus, not just this scan's own target(s).

    A separate, standalone job (seedcx/strix-scan-workflow's
    build_corpus_graph.py) builds and publishes that index to S3; this addon
    stays AWS-free (same posture as cache.py's own docstring) by never
    fetching it itself -- the wrapping CI workflow downloads it to a local
    path BEFORE invoking Strix and sets this env var, exactly the same
    runner-local-path convention STRIX_CODE_GRAPH_PERSIST_DIR already uses.

    Best-effort: unset env, a missing file, or a merge failure all degrade
    to "use the session-only graph" -- never breaks the scan.
    """
    corpus_path = os.environ.get("STRIX_CORPUS_GRAPH_PATH", "").strip()
    if not corpus_path:
        return
    corpus_sqlite = Path(corpus_path)
    if not corpus_sqlite.is_file():
        logger.info(
            "strix-code-graph: STRIX_CORPUS_GRAPH_PATH set but no file at %s; "
            "using session-only index", corpus_sqlite,
        )
        return
    try:
        from .indexer import merge_sqlite_indexes

        # "" keeps the session's own graph's paths bare (unprefixed, exactly
        # as they already are); "corpus" prefixes the pre-built graph's
        # paths so they never collide with the session's own repo(s).
        merged = session_sqlite.with_name("code_graph.with-corpus.sqlite")
        merge_sqlite_indexes([("", session_sqlite), ("corpus", corpus_sqlite)], merged)
        merged.replace(session_sqlite)
        logger.info(
            "strix-code-graph: merged corpus-wide graph (%s) into session index",
            corpus_sqlite,
        )
    except Exception as exc:  # noqa: BLE001 — never break the scan over this
        logger.warning(
            "strix-code-graph: corpus-graph merge failed (%s); using session-only index", exc,
        )


async def _read_all(session: Any, remote_path: str) -> bytes | None:
    """Read a file out of the sandbox via the SDK session.read() primitive."""
    try:
        handle = await session.read(Path(remote_path))
    except Exception as exc:  # noqa: BLE001
        logger.debug("strix-code-graph: session.read(%s) failed: %s", remote_path, exc)
        return None
    try:
        raw = handle.read()
    finally:
        with _suppress():
            handle.close()
    return raw if isinstance(raw, bytes | bytearray) else bytes(raw)


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True
