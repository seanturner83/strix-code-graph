"""Every module's logger must be a child of Strix's own tracked "strix"
logger root, not a separate "strix_code_graph.*" namespace.

Strix's logging setup (strix/telemetry/logging.py setup_scan_logging) only
attaches its file/stream handlers to loggers named "strix" or
"openai.agents" (and their children). A logger named plain __name__
("strix_code_graph.bootstrap") is a SEPARATE top-level namespace that
Strix's handlers never reach -- live-observed: every logger.info/warning
call in this addon was silently swallowed during a real Strix scan.
"""
from __future__ import annotations

import logging

from strix_code_graph import (
    bootstrap,
    cache,
    event_flow,
    indexer,
    query,
    tools,
)
from strix_code_graph.scip_k8s import indexer as k8s_indexer
from strix_code_graph.scip_protobuf import indexer as protobuf_indexer
from strix_code_graph.scip_terraform import indexer as terraform_indexer

_MODULES = [
    bootstrap, cache, event_flow, indexer, query, tools,
    k8s_indexer, protobuf_indexer, terraform_indexer,
]


def test_every_module_logger_is_a_child_of_strix() -> None:
    for mod in _MODULES:
        name = mod.logger.name
        assert name == "strix" or name.startswith("strix."), (
            f"{mod.__name__}'s logger is {name!r} -- not a child of Strix's "
            f"own tracked 'strix' logger root, so Strix's logging setup "
            f"never attaches a handler to it"
        )


def test_module_logger_propagates_into_a_strix_named_ancestor() -> None:
    # The exact mechanism setup_scan_logging relies on: a record emitted on
    # the child logger must reach a handler attached to a logger literally
    # named "strix" via normal propagation.
    strix_root = logging.getLogger("strix")
    records = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    strix_root.addHandler(handler)
    try:
        bootstrap.logger.warning("test propagation")
    finally:
        strix_root.removeHandler(handler)

    assert any(r.getMessage() == "test propagation" for r in records)
