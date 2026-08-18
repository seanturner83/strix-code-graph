"""_index_typescript: --infer-tsconfig must always be passed to scip-typescript.

Live-verified upstream (scip-typescript 0.4.0, the exact version this repo
pins): --infer-tsconfig synthesizes a tsconfig.json for real TS/JS source
that has none (confirmed against a real corpus repo with 16 real .js files
and no tsconfig -- previously "no supported source languages detected",
now 16 real documents), and is a byte-identical no-op when a real
tsconfig.json already exists (confirmed against a real repo with one --
output was byte-for-byte identical with/without the flag). So it's safe to
pass unconditionally rather than branching on whether a tsconfig was found.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from strix_code_graph import indexer


def test_passes_infer_tsconfig_flag_to_scip_typescript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "package.json").write_text('{"name": "foo"}')
    monkeypatch.setattr(indexer, "_binary_exists", lambda name: True)
    monkeypatch.setattr(indexer, "_ensure_node_version", lambda target: None)

    calls = []

    def _fake_run(cmd, cwd=None, **kw):
        calls.append(cmd)
        if cmd[0] == "scip-typescript":
            (tmp_path / "ts.scip").write_bytes(b"fake")
    monkeypatch.setattr(indexer, "_run", _fake_run)

    result = indexer._index_typescript(tmp_path, tmp_path)
    assert result == tmp_path / "ts.scip"
    ts_calls = [c for c in calls if c[0] == "scip-typescript"]
    assert len(ts_calls) == 1
    assert "--infer-tsconfig" in ts_calls[0]


def test_returns_none_when_scip_typescript_produces_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # scip-typescript's own degrade path for zero real source (e.g. a
    # config-only npm package): exits 0, prints "no files got indexed",
    # writes no output file. Must not raise -- same graceful degrade as
    # every other no-content case.
    (tmp_path / "package.json").write_text('{"name": "foo"}')
    monkeypatch.setattr(indexer, "_binary_exists", lambda name: True)
    monkeypatch.setattr(indexer, "_ensure_node_version", lambda target: None)
    monkeypatch.setattr(indexer, "_run", lambda cmd, cwd=None, **kw: None)

    assert indexer._index_typescript(tmp_path, tmp_path) is None


def test_npm_install_falls_back_to_legacy_peer_deps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Live-observed (seed-pg-ts): an ERESOLVE peer-dependency conflict
    # (old ts-node alongside a typeorm that peer-requires a newer one)
    # persists unchanged through --engine-strict=false -- a DIFFERENT
    # failure mode needs a DIFFERENT flag.
    (tmp_path / "package.json").write_text('{"name": "foo"}')
    monkeypatch.setattr(indexer, "_binary_exists", lambda name: True)
    monkeypatch.setattr(indexer, "_ensure_node_version", lambda target: None)

    calls = []

    def _fake_run(cmd, cwd=None, **kw):
        calls.append(cmd)
        if cmd[0] == "npm":
            if "--legacy-peer-deps" not in cmd:
                raise indexer.IndexerError("npm error code ERESOLVE")
        elif cmd[0] == "scip-typescript":
            (tmp_path / "ts.scip").write_bytes(b"fake")
    monkeypatch.setattr(indexer, "_run", _fake_run)

    result = indexer._index_typescript(tmp_path, tmp_path)
    assert result == tmp_path / "ts.scip"
    npm_calls = [c for c in calls if c[0] == "npm"]
    assert len(npm_calls) == 3
    assert "--legacy-peer-deps" in npm_calls[2]
    assert "--engine-strict=false" in npm_calls[2]


def test_npm_install_gives_up_after_all_tiers_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "package.json").write_text('{"name": "foo"}')
    monkeypatch.setattr(indexer, "_binary_exists", lambda name: True)
    monkeypatch.setattr(indexer, "_ensure_node_version", lambda target: None)

    calls = []

    def _fake_run(cmd, cwd=None, **kw):
        calls.append(cmd)
        if cmd[0] == "npm":
            raise indexer.IndexerError("npm error code ERESOLVE")
        if cmd[0] == "scip-typescript":
            (tmp_path / "ts.scip").write_bytes(b"fake")
    monkeypatch.setattr(indexer, "_run", _fake_run)

    # Must not raise -- proceeds to index without deps, same posture as
    # every other degrade-gracefully case.
    result = indexer._index_typescript(tmp_path, tmp_path)
    assert result == tmp_path / "ts.scip"
    assert len([c for c in calls if c[0] == "npm"]) == 3


def test_run_error_message_falls_back_to_stdout_when_stderr_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Live-observed (scip-typescript's own "no files got indexed"
    # diagnostic): some tools write their real error text to stdout, not
    # stderr. stderr-only capture silently discarded it, reporting a
    # blank message for a genuinely informative failure.
    class _FakeCompletedProcess:
        returncode = 1
        stderr = ""
        stdout = "error: no files got indexed."

    monkeypatch.setattr(
        indexer.subprocess, "run", lambda *a, **kw: _FakeCompletedProcess(),
    )
    with pytest.raises(indexer.IndexerError, match="no files got indexed"):
        indexer._run(["scip-typescript", "index"])
