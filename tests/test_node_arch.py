"""_node_arch()/_ensure_node_version(): the on-demand Node install must fetch
a tarball matching the RUNNER's own architecture, not a hardcoded one.

Previously hardcoded to "linux-x64" -- on an arm64 runner, the fetched
tarball is an incompatible-architecture ELF binary. The shell can't exec it
and falls back to interpreting its binary content AS a script, producing a
confusing "Syntax error: ')' unexpected" with no indication the real
problem is architecture mismatch. Live-observed on an arm64 ARC runner:
every repo pinning engines.node (funding-service, trade-repository-service)
hit this on every run.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from strix_code_graph import indexer


def test_node_arch_maps_aarch64_to_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indexer.platform, "machine", lambda: "aarch64")
    assert indexer._node_arch() == "arm64"


def test_node_arch_maps_arm64_to_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indexer.platform, "machine", lambda: "arm64")
    assert indexer._node_arch() == "arm64"


def test_node_arch_defaults_to_x64_for_unmapped_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indexer.platform, "machine", lambda: "x86_64")
    assert indexer._node_arch() == "x64"


def test_ensure_node_version_returns_none_without_package_json(tmp_path: Path) -> None:
    assert indexer._ensure_node_version(tmp_path) is None


def test_ensure_node_version_returns_none_without_engines_pin(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "foo"}')
    assert indexer._ensure_node_version(tmp_path) is None


def test_ensure_node_version_fetches_the_runner_own_architecture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "package.json").write_text('{"engines": {"node": "22.15.0"}}')
    monkeypatch.setattr(indexer.platform, "machine", lambda: "aarch64")
    # System `node --version` check: force it to NOT match, so this reaches
    # the fetch path rather than short-circuiting.
    monkeypatch.setattr(
        indexer.subprocess, "run",
        lambda cmd, **kw: indexer.subprocess.CompletedProcess(cmd, 0, stdout="v18.0.0\n"),
    )

    calls = []
    real_dir = Path("/tmp/node-v22.15.0-linux-arm64")

    def _fake_run(cmd, timeout=600, **kw):
        calls.append(cmd)
        # Simulate the tarball extraction actually landing the binary at
        # the path _ensure_node_version will check for afterward.
        (real_dir / "bin").mkdir(parents=True, exist_ok=True)
        (real_dir / "bin" / "node").write_text("#!/bin/sh\n")

    monkeypatch.setattr(indexer, "_run", _fake_run)
    try:
        result = indexer._ensure_node_version(tmp_path)
        assert result == "/tmp/node-v22.15.0-linux-arm64/bin"
        assert len(calls) == 1
        fetch_cmd = calls[0]
        assert "node-v22.15.0-linux-arm64.tar.xz" in fetch_cmd[-1]
        assert "linux-x64" not in fetch_cmd[-1]
    finally:
        import shutil
        shutil.rmtree("/tmp/node-v22.15.0-linux-arm64", ignore_errors=True)


def test_ensure_node_version_uses_cache_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "package.json").write_text('{"engines": {"node": "22.15.0"}}')
    monkeypatch.setattr(indexer.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(
        indexer.subprocess, "run",
        lambda cmd, **kw: indexer.subprocess.CompletedProcess(cmd, 0, stdout="v18.0.0\n"),
    )
    cached_dir = Path("/tmp/node-v22.15.0-linux-arm64")
    (cached_dir / "bin").mkdir(parents=True, exist_ok=True)
    (cached_dir / "bin" / "node").write_text("#!/bin/sh\n")

    def _boom(*a, **kw):
        raise AssertionError("must not fetch when the correct-arch dir is already cached")
    monkeypatch.setattr(indexer, "_run", _boom)

    try:
        assert indexer._ensure_node_version(tmp_path) == str(cached_dir / "bin")
    finally:
        import shutil
        shutil.rmtree(cached_dir, ignore_errors=True)
