"""scip-protobuf: a Protobuf/Buf schema -> SCIP indexer driven by `buf build`.

Public API:
  index(target, out_dir) -> Path | None   # build proto.scip for a repo
"""
from .indexer import index  # noqa: F401
