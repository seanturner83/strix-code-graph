"""scip-terraform: a Terraform/HCL → SCIP indexer driven by terraform-ls.

Public API:
  index(target, out_dir) -> Path | None   # build tf.scip for a repo
"""
from .indexer import index  # noqa: F401
