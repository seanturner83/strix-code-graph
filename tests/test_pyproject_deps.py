"""strix_code_graph/scip_k8s/indexer.py imports yaml directly to parse
Chart.yaml/values.yaml. pyproject.toml must declare it as a real dependency
-- it previously didn't, and only "worked" by riding along transitively via
strix-agent's own dependency tree, which silently broke for any install
path that skips strix-agent (e.g. a standalone `pip install --no-deps`,
used both by sandbox/Dockerfile and external corpus-wide index builds).
"""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_pyproject_declares_pyyaml() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text()
    )
    deps = pyproject["project"]["dependencies"]
    assert any(d.strip().lower().startswith("pyyaml") for d in deps), (
        "scip_k8s/indexer.py does `import yaml` directly -- pyproject.toml "
        "must declare pyyaml as a real dependency, not rely on it riding "
        "along via strix-agent's own transitive deps."
    )
