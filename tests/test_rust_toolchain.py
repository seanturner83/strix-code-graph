"""_cargo_home() / _ensure_rust_toolchain(): the Rust toolchain install root
must be configurable via CARGO_HOME, not hardcoded to /home/pentester --
that path is the SANDBOX image's user, and this addon also needs to run
standalone (e.g. a corpus-wide index build on a bare CI runner) where that
user doesn't exist. Default (no CARGO_HOME set) must stay byte-for-byte the
original hardcoded path, so the baked sandbox image's behaviour is
unaffected.
"""

from __future__ import annotations

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
