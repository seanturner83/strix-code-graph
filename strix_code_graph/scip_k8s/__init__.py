"""scip-k8s: a Kubernetes/Helm → SCIP indexer.

Renders charts (`helm template`) / overlays (`kustomize build`) to concrete
manifests, then resolves by-name cross-object references (workload → SA,
RoleBinding → SA/Role, Pod → Secret, Ingress → Service) into a SCIP index so
the code_graph query layer can answer "what binds this Role?" /
"is this SA reachable from an exposed workload?".

Public API:
  index(target, out_dir) -> Path | None   # build k8s.scip for a repo
"""
from .indexer import index  # noqa: F401
