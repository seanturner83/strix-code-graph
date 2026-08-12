"""APPSEC-1396: the indexer's install/build steps run the TARGET repo's own
build backend, so credential-shaped env vars must not reach that subprocess.

These tests exercise the real subprocess path (no mocking of _run/subprocess)
against a fixture that behaves like the PoC's malicious setup.py: it dumps
os.environ to a file so the test can assert on exactly what the child process
saw.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

import pytest

from strix_code_graph import indexer


def test_scrubbed_env_drops_credential_shaped_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NPM_TOKEN", "npm_secret_abc")
    monkeypatch.setenv("TOOL_SERVER_TOKEN", "ts_tok_xyz")
    monkeypatch.setenv("NODE_AUTH_TOKEN", "node_secret")
    monkeypatch.setenv("CARGO_REGISTRIES_FOO_TOKEN", "cargo_secret")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN-----")
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "aws_secret")
    monkeypatch.setenv("SOME_AUTH_HEADER", "Bearer xyz")

    scrubbed = indexer._scrubbed_env()

    for leaked in (
        "NPM_TOKEN", "TOOL_SERVER_TOKEN", "NODE_AUTH_TOKEN",
        "CARGO_REGISTRIES_FOO_TOKEN", "GITHUB_APP_PRIVATE_KEY",
        "DB_PASSWORD", "AWS_SESSION_TOKEN", "SOME_AUTH_HEADER",
    ):
        assert leaked not in scrubbed, f"{leaked} survived the scrub"


def test_scrubbed_env_keeps_non_credential_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/pentester")
    monkeypatch.setenv("GOPROXY", "https://proxy.golang.org")
    monkeypatch.setenv("LANG", "en_US.UTF-8")

    scrubbed = indexer._scrubbed_env()

    assert scrubbed.get("PATH") == "/usr/bin:/bin"
    assert scrubbed.get("HOME") == "/home/pentester"
    assert scrubbed.get("GOPROXY") == "https://proxy.golang.org"
    assert scrubbed.get("LANG") == "en_US.UTF-8"


def test_run_base_env_replaces_rather_than_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    """base_env must be the subprocess's ONLY base — os.environ itself (with
    the secret still in it) must not leak in underneath."""
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "should-not-appear")
    scrubbed = {"PATH": os.environ.get("PATH", "")}
    indexer._run(["python3", "-c", "import os,sys; sys.exit(1 if 'SUPER_SECRET_TOKEN' in os.environ else 0)"],
                 base_env=scrubbed)


def test_run_env_still_additive_onto_os_environ_when_no_base_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged behaviour for existing callers (_index_go): env= alone still
    layers onto the full os.environ, doesn't scrub anything."""
    monkeypatch.setenv("EXISTING_VAR", "still-here")
    indexer._run(
        ["python3", "-c",
         "import os,sys; sys.exit(0 if os.environ.get('EXISTING_VAR')=='still-here' "
         "and os.environ.get('EXTRA')=='added' else 1)"],
        env={"EXTRA": "added"},
    )


def test_malicious_setup_py_cannot_read_scrubbed_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end mirror of the APPSEC-1396 PoC: a setup.py that tries to
    read a forwarded secret must find it gone when installed via the
    scrubbed env _index_python now uses."""
    monkeypatch.setenv("NPM_TOKEN", "npm_FORWARDED_SECRET_abc123")
    proof = tmp_path / "proof.json"

    target = tmp_path / "malicious_target"
    target.mkdir()
    (target / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools"]\n'
        'build-backend = "setuptools.build_meta"\n'
    )
    (target / "setup.py").write_text(
        "import os, json\n"
        f"stolen = os.environ.get('NPM_TOKEN')\n"
        f"open({str(proof)!r}, 'w').write(json.dumps({{'stolen': stolen}}))\n"
        "from setuptools import setup\n"
        "setup(name='evil-proj', version='0.0.0', py_modules=[])\n"
    )
    (target / "evil_proj.py").write_text("X = 1\n")

    # Install into a throwaway venv rather than the host interpreter — macOS
    # Homebrew Python refuses global installs (PEP 668
    # externally-managed-environment), which is a local dev-machine artifact,
    # not something the real in-container indexer hits (that sandbox is built
    # specifically to run pip installs).
    venv_dir = tmp_path / ".venv"
    indexer._run([sys.executable, "-m", "venv", str(venv_dir)], timeout=60)
    venv_python = venv_dir / "bin" / "python"

    # install failure is fine here; we only care whether the secret leaked
    with contextlib.suppress(indexer.IndexerError):
        indexer._run(
            [str(venv_python), "-m", "pip", "install", "--no-deps", "-e", "."],
            cwd=target,
            timeout=120,
            base_env=indexer._scrubbed_env(),
        )

    assert proof.exists(), "setup.py never ran — test fixture is broken"
    result = json.loads(proof.read_text())
    assert result["stolen"] is None, (
        f"NPM_TOKEN leaked into the target's setup.py: {result['stolen']!r}"
    )


def test_malicious_build_rs_cannot_read_scrubbed_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end mirror of the APPSEC-1396 PoC for Rust: `cargo fetch` alone
    does NOT trigger build.rs (verified manually, not asserted here — this
    test only covers the step that DOES: rust-analyzer's own scip/metadata
    pass, the exact call _index_rust makes)."""
    if not indexer._binary_exists("rust-analyzer") or not indexer._binary_exists("cargo"):
        pytest.skip("rust-analyzer/cargo not available in this environment")

    monkeypatch.setenv("NPM_TOKEN", "npm_FORWARDED_SECRET_abc123")
    proof = tmp_path / "proof.txt"

    target = tmp_path / "malicious_crate"
    (target / "src").mkdir(parents=True)
    (target / "Cargo.toml").write_text(
        '[package]\nname = "evil-crate"\nversion = "0.1.0"\nedition = "2021"\n'
        'build = "build.rs"\n'
    )
    # Rust string literals are double-quoted, not Python repr() single-quoted
    # (that produces a char literal and fails to compile) — escape backslashes
    # for Windows-style paths, which won't occur here but keeps this correct.
    proof_rust_literal = '"' + str(proof).replace("\\", "\\\\") + '"'
    (target / "build.rs").write_text(
        "use std::env;\nuse std::fs;\n\n"
        "fn main() {\n"
        '    let stolen = env::var("NPM_TOKEN").ok();\n'
        f"    fs::write({proof_rust_literal}, format!(\"{{:?}}\", stolen)).unwrap();\n"
        "}\n"
    )
    (target / "src" / "lib.rs").write_text("pub fn hello() -> &'static str { \"hello\" }\n")

    out = target / "out.scip"
    indexer._run(
        ["rust-analyzer", "scip", str(target), "--output", str(out)],
        cwd=target,
        timeout=120,
        base_env=indexer._scrubbed_env(),
    )

    assert proof.exists(), "build.rs never ran — test fixture is broken"
    assert proof.read_text().strip() == "None", (
        f"NPM_TOKEN leaked into the target's build.rs: {proof.read_text()!r}"
    )
