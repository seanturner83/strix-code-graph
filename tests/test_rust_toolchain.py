"""_cargo_home() / _ensure_rust_toolchain(): the Rust toolchain install root
must be configurable via CARGO_HOME, not hardcoded to /home/pentester --
that path is the SANDBOX image's user, and this addon also needs to run
standalone (e.g. a corpus-wide index build on a bare CI runner) where that
user doesn't exist. Default (no CARGO_HOME set) must stay byte-for-byte the
original hardcoded path, so the baked sandbox image's behaviour is
unaffected.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from strix_code_graph import indexer


def test_cargo_home_defaults_to_pentester_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CARGO_HOME", raising=False)
    assert indexer._cargo_home() == Path("/home/pentester")


def test_cargo_home_respects_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARGO_HOME", "/runner/rust-toolchain")
    assert indexer._cargo_home() == Path("/runner/rust-toolchain")


def test_cargo_home_strips_trailing_dot_cargo_from_override(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    # CARGO_HOME conventionally points AT .cargo itself; this function
    # returns the PARENT (the toolchain root, sibling to .rustup), not
    # .cargo/.cargo.
    monkeypatch.setenv("CARGO_HOME", "/runner/rust-toolchain/.cargo")
    assert indexer._cargo_home() == Path("/runner/rust-toolchain")


def test_ensure_rust_toolchain_returns_cached_bin_without_installing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / ".cargo"))
    cargo_bin = tmp_path / ".cargo" / "bin"
    cargo_bin.mkdir(parents=True)
    (cargo_bin / "rust-analyzer").write_text("#!/bin/sh\n")

    def _boom(*a, **kw):
        raise AssertionError("must not attempt install when rust-analyzer already exists")
    monkeypatch.setattr(indexer, "_run", _boom)

    assert indexer._ensure_rust_toolchain() == str(cargo_bin)


def test_ensure_rust_toolchain_installs_into_configured_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / ".cargo"))
    cargo_bin = tmp_path / ".cargo" / "bin"

    calls = []

    def _fake_run(cmd, cwd=None, timeout=600, env=None, base_env=None):
        calls.append({"cmd": cmd, "env": env})
        # Simulate rustup-init + component-add actually landing the binary.
        cargo_bin.mkdir(parents=True, exist_ok=True)
        (cargo_bin / "rust-analyzer").write_text("#!/bin/sh\n")

    monkeypatch.setattr(indexer, "_run", _fake_run)
    result = indexer._ensure_rust_toolchain()

    assert result == str(cargo_bin)
    assert len(calls) == 2
    for c in calls:
        assert c["env"]["CARGO_HOME"] == str(tmp_path / ".cargo")
        assert c["env"]["RUSTUP_HOME"] == str(tmp_path / ".rustup")
    # Second call (component add) must invoke the JUST-installed rustup by
    # its full configured path, not a bare "rustup" that could resolve to
    # some other toolchain on PATH.
    assert calls[1]["cmd"][0] == str(cargo_bin / "rustup")


def test_ensure_rust_toolchain_returns_none_when_install_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / ".cargo"))

    def _boom(*a, **kw):
        raise indexer.IndexerError("network unreachable")
    monkeypatch.setattr(indexer, "_run", _boom)

    assert indexer._ensure_rust_toolchain() is None


def test_ensure_rust_toolchain_returns_none_when_binary_still_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / ".cargo"))
    # _run "succeeds" (no exception) but never actually wrote rust-analyzer --
    # the install reported success without the binary landing where expected.
    monkeypatch.setattr(indexer, "_run", lambda *a, **kw: None)

    assert indexer._ensure_rust_toolchain() is None


def test_index_rust_passes_matching_cargo_and_rustup_home_to_cargo_fetch_and_scip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: cargo/rust-analyzer are rustup PROXY binaries that resolve
    the active toolchain via CARGO_HOME/RUSTUP_HOME at RUN time, not just at
    install time. _ensure_rust_toolchain's install only set these for its OWN
    subprocess calls -- _index_rust's later cargo-fetch/rust-analyzer-scip
    calls used base_env=scrubbed with no env= override, so on any host where
    _cargo_home() differs from rustup's own default ($HOME/.rustup) -- e.g.
    CARGO_HOME pointed at a CI workspace dir -- those calls silently resolved
    against the WRONG (empty) toolchain root and failed with "Unknown binary
    'rust-analyzer' in official toolchain ...". Never surfaced against the
    sandbox's hardcoded /home/pentester default because HOME was already
    /home/pentester there too.
    """
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / ".cargo"))
    cargo_bin = tmp_path / ".cargo" / "bin"
    cargo_bin.mkdir(parents=True)
    (cargo_bin / "rust-analyzer").write_text("#!/bin/sh\n")

    target = tmp_path / "some-crate"
    target.mkdir()
    (target / "Cargo.toml").write_text("[package]\nname = \"foo\"\n")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    calls = []

    def _fake_run(cmd, cwd=None, timeout=600, env=None, base_env=None):
        calls.append({"cmd": cmd, "env": env})
        if cmd[0] == cargo_bin / "rust-analyzer" or (isinstance(cmd[0], str) and cmd[0].endswith("rust-analyzer")):
            (out_dir / "rs.scip").write_bytes(b"fake scip")

    monkeypatch.setattr(indexer, "_run", _fake_run)
    indexer._index_rust(target, out_dir)

    assert len(calls) == 2  # cargo fetch, then rust-analyzer scip
    for c in calls:
        assert c["env"] == {
            "CARGO_HOME": str(tmp_path / ".cargo"),
            "RUSTUP_HOME": str(tmp_path / ".rustup"),
        }


def test_ensure_rust_toolchain_installs_only_once_under_concurrent_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standalone multi-repo indexing job (this addon's own primary
    concurrent consumer today) runs several Rust targets' indexing in
    parallel worker threads -- this function was originally written for
    strix-code-graph's own in-sandbox loop, which indexes strictly
    sequentially, so the check-then-install here was never exercised
    concurrently before. Without a lock, N threads can all observe
    "not installed yet" and all launch rustup-init into the same
    CARGO_HOME/RUSTUP_HOME at once."""
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / ".cargo"))
    cargo_bin = tmp_path / ".cargo" / "bin"
    install_calls = []
    lock = threading.Lock()

    def _fake_run(cmd, cwd=None, timeout=600, env=None, base_env=None):
        with lock:
            install_calls.append(cmd)
        # Simulate a slow install so concurrent callers actually overlap.
        time.sleep(0.05)
        cargo_bin.mkdir(parents=True, exist_ok=True)
        (cargo_bin / "rust-analyzer").write_text("#!/bin/sh\n")

    monkeypatch.setattr(indexer, "_run", _fake_run)

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(indexer._ensure_rust_toolchain()))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [str(cargo_bin)] * 8
    # Exactly one install attempt (2 _run calls: rustup-init + component add)
    # -- not one PER concurrent caller.
    assert len(install_calls) == 2, f"expected exactly 1 install (2 calls), got {install_calls}"
