"""SCIP indexer + SQLite loader for the Strix code-graph tools.

W1 scope: build the SCIP index for a target repo, convert it to SQLite via
the scip CLI, return the SQLite path. The actual graph-query tools are
registered in W2.

Indexer selection (W1):
    - TypeScript / JavaScript: scip-typescript (npm @sourcegraph/scip-typescript
      pinned to 0.4.0 in containers/Dockerfile).
    - Go: scip-go (github.com/scip-code/scip-go pinned to v0.2.7).
    - Python, Java: deferred to W5.

Local smoke validation (2026-06-04):
    - portal-api    (TypeScript): ~500ms, 3.25 MB SCIP, 5267 symbols.
    - payment-orchestrator (Go): ~32s,   8.9  MB SCIP, 5197 symbols.

Multi-language repos run both indexers; the SQLite loader merges them so a
single .sqlite handle answers cross-language queries.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cache import CacheKey, CodeGraphCache
from .cache import from_env as _cache_from_env

logger = logging.getLogger(__name__)


class IndexerError(RuntimeError):
    """Raised when an indexer tool fails or is missing from the sandbox."""


# APPSEC-1396: the sandbox env carries real secrets forwarded in for
# legitimate reasons — NPM_TOKEN/NODE_AUTH_TOKEN for private npm scopes,
# CARGO_REGISTRIES_*_TOKEN for private crates, TOOL_SERVER_TOKEN for the
# in-sandbox tool-execution API. The install/build steps below
# (_index_python's uv/pip, _index_rust's rust-analyzer-scip metadata pass,
# _index_typescript's npm install) execute the TARGET repo's own build
# backend — setup.py, build.rs/proc-macros, npm lifecycle scripts — and
# Strix's whole premise is that target is potentially attacker-controlled.
# Confirmed dynamically, both via the ticket's own PoC and live re-verification
# (2026-08-12): a planted setup.py read NPM_TOKEN/TOOL_SERVER_TOKEN straight
# out of os.environ during `pip install -e .`; a planted build.rs did the same
# during `rust-analyzer scip` — no exploit needed beyond "the value is
# sitting in the env".
#
# Fix: scrub by NAME PATTERN, not an enumerated list. An enumerated list
# (matching just the vars named in the ticket) rots the moment someone adds
# a new credential to the sandbox env — it protects against last quarter's
# secrets, not next quarter's. Substring-matching on credential-shaped names
# fails safe: a false positive just costs an installer a var it likely didn't
# need for public-registry resolution anyway; a false negative is the
# actual danger. Deliberately NOT included: GOPROXY/GOPRIVATE/GOSUMDB —
# these are proxy URLs/hostname-prefix config the Go leg (_index_go) uses,
# not credentials, and _index_go type-checks via go/packages rather than
# executing the target's own build code, so they're outside this threat
# model. If that changes (e.g. Go gets an install step that runs target
# code), revisit.
#
# _URL/_URI/_DSN/WEBHOOK cover the other common credential shape: a secret
# embedded in a connection string (postgres://user:pass@host, a webhook URL
# with a signing token in the path) rather than a bare token value. KEY
# (not just _KEY) also catches APIKEY/SSHKEY/KEYSTORE-style names without an
# underscore separator — checked against the sandbox's actual non-credential
# vars (PATH, HOME, JAVA_HOME, GRADLE_USER_HOME, MAVEN_OPTS, GOPROXY,
# http_proxy/https_proxy) to confirm none of them collide.
_SECRET_ENV_PATTERNS = (
    "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "KEY", "AUTH",
    "_URL", "_URI", "_DSN", "WEBHOOK",
)


def _scrubbed_env() -> dict[str, str]:
    """Copy of os.environ with credential-shaped vars removed — pass as the
    base env to any subprocess that runs the TARGET repo's own build/install
    code. PATH/HOME/etc are untouched; only names matching a credential
    pattern (case-insensitive) are dropped."""
    return {
        k: v
        for k, v in os.environ.items()
        if not any(pat in k.upper() for pat in _SECRET_ENV_PATTERNS)
    }


@dataclass(frozen=True)
class IndexResult:
    target_dir: Path
    scip_paths: tuple[Path, ...]
    sqlite_path: Path


def _binary_exists(name: str) -> bool:
    return shutil.which(name) is not None


def _has_files_matching(target: Path, *patterns: str) -> bool:
    for pattern in patterns:
        if next(target.rglob(pattern), None) is not None:
            return True
    return False


def _run(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = 600,
    env: dict[str, str] | None = None,
    base_env: dict[str, str] | None = None,
) -> None:
    """base_env, when given, REPLACES os.environ as the base (env, if also
    given, still layers on top) — used to run a command against the
    _scrubbed_env() base rather than the full environment. env alone stays
    additive-onto-os.environ, unchanged for every existing caller."""
    logger.info("code_graph: running %s (cwd=%s)", " ".join(cmd), cwd)
    run_env = None
    if base_env is not None:
        run_env = {**base_env, **(env or {})}
    elif env:
        run_env = {**os.environ, **env}
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=run_env,
    )
    if proc.returncode != 0:
        raise IndexerError(
            f"command {cmd!r} failed (rc={proc.returncode}): {proc.stderr[:500]}"
        )


def _ensure_node_version(target: Path) -> str | None:
    """Resolve the Node version pinned by package.json's `engines.node` and
    install it into /tmp on demand if the sandbox's default node doesn't
    match. Returns the absolute path to the resolved node bin dir (suitable
    for prepending to PATH), or None if no version pin is found or current
    node already satisfies it.

    Background: many TS repos pin engines.node to a specific major (e.g.
    "22.15.0"). npm install fails with EBADENGINE if the sandbox's bundled
    node is older. Rather than baking every required node version into the
    Strix sandbox image, fetch the requested version from nodejs.org on
    demand and stage it under /tmp.
    """
    pkg = target / "package.json"
    if not pkg.exists():
        return None
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("code_graph: package.json unparseable (%s); skipping node pin", exc)
        return None
    desired_raw = (data.get("engines") or {}).get("node", "")
    if not isinstance(desired_raw, str):
        return None
    # Strip semver-range prefixes / whitespace ("^22", "~22.15", ">=22.15.0").
    # For simple ranges we pick the lower bound; for an exact pin we use as-is.
    # Multi-clause ranges ("^22 || ^20") fall through to "use whatever exists";
    # nodejs.org doesn't serve range-resolution.
    match = re.match(r'^\s*[\^~>=<]*\s*(\d+(?:\.\d+){0,2})', desired_raw)
    if not match:
        return None
    desired = match.group(1)
    # Pad to full M.m.p — nodejs.org only serves complete tarball names.
    parts = desired.split(".")
    if len(parts) == 1:
        # "22" → resolve to a known-LTS minor.patch. For simplicity pin to
        # the latest *known stable* for the major. Default to .0.0 and let
        # nodejs.org redirect; if that doesn't exist we'll fall through.
        desired = f"{parts[0]}.0.0"
    elif len(parts) == 2:
        desired = f"{desired}.0"

    # Skip download if the system node already matches.
    try:
        rc = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
        if rc.returncode == 0 and rc.stdout.strip().lstrip("v") == desired:
            return None  # system node already matches; nothing to do
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    node_dir = Path(f"/tmp/node-v{desired}-linux-x64")
    if node_dir.exists() and (node_dir / "bin" / "node").exists():
        logger.info("code_graph: using cached node v%s at %s", desired, node_dir)
        return str(node_dir / "bin")

    url = f"https://nodejs.org/dist/v{desired}/node-v{desired}-linux-x64.tar.xz"
    logger.info("code_graph: fetching node v%s from nodejs.org", desired)
    try:
        _run(
            ["sh", "-c", f"curl -fLsS {url} | tar -xJ -C /tmp/"],
            timeout=180,
        )
    except IndexerError as exc:
        logger.warning(
            "code_graph: node v%s install failed (%s); falling back to system node",
            desired,
            exc,
        )
        return None
    if not (node_dir / "bin" / "node").exists():
        logger.warning(
            "code_graph: node v%s tarball extracted but no node binary at %s",
            desired,
            node_dir / "bin" / "node",
        )
        return None
    return str(node_dir / "bin")


def _index_typescript(target: Path, out_dir: Path) -> Path | None:
    if not _has_files_matching(target, "tsconfig.json", "package.json"):
        return None
    if not _binary_exists("scip-typescript"):
        raise IndexerError("scip-typescript missing from sandbox")
    # scip-typescript invokes the TypeScript compiler under the hood,
    # which refuses to proceed if `tsconfig.json` has an extends chain
    # it can't resolve. The common idiom of extending a package config
    # (e.g. `"extends": "@some-org/tsconfig-node"`) requires that
    # package to be present under node_modules — relative-path extends
    # (e.g. `"./tsconfig.base.json"`) don't.
    #
    # Most CI checkouts don't run `npm install`, so package-named
    # extends are unresolvable at index time and scip-typescript
    # fails with "error TS6053: File '<pkg>' not found" → no SCIP
    # produced.
    #
    # Install deps minimally to make the compiler happy: skip lifecycle
    # scripts + audit + funding for speed, use prefer-offline so repeat
    # scans of the same repo hit the npm cache warm. Cleanup of
    # node_modules + package-lock.json happens in the docker_runtime
    # hook after the indexer returns, so downstream tools (agent
    # loop, etc.) never see the installed deps.
    if not (target / "node_modules").exists() and (target / "package.json").exists():
        # Fallback chain for npm install:
        #   1. Try to match Node version from package.json engines.node.
        #      Many repos pin a specific node (e.g. "22.15.0") and npm
        #      refuses install with EBADENGINE on mismatch.
        #   2. If install still fails, retry with --engine-strict=false
        #      to bypass the engine check entirely.
        #   3. If THAT also fails, log + proceed without deps;
        #      scip-typescript will run on the bare tree and produce
        #      partial output (or fail; indexer module exits 0 either
        #      way per the warn-and-continue policy).
        node_bin = _ensure_node_version(target)
        base_args = [
            "npm",
            "install",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--prefer-offline",
        ]
        # If we resolved a node bin, wrap cmd in sh -c to prepend its
        # bin dir to PATH for the subprocess (subprocess.run env doesn't
        # shell-expand $PATH).
        if node_bin:
            wrapped = (
                f"export PATH={node_bin}:$PATH; "
                + " ".join(base_args)
            )
            install_cmd = ["sh", "-c", wrapped]
        else:
            install_cmd = base_args
        # APPSEC-1396: --ignore-scripts already blocks npm's main RCE vector
        # (pre/postinstall lifecycle scripts), so this isn't currently an
        # open code-execution path the way Python/Rust's installs are. Scrub
        # anyway for defense-in-depth — a future change dropping
        # --ignore-scripts, or an npm bug, shouldn't silently reopen
        # credential exposure here too.
        scrubbed = _scrubbed_env()
        try:
            _run(install_cmd, cwd=target, timeout=300, base_env=scrubbed)
        except IndexerError as exc:
            logger.warning(
                "code_graph: npm install (engine-strict default) failed (%s); "
                "retrying with --engine-strict=false",
                exc,
            )
            fallback_args = base_args + ["--engine-strict=false"]
            if node_bin:
                fallback_cmd = [
                    "sh",
                    "-c",
                    f"export PATH={node_bin}:$PATH; " + " ".join(fallback_args),
                ]
            else:
                fallback_cmd = fallback_args
            try:
                _run(fallback_cmd, cwd=target, timeout=300, base_env=scrubbed)
            except IndexerError as exc2:
                logger.warning(
                    "code_graph: npm install fallback (--engine-strict=false) also "
                    "failed (%s); indexing without deps",
                    exc2,
                )
    out = out_dir / "ts.scip"
    _run(["scip-typescript", "index", "--output", str(out)], cwd=target)
    return out if out.exists() else None


def _index_go(target: Path, out_dir: Path) -> Path | None:
    if not _has_files_matching(target, "go.mod"):
        return None
    if not _binary_exists("scip-go"):
        raise IndexerError("scip-go missing from sandbox")
    out = out_dir / "go.scip"
    # GOTOOLCHAIN=local forces Go to use the toolchain baked into the
    # sandbox instead of honouring the target go.mod's `go 1.x.y` pin,
    # which triggers an on-demand toolchain DOWNLOAD. That download fails
    # in our sandbox because GOSUMDB=off makes Go refuse to verify the
    # toolchain module's checksum ("checksum database disabled by
    # GOSUMDB=off") — so the whole SCIP index silently skips and every Go
    # finding lands location-less. The baked toolchain indexes fine
    # regardless of the repo's go-directive; scip-go only needs to parse +
    # type-check, not match the exact patch release. Observed on
    # global-policy-engine#10 (go.mod pinned go1.26.4) 2026-06-15.
    go_env = {"GOTOOLCHAIN": "local"}
    # Private-dep resolution: real fleet repos vendor nothing and pull dozens
    # of private github.com/<org>/* modules, but the sandbox is SEALED (no VCS
    # auth, no network). scip-go type-checks with go/packages, so unresolved
    # deps yield an EMPTY index. Fix: point GOMODCACHE at a warm module cache
    # pre-populated out-of-band (locally a bind-mount; on the fleet the same S3
    # warm cache the dependency-drain already maintains) and force fully
    # offline resolution so Go reads only from that cache. Opt-in via
    # STRIX_GO_MODCACHE (a sandbox path); absent = today's behaviour unchanged.
    modcache = os.environ.get("STRIX_GO_MODCACHE", "").strip()
    if modcache:
        go_env.update(
            {
                "GOMODCACHE": modcache,
                "GOFLAGS": "-mod=mod",
                "GOPROXY": "off",  # read only from the warm cache, never network
                "GOSUMDB": "off",
                "GOPRIVATE": os.environ.get("STRIX_GO_PRIVATE", "*"),
            }
        )
        logger.info("code_graph: scip-go offline via warm GOMODCACHE=%s", modcache)
    _run(["scip-go", "--output", str(out)], cwd=target, env=go_env)
    return out if out.exists() else None


def _index_python(target: Path, out_dir: Path) -> Path | None:
    """SCIP index for Python projects via @sourcegraph/scip-python.

    Detection: pyproject.toml / setup.py / setup.cfg / requirements.txt.
    Dependency install: scip-python uses Pyright under the hood; for
    accurate import resolution we install deps when an obvious
    install path exists (uv.lock → uv, pyproject.toml → uv if
    available else pip-install -e, requirements.txt → pip install -r).
    If the install fails or the toolchain is missing we proceed
    without — scip-python emits a partial index rather than failing.
    Cleanup of .venv/ + venv/ + __pycache__/ happens in the
    docker_runtime hook.
    """
    py_markers = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
    if not _has_files_matching(target, *py_markers):
        return None
    if not _binary_exists("scip-python"):
        raise IndexerError("scip-python missing from sandbox")

    has_venv = (target / ".venv").exists() or (target / "venv").exists()
    has_pyproject = (target / "pyproject.toml").exists()
    has_requirements = (target / "requirements.txt").exists()

    if not has_venv:
        # APPSEC-1396: this install step runs the TARGET's own build backend
        # (setup.py / PEP517 hooks) — scrub credential-shaped env vars so a
        # malicious target can't read them, same as _index_rust/_index_typescript.
        scrubbed = _scrubbed_env()
        try:
            if has_pyproject and _binary_exists("uv"):
                # uv is 10-100x faster than pip; prefer it when present.
                _run(
                    ["uv", "sync", "--no-dev", "--frozen"],
                    cwd=target,
                    timeout=300,
                    base_env=scrubbed,
                )
            elif has_pyproject:
                _run(
                    ["pip", "install", "--no-deps", "-e", "."],
                    cwd=target,
                    timeout=300,
                    base_env=scrubbed,
                )
            elif has_requirements:
                _run(
                    ["pip", "install", "-r", "requirements.txt"],
                    cwd=target,
                    base_env=scrubbed,
                    timeout=300,
                )
        except IndexerError as exc:
            logger.warning(
                "code_graph: python deps install failed (%s); "
                "indexing without resolved imports",
                exc,
            )

    out = out_dir / "py.scip"
    _run(["scip-python", "index", "--output", str(out)], cwd=target)
    return out if out.exists() else None


def _cargo_home() -> Path:
    """Root for the Rust toolchain install (parent of .cargo/.rustup).

    Defaults to /home/pentester -- the sandbox image's user, matching this
    function's original hardcoded behaviour exactly (so the baked sandbox
    image's lazy-install path is completely unaffected). Override via
    CARGO_HOME (Rust's own standard env var for this, not a strix-code-
    graph invention) for hosts where that user doesn't exist -- e.g. a bare
    CI runner building a corpus-wide index standalone, outside any Strix
    sandbox session.
    """
    override = os.environ.get("CARGO_HOME", "").strip()
    if override:
        # CARGO_HOME conventionally points AT .cargo itself, not its parent
        # -- if the caller set it to .../.cargo, use its parent as the
        # toolchain root so RUSTUP_HOME lands as a .rustup sibling, not
        # nested inside .cargo.
        p = Path(override)
        return p.parent if p.name == ".cargo" else p
    return Path("/home/pentester")


# Guards the check-then-install below against concurrent callers -- a
# multi-repo indexing job (e.g. a standalone corpus-wide build) can run
# several Rust targets' indexing in parallel worker threads, and this
# function was originally written for strix-code-graph's own in-sandbox
# loop, which indexes targets strictly sequentially (no concurrency ever
# existed here before). A plain module-level threading.Lock is sufficient
# scope: every concurrent caller in this class of use is a thread in the
# SAME process, never a separate process.
_RUST_TOOLCHAIN_LOCK = threading.Lock()


def _ensure_rust_toolchain() -> str | None:
    """Lazy-install rustup + rust-analyzer component on first use.

    Rust toolchain is ~500MB which we don't want to bake into the
    sandbox image — most scans don't touch Rust. Install on demand
    into <_cargo_home()>/.cargo + .rustup; cached across scans in the
    same sandbox lifetime (or, for a standalone CARGO_HOME override,
    across runs of whatever process set it).

    Returns the bin dir containing rust-analyzer + cargo + rustc, or
    None if install fails (caller logs + degrades to no-index).
    """
    home = _cargo_home()
    cargo_bin = home / ".cargo" / "bin"
    if (cargo_bin / "rust-analyzer").exists():
        return str(cargo_bin)
    with _RUST_TOOLCHAIN_LOCK:
        # Re-check: another thread may have finished installing while this
        # one was waiting for the lock.
        if (cargo_bin / "rust-analyzer").exists():
            return str(cargo_bin)
        logger.info(
            "code_graph: lazy-installing rust toolchain for first Rust target (root=%s)", home,
        )
        install_env = {"CARGO_HOME": str(home / ".cargo"), "RUSTUP_HOME": str(home / ".rustup")}
        try:
            # rustup-init script: minimal profile (no docs/clippy/rustfmt),
            # stable channel, then add rust-analyzer component explicitly.
            _run(
                [
                    "sh",
                    "-c",
                    "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs "
                    "| sh -s -- -y --default-toolchain stable --profile minimal "
                    "--no-modify-path",
                ],
                timeout=600,
                env=install_env,
            )
            _run(
                [str(cargo_bin / "rustup"), "component", "add", "rust-analyzer"],
                timeout=300,
                env=install_env,
            )
        except IndexerError as exc:
            logger.warning("code_graph: rust toolchain lazy-install failed (%s)", exc)
            return None
        if not (cargo_bin / "rust-analyzer").exists():
            logger.warning(
                "code_graph: rustup install reported success but rust-analyzer "
                "not at %s; check toolchain",
                cargo_bin,
            )
            return None
        return str(cargo_bin)


def _index_rust(target: Path, out_dir: Path) -> Path | None:
    """SCIP index for Rust projects via rust-analyzer's built-in `scip`
    subcommand (rust-analyzer >= 2024.x ships SCIP support natively).

    Detection: Cargo.toml. rust-analyzer is lazy-installed on first
    use to keep the sandbox image lean — see _ensure_rust_toolchain.
    Cleanup of target/ happens in the docker_runtime hook.
    """
    if not _has_files_matching(target, "Cargo.toml"):
        return None
    cargo_bin = _ensure_rust_toolchain()
    if not cargo_bin:
        raise IndexerError("rust toolchain unavailable for indexing")

    rust_analyzer = f"{cargo_bin}/rust-analyzer"
    cargo = f"{cargo_bin}/cargo"

    # APPSEC-1396: `rust-analyzer scip` below runs the target's build.rs /
    # proc-macros as part of its own metadata pass — confirmed live
    # (2026-08-12): a crate with a build.rs that reads os.environ leaked the
    # var on an unscrubbed run, and stopped leaking once base_env=scrubbed
    # was applied to this same call. `cargo fetch` alone does NOT trigger
    # build.rs (verified: no execution) — it only downloads crate sources —
    # so it's scrubbed here purely for consistency/defense-in-depth, not
    # because it's independently exploitable.
    scrubbed = _scrubbed_env()

    # rust-analyzer/cargo are rustup PROXY binaries — they resolve the
    # active toolchain via CARGO_HOME/RUSTUP_HOME at RUN time, not just at
    # install time. _ensure_rust_toolchain's install only set these for its
    # OWN subprocess calls (env=, additive-only, never persisted into this
    # process's os.environ) — CARGO_HOME rides along in _scrubbed_env() for
    # free (any caller with it set as a real process env var, e.g. this
    # job's own step-level env, keeps it), but RUSTUP_HOME never did, so a
    # non-default _cargo_home() (anything but /home/pentester) left these
    # proxy calls resolving against rustup's own default $HOME/.rustup --
    # empty on a bare runner -- and failing with "Unknown binary
    # 'rust-analyzer' in official toolchain ...". Pass both explicitly,
    # layered on top of scrubbed via _run's env= (never a security concern:
    # these are directory paths, not secrets).
    home = _cargo_home()
    toolchain_env = {"CARGO_HOME": str(home / ".cargo"), "RUSTUP_HOME": str(home / ".rustup")}

    # Pre-fetch the crate graph. rust-analyzer's metadata pass would
    # otherwise stall on network; pulling crates explicitly with a
    # tight timeout fails fast on network issues without blocking
    # the indexer indefinitely.
    try:
        _run([cargo, "fetch"], cwd=target, timeout=300, base_env=scrubbed, env=toolchain_env)
    except IndexerError as exc:
        logger.warning(
            "code_graph: cargo fetch failed (%s); rust-analyzer may emit "
            "partial scip without dep resolution",
            exc,
        )

    out = out_dir / "rs.scip"
    _run(
        [rust_analyzer, "scip", str(target), "--output", str(out)],
        cwd=target,
        timeout=600,
        base_env=scrubbed,
        env=toolchain_env,
    )
    return out if out.exists() else None


def _index_java(target: Path, out_dir: Path) -> Path | None:
    """SCIP index for JVM projects via scip-java.

    Detection: a Maven (``pom.xml``) or Gradle (``build.gradle``/
    ``build.gradle.kts``) build file. scip-java drives the project's own build
    tool to resolve the classpath, then emits SCIP — so it needs the build tool
    on PATH and, for accurate cross-module resolution, network access to the
    dependency repositories. Covers Java and other JVM languages scip-java
    supports (Scala, Kotlin) when the build file is present.

    ``scip-java index`` auto-detects Maven vs Gradle. It can be heavy (a full
    dependency resolve + compile), so the timeout is generous; on failure we
    degrade like the other legs rather than aborting the whole build.
    """
    jvm_markers = ("pom.xml", "build.gradle", "build.gradle.kts")
    if not _has_files_matching(target, *jvm_markers):
        return None
    if not _binary_exists("scip-java"):
        raise IndexerError("scip-java missing from sandbox")
    out = out_dir / "java.scip"
    # APPSEC-1396: scip-java drives the TARGET's own Maven/Gradle build (see
    # docstring) — Maven plugins and Gradle build scripts execute arbitrary
    # code during that resolve/compile, same class of risk as _index_python's
    # pip/uv and _index_rust's rust-analyzer-scip pass. Missed in the initial
    # sweep (caught by push-review); scrub here too.
    #
    # scip-java writes index.scip into the cwd by default; pin --output so it
    # lands in out_dir alongside the other legs' artifacts.
    _run(
        ["scip-java", "index", "--output", str(out)],
        cwd=target,
        timeout=900,
        base_env=_scrubbed_env(),
    )
    return out if out.exists() else None


def _index_terraform(target: Path, out_dir: Path) -> Path | None:
    """SCIP index for Terraform/HCL via the scip_terraform bridge (drives
    terraform-ls). Detection: any *.tf. Returns None when there's no terraform
    or terraform-ls is unavailable (degrades cleanly — same posture as the other
    legs). Closes the tf-* code-graph gap: no upstream scip-terraform exists, so
    this bridges terraform-ls's definition/reference resolution into SCIP.
    Reference-level (not full semantic) — enough for the triager's reachability /
    guard grounding on IaC findings."""
    if not _has_files_matching(target, "*.tf"):
        return None
    if not _binary_exists("terraform-ls"):
        # Not a hard error: the sandbox may predate the terraform-ls install
        # (Dockerfile SEC-tf); degrade to no-tf-index rather than fail the run.
        logger.warning("code_graph: terraform-ls missing; skipping terraform index")
        return None
    try:
        # Import inside the try: scip_terraform pulls in the vendored
        # scip_pb2, whose protobuf runtime-version guard can raise at
        # IMPORT time (not call time). Keeping the import here contains any
        # such error to the tf leg (degrades to None), rather than letting
        # it escape both this except and the dispatch loop's
        # `except IndexerError` and kill indexing for every language.
        from .scip_terraform import index as tf_index

        return tf_index(target, out_dir)
    except Exception as exc:  # noqa: BLE001 — indexer must never break the scan
        logger.warning("code_graph: terraform index failed: %s", exc)
        return None


def _index_k8s(target: Path, out_dir: Path) -> Path | None:
    """SCIP index for Kubernetes/Helm. Detection: any Chart.yaml or
    kustomization.yaml. Renders (helm template / kustomize build) then resolves
    by-name cross-object refs (workload→SA, RoleBinding→SA/Role, Pod→Secret,
    Ingress→Service) into SCIP. Returns None when there's no k8s config or the
    render tooling (helm/kustomize) is unavailable — degrades cleanly like the
    other legs. Closes the k8s code-graph gap: per-manifest scanners (Checkov)
    can't see cross-object RBAC/secret chains; this makes them queryable for
    the agent's privesc reasoning + the triager's reachability grounding.
    v1: by-name edges only (label-selector edges are Phase 2)."""
    if not _has_files_matching(target, "Chart.yaml", "kustomization.yaml"):
        return None
    if not (_binary_exists("kustomize") or _binary_exists("helm")):
        # Sandbox may predate the helm/kustomize install; degrade rather than
        # fail (same posture as terraform-ls).
        logger.warning("code_graph: helm/kustomize missing; skipping k8s index")
        return None
    try:
        # Import inside the try (see _index_terraform): scip_k8s pulls in the
        # vendored scip_pb2 + pyyaml; any import-time error stays contained to
        # this leg instead of killing indexing for every language.
        from .scip_k8s import index as k8s_index

        return k8s_index(target, out_dir)
    except Exception as exc:  # noqa: BLE001 — indexer must never break the scan
        logger.warning("code_graph: k8s index failed: %s", exc)
        return None


def _convert_to_sqlite(scip_paths: tuple[Path, ...], out_dir: Path) -> Path:
    if not _binary_exists("scip"):
        raise IndexerError("scip CLI missing from sandbox")
    sqlite_path = out_dir / "code_graph.sqlite"

    # `scip expt-convert` takes ONE index at a time, so for a multi-language
    # target (Go + Python + TS in one repo) we convert each .scip to its own
    # SQLite and then UNION them with merge_sqlite_indexes — the same primitive
    # used for multi-target scans. A single .scip is the common case and skips
    # the merge (straight passthrough).
    #
    # Per-language isolation: a candidate can fail conversion — notably
    # scip-python emits synthetic `_ScratchFile#` symbols that expt-convert's
    # validator rejects (observed on a real polyglot repo). One language's
    # failure must not lose the others, so we convert best-effort and merge
    # whatever succeeded.
    converted: list[tuple[str, Path]] = []
    last_error: IndexerError | None = None
    for candidate in scip_paths:
        # Label by the .scip stem (e.g. "go", "py", "ts") — merge_sqlite_indexes
        # only prefixes paths when there's >1 source, so single-language keeps
        # bare relative paths.
        lang = candidate.stem
        per_lang = out_dir / f"{lang}.code_graph.sqlite"
        try:
            _run(["scip", "expt-convert", str(candidate), "--output", str(per_lang)])
        except IndexerError as exc:
            last_error = exc
            logger.warning(
                "code_graph: expt-convert failed on %s: %s", candidate.name, str(exc)[:300],
            )
            per_lang.unlink(missing_ok=True)
            continue
        converted.append((lang, per_lang))

    if converted:
        # For a single language, merge_sqlite_indexes copies through with bare
        # paths (no label prefix); for several it unions with per-language
        # prefixes. Either way the tool layer opens one code_graph.sqlite.
        merge_sqlite_indexes(converted, sqlite_path)
        if len(converted) > 1:
            logger.info(
                "code_graph: merged %d language index(es): %s",
                len(converted), [lang for lang, _ in converted],
            )
        return sqlite_path

    # All candidates failed conversion. Re-raise so _main() warn-and-
    # continues (INDEXER: SKIPPED → tool layer degrades to "code graph
    # unavailable", per the contract in W2.2's _open_index).
    raise last_error or IndexerError("no scip paths to convert")


def _select_languages(
    indexers: tuple[tuple[str, Any], ...],
    langs: str | None = None,
) -> tuple[tuple[str, Any], ...]:
    """Filter the indexer table by a language allowlist.

    ``langs`` is a comma-separated list of language keys (e.g.
    ``go,python,terraform``); when None it falls back to
    ``$STRIX_CODE_GRAPH_LANGS``. Empty/unset → all languages run (default).
    One image can therefore carry every toolchain while each scan indexes only
    the languages it cares about — faster, and lets a deployment skip a leg it
    has no repos for (e.g. a Go/IaC shop skipping ``java``). Unknown keys are
    ignored with a warning; if the filter selects nothing, fall back to all
    (a typo shouldn't silently disable code-graph entirely)."""
    raw = (langs if langs is not None else os.environ.get("STRIX_CODE_GRAPH_LANGS", "")).strip()
    if not raw:
        return indexers
    wanted = {tok.strip().lower() for tok in raw.split(",") if tok.strip()}
    known = {lang for lang, _ in indexers}
    unknown = wanted - known
    if unknown:
        logger.warning(
            "STRIX_CODE_GRAPH_LANGS: ignoring unknown language(s) %s (known: %s)",
            sorted(unknown), sorted(known),
        )
    selected = tuple((lang, fn) for lang, fn in indexers if lang in wanted)
    if not selected:
        logger.warning(
            "STRIX_CODE_GRAPH_LANGS=%r selected no known languages; running all",
            raw,
        )
        return indexers
    logger.info("code_graph: language selection active — %s", [lang for lang, _ in selected])
    return selected


def build_index(target_dir: Path, out_dir: Path) -> IndexResult:
    """Build SCIP indexes for the target repo, convert to SQLite.

    Detection is filename-based (tsconfig.json/package.json → TS; go.mod →
    Go). Multiple indexers run for multi-language repos but only the first
    is SQLite-converted in W1; W2 generalises the loader.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    scip_paths: list[Path] = []
    # Indexer order = priority. _convert_to_sqlite only consumes
    # scip_paths[0] today (W1 single-language path), so the first
    # language detected wins. TS first to match the common shape of
    # zh services (TS auth/business-logic with Python scripts);
    # multi-language merge is W5 follow-up.
    #
    # Per-language isolation: a language's indexer can RAISE (not just return
    # None) when its marker is present but the tool then fails — e.g. a repo
    # with package.json but no tsconfig.json makes scip-typescript exit rc=1.
    # Without isolation that exception aborts build_index entirely, so a
    # multi-language repo (package.json + go.mod + requirements.txt) whose
    # FIRST-tried language fails loses ALL code-graph indexing, including the
    # languages that would have indexed fine. Guard each call so one
    # language's hard failure degrades to the next instead of killing the run.
    # (Observed: composite-actions-smoke-test, package.json w/o tsconfig →
    # empty code_graph, no guard enrichment for its Go/Python either.)
    indexers = (
        ("typescript", _index_typescript),
        ("go", _index_go),
        ("python", _index_python),
        ("rust", _index_rust),
        ("java", _index_java),
        ("terraform", _index_terraform),
        ("kubernetes", _index_k8s),
    )
    indexers = _select_languages(indexers)
    for lang, index_fn in indexers:
        try:
            idx = index_fn(target_dir, out_dir)
        except IndexerError as exc:
            logger.warning(
                "code_graph: %s indexer failed (%s); continuing with other "
                "languages", lang, exc,
            )
            continue
        if idx is not None:
            scip_paths.append(idx)

    if not scip_paths:
        raise IndexerError(
            f"no supported source languages detected under {target_dir}"
        )

    sqlite_path = _convert_to_sqlite(tuple(scip_paths), out_dir)
    return IndexResult(
        target_dir=target_dir,
        scip_paths=tuple(scip_paths),
        sqlite_path=sqlite_path,
    )


def load_index(sqlite_path: Path) -> Path:
    """Stub: opens the SQLite index for tool consumption. W2 returns a
    connection wrapper with the find_definition / find_references / etc
    query methods bound."""
    if not sqlite_path.exists():
        raise IndexerError(f"code_graph index missing at {sqlite_path}")
    return sqlite_path


_PKG_SCHEMES = {"gomod", "npm", "python", "cargo", "maven", "nuget", "pypi", "semanticdb"}


def _normalize_moniker_version(symbol: str | None, label: str) -> str | None:
    """Normalize a package-manager SCIP moniker to its DESCRIPTOR so the same
    symbol unifies across repos regardless of dependency version OR how the
    indexer attributed the owning module.

    SCIP package monikers are space-delimited:
    ``<indexer> <scheme> <package-name> <version> <descriptor...>``, e.g.
    ``scip-go gomod github.com/acme/shared-messages v1.2002.0 `github.com/
    acme/shared-messages/pkg/events/v1`/OrderEvent#``.

    We blank BOTH the version (token 3) and the package-name (token 2), keying
    identity on ``<scheme> * * <descriptor>``. Two reasons, both observed on a
    real large multi-repo scan (~57 repos):

    1. VERSION drift — consumers pin different versions of a shared lib (and a
       working-tree checkout gets a commit-hash pseudo-version), so the same
       symbol is keyed under many versions. (In one real fleet, ~260 consumers
       of a single shared message library pinned ~150 distinct versions.)
    2. MODULE mis-attribution — scip-go, when it can't fully resolve a
       cross-module reference to the defining module, keys the symbol under the
       REFERENCING repo's module path rather than the dep's. So a shared
       library's ``OrderEvent`` can appear under many different module tokens;
       a version-only key found it in 2 repos, a descriptor-based key finds it
       in all ~12 that reference it.

    The DESCRIPTOR (the backtick-wrapped package path + symbol) is scip-go's true
    cross-module identity: it always carries the real defining package
    (``shared-messages/pkg/events/v1``), so keying on it merges the
    genuinely-same symbol while keeping genuinely-different types apart — e.g. a
    service's own local ``some-service/internal/models`/OrderEvent`` (a different
    type that merely shares the display name ``OrderEvent``) does NOT collapse
    into the shared proto one, because its descriptor path differs. The stored
    ``symbol`` value is untouched; this is only the DEDUP KEY.

    Non-package monikers (``local <id>`` etc.) and malformed/short monikers are
    qualified by ``label`` (the source target) rather than returned verbatim.
    SCIP's own "local N" numbering restarts per document/index — it is only
    unique WITHIN its own originating index, never globally — so two entirely
    unrelated locally-scoped symbols from different targets (or different
    per-language legs of the same target) routinely produce the identical
    literal moniker (e.g. both emit ``local 0``). Returning it verbatim would
    make the merge's cross-source dedup treat them as "the same symbol" and
    collapse them into one canonical node — silently wrong graph edges, not
    just a missed merge, and (observed live merging 5 real repos of five
    different languages) can also produce a genuine duplicate `mentions` row
    once two such wrongly-unified locals are each mentioned with the same
    role. Package monikers deliberately stay label-agnostic — that's the
    cross-repo edge this function exists to create.
    """
    if not symbol:
        return symbol
    parts = symbol.split(" ")
    # <indexer> <scheme> <name> <version> <descriptor...>  → need ≥5 and a
    # recognised package scheme in slot 1. `local` monikers have no version.
    if len(parts) >= 5 and parts[1] in _PKG_SCHEMES:
        parts[2] = "*"  # package-name: blank (indexer mis-attribution)
        parts[3] = "*"  # version: blank (pin drift / commit-hash pseudo-version)
        return " ".join(parts)
    return f"{label}\x00{symbol}"


def merge_sqlite_indexes(sources: list[tuple[str, Path]], dest: Path) -> Path:
    """Merge several per-target code-graph SQLite DBs into ONE, so a
    multi-target scan queries a single unified graph.

    ``sources`` is ``[(target_label, sqlite_path), ...]``. Each source's
    ``documents.relative_path`` is prefixed with ``<target_label>/`` so paths
    from different repos never collide (``src/app.ts`` → ``repo-b/src/app.ts``),
    and the label surfaces in every rendered location. Row ids are offset per
    source so primary keys and foreign keys (chunks/mentions/
    defn_enclosing_ranges → documents/global_symbols/chunks) stay consistent.

    Cross-repo EDGES only resolve where SCIP monikers genuinely match across
    targets (shared package coordinates); otherwise this is a co-resident union
    of per-repo graphs — see the addon README's limitations note.

    A single source is copied through unprefixed (bare relative paths), matching
    the single-target shape exactly.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)

    real = [(lbl, p) for lbl, p in sources if p.exists()]
    if not real:
        raise IndexerError("no source indexes to merge")
    if len(real) == 1:
        shutil.copyfile(real[0][1], dest)
        return dest

    conn = sqlite3.connect(dest)
    # Version-agnostic symbol identity: a package-manager SCIP moniker embeds the
    # dependency VERSION (`scip-go gomod <module> <version> <descriptor>`). The
    # SAME symbol is therefore keyed differently across repos — a consumer that
    # pins shared-messages v1.2002.0 references `... v1.2002.0 ...` while the
    # library indexed as a working tree defines `... 4c2fdaa43a2e ...` (a
    # commit-hash pseudo-version). Left as-is, no cross-repo edge EVER forms
    # (observed on a real service vertical: ~260 consumers pin ~150 distinct
    # versions of one shared lib). We normalise the version token to a constant
    # for the dedup KEY so the same symbol at any version collapses to one row —
    # the edge we want. Tradeoff: if two pinned versions genuinely diverged
    # (a symbol added/removed between releases) they merge anyway; for a
    # reachability/chaining graph an over-connected edge beats a missing one.
    conn.create_function("norm_moniker", 2, _normalize_moniker_version, deterministic=True)
    try:
        # Materialise the schema from the first source, then append every
        # source's rows with id offsets + path prefixes.
        first = real[0][1]
        conn.execute("ATTACH DATABASE ? AS src", (str(first),))
        conn.executescript(
            "".join(
                f"{row[0]};\n"
                for row in conn.execute(
                    "SELECT sql FROM src.sqlite_master "
                    "WHERE type='table' AND sql IS NOT NULL",
                ).fetchall()
            ),
        )
        conn.execute("DETACH DATABASE src")

        # global_symbols is DEDUPED on the UNIQUE `symbol` moniker, NOT
        # id-offset-appended like the other tables. A symbol defined in one
        # target and referenced in another appears in BOTH source indexes with
        # the SAME SCIP moniker (that is how SCIP cross-references work). Blindly
        # offsetting ids and re-inserting both would (a) violate the UNIQUE
        # constraint — the merge would crash — and (b) even if it didn't, split
        # one logical symbol across two ids so a reference in target B could
        # never resolve to a definition in target A. Deduping on the moniker and
        # remapping every source's symbol_id to the single canonical row is
        # PRECISELY what unifies the graph: it's the mechanism that lets
        # find_references / find_definition span repos. Locally-scoped symbols
        # carry module-qualified monikers (e.g. `scip-go gomod <module> ...`) so
        # genuinely distinct definitions don't collide — only truly shared
        # symbols merge, which is the intent.
        #
        # Column-agnostic on global_symbols: it carries extra columns beyond the
        # core set (notably the `relationships` protobuf blob find_implementations
        # reads); we pass every non-id column through verbatim. First-writer wins
        # for a shared symbol's descriptive columns (signature/docs); the
        # definition LOCATION comes from defn_enclosing_ranges, populated by
        # whichever target actually defines it, so find_definition is unaffected.
        gs_cols = [r[1] for r in conn.execute("PRAGMA table_info(global_symbols)").fetchall()]
        gs_non_id = [c for c in gs_cols if c != "id"]
        gs_col_list = ", ".join(gs_non_id)

        # PERF: the version-agnostic dedup key (norm_moniker) is a Python UDF, so
        # calling it inside a `NOT IN (SELECT norm_moniker(...))` / a join
        # predicate re-invokes it per row-PAIR — O(N²) UDF calls, which pegs a
        # core for tens of minutes on a big proto lib (tens of thousands of
        # symbols). Instead compute the key ONCE per row into an INDEXED `nkey`
        # column on dest, and once per source row into an indexed temp table, so
        # dedup + id-remap are ordinary indexed equality joins.
        conn.execute("ALTER TABLE global_symbols ADD COLUMN nkey TEXT")
        conn.execute("CREATE INDEX ix_gs_nkey ON global_symbols(nkey)")

        doc_off = chunk_off = 0
        for label, path in real:
            conn.execute("ATTACH DATABASE ? AS src", (str(path),))
            prefix = f"{label}/" if label else ""
            conn.execute(
                "INSERT INTO documents(id, language, relative_path, position_encoding, text) "
                "SELECT id + ?, language, ? || relative_path, position_encoding, text "
                "FROM src.documents",
                (doc_off, prefix),
            )
            # Materialise this source's symbols with their precomputed nkey once,
            # indexed — the norm_moniker UDF runs exactly len(src.global_symbols)
            # times total, not per comparison.
            conn.execute("DROP TABLE IF EXISTS _src_syms")
            conn.execute(
                f"CREATE TEMP TABLE _src_syms AS "
                f"SELECT id AS src_id, norm_moniker(symbol, ?) AS nkey, {gs_col_list} "
                f"FROM src.global_symbols",
                (label,),
            )
            conn.execute("CREATE INDEX ix_srcsyms_nkey ON _src_syms(nkey)")
            conn.execute("CREATE INDEX ix_srcsyms_id ON _src_syms(src_id)")
            # Insert only symbols whose NORMALIZED key isn't already present —
            # dedup collapses the same symbol pinned at different versions. dest
            # auto-assigns id (INTEGER PRIMARY KEY rowid alias). Stored `symbol`
            # is the first real moniker seen; nkey is the collision key.
            conn.execute(
                f"INSERT INTO global_symbols(nkey, {gs_col_list}) "
                f"SELECT s.nkey, {', '.join('s.'+c for c in gs_non_id)} "
                f"FROM _src_syms s "
                "WHERE s.nkey NOT IN (SELECT nkey FROM global_symbols)",
            )
            # ...then map EVERY source symbol_id (new or shared) to its canonical
            # dest id via the indexed nkey, so a v1.2002.0 reference and a
            # commit-hash definition of the same symbol point at one row — the
            # cross-repo edge. mentions/defn ranges below rewrite through this.
            conn.execute("DROP TABLE IF EXISTS _sym_map")
            conn.execute(
                "CREATE TEMP TABLE _sym_map AS "
                "SELECT s.src_id AS src_id, d.id AS dest_id "
                "FROM _src_syms s JOIN global_symbols d ON s.nkey = d.nkey",
            )
            conn.execute("CREATE INDEX ix_symmap_src ON _sym_map(src_id)")
            conn.execute(
                "INSERT INTO chunks"
                "(id, document_id, chunk_index, start_line, end_line, occurrences) "
                "SELECT id + ?, document_id + ?, chunk_index, start_line, end_line, occurrences "
                "FROM src.chunks",
                (chunk_off, doc_off),
            )
            # OR IGNORE: the version-agnostic dedup above is many-to-one by
            # design (_sym_map can send two DISTINCT source symbol_ids to the
            # same dest_id — e.g. two locally-scoped symbols whose monikers
            # happen to normalize identically). If both of those source
            # symbols are mentioned with the same role in the same chunk, the
            # remapped rows are now IDENTICAL triples -- a real UNIQUE
            # constraint violation observed live merging 5 real repos ("even
            # deeper issue" beyond the CARGO_HOME/RUSTUP_HOME fixes). The
            # fact asserted ("this chunk mentions this canonical symbol with
            # this role") is unchanged by the duplicate; there's nothing to
            # lose by keeping just one row.
            conn.execute(
                "INSERT OR IGNORE INTO mentions(chunk_id, symbol_id, role) "
                "SELECT m.chunk_id + ?, mp.dest_id, m.role "
                "FROM src.mentions m JOIN _sym_map mp ON m.symbol_id = mp.src_id",
                (chunk_off,),
            )
            conn.execute(
                "INSERT INTO defn_enclosing_ranges"
                "(symbol_id, document_id, start_line, start_char, end_line, end_char) "
                "SELECT mp.dest_id, r.document_id + ?, "
                "r.start_line, r.start_char, r.end_line, r.end_char "
                "FROM src.defn_enclosing_ranges r JOIN _sym_map mp ON r.symbol_id = mp.src_id",
                (doc_off,),
            )
            maxes = conn.execute(
                "SELECT (SELECT COALESCE(MAX(id),0) FROM src.documents), "
                "(SELECT COALESCE(MAX(id),0) FROM src.chunks)",
            ).fetchone()
            doc_off += maxes[0]
            chunk_off += maxes[1]
            # Commit before DETACH: the INSERTs opened an implicit transaction,
            # and SQLite refuses to detach an attached DB with a live txn
            # ("database src is locked").
            conn.commit()
            conn.execute("DETACH DATABASE src")
    finally:
        conn.close()
    return dest


def build_index_cached(
    target_dir: Path,
    out_dir: Path,
    *,
    repo: str | None = None,
    head_sha: str | None = None,
    cache: CodeGraphCache | None = None,
) -> IndexResult:
    """Build the index, but consult the cache first when (repo, head_sha)
    are known. Cache miss falls through to a full build and a put."""
    cache = cache if cache is not None else _cache_from_env()
    sqlite_path = out_dir / "code_graph.sqlite"

    cache_key: CacheKey | None = None
    if repo and head_sha:
        try:
            cache_key = CacheKey(repo=repo, head_sha=head_sha)
        except ValueError as exc:
            logger.warning("code_graph: invalid cache key (%s); proceeding uncached", exc)

    if cache_key is not None and cache.get(cache_key, sqlite_path):
        return IndexResult(
            target_dir=target_dir,
            scip_paths=(),
            sqlite_path=sqlite_path,
        )

    result = build_index(target_dir, out_dir)
    if cache_key is not None:
        cache.put(cache_key, result.sqlite_path)
    return result


def _main(argv: list[str] | None = None) -> int:
    """Sandbox-side CLI: invoked by docker_runtime.create_sandbox after the
    target repo is copied into the container. Failure must not break the
    scan — exit 0 even on indexer error, just leave the SQLite missing and
    let the tools layer handle the absence (W2)."""
    # diag: print() to stderr bypasses log-config no-op risk
    # (basicConfig is a no-op if any import already configured logging).
    # Keep these prints until SCIP is end-to-end-validated in GHA.
    print("INDEXER: _main entered", file=sys.stderr, flush=True)
    parser = argparse.ArgumentParser(
        prog="python -m strix.tools.code_graph.indexer",
        description="Build SCIP code-graph index for the target repo.",
    )
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--repo", default=None, help="owner/name, for cache key")
    parser.add_argument("--head-sha", default=None, help="full commit SHA, for cache key")
    parser.add_argument(
        "--langs",
        default=None,
        help="comma-separated language allowlist (e.g. go,python,terraform); "
        "default = $STRIX_CODE_GRAPH_LANGS or all",
    )
    parser.add_argument(
        "--gomodcache",
        default=None,
        help="path to a warm Go module cache (in-sandbox) for offline scip-go "
        "resolution of private deps; default = $STRIX_GO_MODCACHE or none",
    )
    args = parser.parse_args(argv)
    # Thread --langs / --gomodcache to the legs via the env vars they read
    # (avoids plumbing params through build_index_cached → build_index; also
    # the only channel available, since session.exec has no env= kwarg).
    if args.langs is not None:
        os.environ["STRIX_CODE_GRAPH_LANGS"] = args.langs
    if args.gomodcache is not None:
        os.environ["STRIX_GO_MODCACHE"] = args.gomodcache
    print(
        f"INDEXER: args target={args.target} out_dir={args.out_dir} "
        f"repo={args.repo} head_sha={args.head_sha}",
        file=sys.stderr,
        flush=True,
    )
    print(
        f"INDEXER: target.exists={args.target.exists()} "
        f"tsconfig={(args.target / 'tsconfig.json').exists()} "
        f"package.json={(args.target / 'package.json').exists()} "
        f"go.mod={(args.target / 'go.mod').exists()}",
        file=sys.stderr,
        flush=True,
    )

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        result = build_index_cached(
            args.target,
            args.out_dir,
            repo=args.repo,
            head_sha=args.head_sha,
        )
        print(
            f"INDEXER: SUCCESS sqlite={result.sqlite_path} "
            f"scip_paths={[str(p) for p in result.scip_paths]}",
            file=sys.stderr,
            flush=True,
        )
    except IndexerError as exc:
        # Warn-and-continue: a missing index means W2 graph tools degrade
        # to no-ops; it does not break the scan.
        print(f"INDEXER: SKIPPED ({exc})", file=sys.stderr, flush=True)
    except Exception as exc:
        # Diagnostic: surface any non-IndexerError exceptions to stderr
        # before re-raising. Without this they'd disappear silently.
        import traceback
        print(
            f"INDEXER: UNEXPECTED {type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            file=sys.stderr,
            flush=True,
        )
        raise
    return 0


if __name__ == "__main__":
    sys.exit(_main())
