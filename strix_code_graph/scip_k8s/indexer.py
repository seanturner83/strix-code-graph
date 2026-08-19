"""Kubernetes/Helm → SCIP indexer.

Pipeline:
  1. discover render roots under the target (kustomization.yaml dirs → rendered
     with `kustomize build --enable-helm`; standalone Chart.yaml+templates/ dirs
     → `helm template`). Many services are kustomize-driven and pull their
     chart via `helmCharts:`, so one `kustomize build` yields the concrete
     chart+overlay manifests.
  2. parse the rendered multi-document YAML into k8s objects.
  3. two-pass by-name resolution:
       pass 1 — index every object as a definition symbol
                (<namespace>/<Kind>/<name>#), recording its source location.
       pass 2 — walk the security-relevant reference fields on each object
                (serviceAccountName, roleRef, subjects[], secret mounts,
                ingress backend service) and emit a reference occurrence
                pointing at the referent's definition symbol.
  4. emit one SCIP index; `scip expt-convert` (called by the parent indexer)
     folds it into the queryable SQLite.

v1 scope: BY-NAME edges only (RBAC + secret + ingress→service). Label-selector
edges (Service/NetworkPolicy → pods) are Phase 2 — they're many-to-many set
matching, not name references.

Everything degrades to None/skip rather than raising, so a malformed chart or
a missing renderer never breaks the scan (the parent leg wraps this in a
catch-all too).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from .emit import Document, Occurrence, build_index, make_symbol, write_index

# See bootstrap.py's comment: renamed to a "strix."-prefixed child so
# Strix's own logging setup actually attaches a handler to it.
logger = logging.getLogger(__name__.replace("strix_code_graph", "strix.code_graph", 1))

# Per-render wall-clock cap. kustomize's helm inflation + the checkov-documented
# parallel-kustomize race (bridgecrewio/checkov#6845) can wedge a render; bound
# it so one bad root can't eat the indexer's 10-min budget.
_RENDER_TIMEOUT_S = 90
# Cap how many render roots we process. The fleet has ~2600 kustomize dirs
# (per-service × per-env overlays); rendering all of them would blow the
# budget. A scan target is normally a single service/chart, so this is a
# defence cap, not the expected path — we log when it trips.
_MAX_RENDER_ROOTS = 40

_DEFAULT_NAMESPACE = "default"

# Kinds we treat as definitions worth linking to. Everything else in the
# rendered stream is still parsed (it may hold references) but doesn't get its
# own definition symbol unless it's here.
_DEFINITION_KINDS = frozenset({
    "ServiceAccount", "Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding",
    "Secret", "ConfigMap", "Service", "Deployment", "StatefulSet", "DaemonSet",
    "Job", "CronJob", "Pod", "Ingress", "NetworkPolicy", "ReplicaSet",
})


def _binary_exists(name: str) -> bool:
    return shutil.which(name) is not None


def _descriptor(namespace: str, kind: str, name: str) -> str:
    """SCIP descriptor chain for a k8s object: <ns>/<Kind>/<name>#."""
    ns = namespace or _DEFAULT_NAMESPACE
    return f"{ns}/{kind}/{name}#"


def _symbol_for(namespace: str, kind: str, name: str) -> str:
    return make_symbol(_descriptor(namespace, kind, name))


# --- render -----------------------------------------------------------------

def _needs_remote_pull(kustomization: Path) -> bool:
    """True if a kustomization.yaml pulls a chart from a REMOTE helm repo
    (helmCharts[].repo). v1 is local-only render (no private-registry auth in
    the sandbox), so these roots are skipped with a Phase-1.5 note rather than
    attempted-and-failed against the authenticated Nexus repo."""
    try:
        doc = yaml.safe_load(kustomization.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return False
    for hc in (doc.get("helmCharts") or []):
        if isinstance(hc, dict) and hc.get("repo"):
            return True
    return False


def _discover_render_roots(target: Path) -> list[tuple[Path, str]]:
    """Find directories to render. Returns (dir, kind) where kind is
    'kustomize' or 'helm'. Prefers kustomize when a dir has both (kustomize
    drives helm via helmCharts: in the common layout).

    v1 LOCAL-ONLY: kustomizations that pull a chart from a remote helm repo are
    skipped — resolving them needs private-registry (Nexus) auth in the
    sandbox, deferred to Phase 1.5. Self-contained charts/kustomizations render
    fully."""
    roots: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    remote_skipped = 0
    for kfile in sorted(target.rglob("kustomization.yaml")):
        d = kfile.parent
        if d in seen:
            continue
        seen.add(d)
        if _needs_remote_pull(kfile):
            remote_skipped += 1
            continue
        roots.append((d, "kustomize"))
    for cfile in sorted(target.rglob("Chart.yaml")):
        d = cfile.parent
        # only standalone charts with their own templates/ (not subcharts
        # pulled in by a parent) and not already covered by a kustomize root
        if d not in seen and (d / "templates").is_dir():
            roots.append((d, "helm"))
            seen.add(d)
    if remote_skipped:
        logger.info(
            "scip-k8s: skipped %d kustomization(s) that pull remote helm charts "
            "(v1 is local-only render; private-registry resolution is Phase 1.5)",
            remote_skipped,
        )
    return roots


def _render(root: Path, kind: str) -> str | None:
    """Render one root to a concrete multi-document YAML string, or None."""
    if kind == "kustomize":
        cmd = ["kustomize", "build", "--enable-helm", str(root)]
    else:
        cmd = ["helm", "template", str(root)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_RENDER_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("scip-k8s: render failed for %s (%s): %s", root, kind, exc)
        return None
    if proc.returncode != 0:
        logger.warning(
            "scip-k8s: %s build returned %d for %s: %s",
            kind, proc.returncode, root, (proc.stderr or "").strip()[:300],
        )
        return None
    return proc.stdout


def _load_objects(rendered: str) -> list[dict[str, Any]]:
    """Parse a rendered multi-doc YAML into a list of k8s object dicts."""
    objs: list[dict[str, Any]] = []
    try:
        docs = yaml.safe_load_all(rendered)
        for doc in docs:
            if isinstance(doc, dict) and doc.get("kind") and doc.get("metadata"):
                objs.append(doc)
    except yaml.YAMLError as exc:
        logger.warning("scip-k8s: YAML parse error in rendered output: %s", exc)
    return objs


# --- reference extraction ----------------------------------------------------

def _obj_ns(obj: dict[str, Any]) -> str:
    return (obj.get("metadata") or {}).get("namespace") or _DEFAULT_NAMESPACE


def _obj_name(obj: dict[str, Any]) -> str:
    return (obj.get("metadata") or {}).get("name") or ""


def _pod_spec(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Return the PodSpec for any workload kind, or None."""
    kind = obj.get("kind")
    spec = obj.get("spec") or {}
    if kind == "Pod":
        return spec
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"):
        return ((spec.get("template") or {}).get("spec")) or None
    if kind == "CronJob":
        return (((spec.get("jobTemplate") or {}).get("spec") or {})
                .get("template") or {}).get("spec") or None
    return None


def _iter_secret_refs(pod_spec: dict[str, Any]) -> Iterable[str]:
    """Secret names referenced by a PodSpec: volumes, envFrom, env valueFrom."""
    for vol in pod_spec.get("volumes") or []:
        sec = (vol or {}).get("secret") or {}
        if sec.get("secretName"):
            yield sec["secretName"]
    containers = (pod_spec.get("containers") or []) + \
        (pod_spec.get("initContainers") or [])
    for c in containers:
        for ef in (c or {}).get("envFrom") or []:
            sref = (ef or {}).get("secretRef") or {}
            if sref.get("name"):
                yield sref["name"]
        for env in (c or {}).get("env") or []:
            skr = ((env or {}).get("valueFrom") or {}).get("secretKeyRef") or {}
            if skr.get("name"):
                yield skr["name"]


def _references(obj: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Cross-object references from `obj`, as (target_kind, target_name,
    target_namespace) triples. By-name only (v1)."""
    refs: list[tuple[str, str, str]] = []
    ns = _obj_ns(obj)
    kind = obj.get("kind")

    pod_spec = _pod_spec(obj)
    if pod_spec:
        sa = pod_spec.get("serviceAccountName") or pod_spec.get("serviceAccount")
        if sa:
            refs.append(("ServiceAccount", sa, ns))
        for sec in _iter_secret_refs(pod_spec):
            refs.append(("Secret", sec, ns))

    if kind in ("RoleBinding", "ClusterRoleBinding"):
        role_ref = obj.get("roleRef") or {}
        if role_ref.get("name"):
            # ClusterRole refs are cluster-scoped (namespace-less); model them
            # in the default namespace so the symbol is stable.
            rk = role_ref.get("kind") or "Role"
            r_ns = ns if rk == "Role" else _DEFAULT_NAMESPACE
            refs.append((rk, role_ref["name"], r_ns))
        for subj in obj.get("subjects") or []:
            if (subj or {}).get("kind") == "ServiceAccount" and subj.get("name"):
                s_ns = subj.get("namespace") or ns
                refs.append(("ServiceAccount", subj["name"], s_ns))

    if kind == "Ingress":
        for rule in (obj.get("spec") or {}).get("rules") or []:
            http = (rule or {}).get("http") or {}
            for path in http.get("paths") or []:
                svc = (((path or {}).get("backend") or {}).get("service")) or {}
                if svc.get("name"):
                    refs.append(("Service", svc["name"], ns))
        default_backend = (obj.get("spec") or {}).get("defaultBackend") or {}
        dsvc = default_backend.get("service") or {}
        if dsvc.get("name"):
            refs.append(("Service", dsvc["name"], ns))

    return refs


# --- indexing ----------------------------------------------------------------

def index(target: Path, out_dir: Path) -> Path | None:
    """Build a k8s SCIP index for `target`; return the .scip path or None."""
    roots = _discover_render_roots(target)
    if not roots:
        return None
    if not (_binary_exists("kustomize") or _binary_exists("helm")):
        logger.warning("scip-k8s: neither kustomize nor helm on PATH; skipping")
        return None
    if len(roots) > _MAX_RENDER_ROOTS:
        logger.warning(
            "scip-k8s: %d render roots exceeds cap %d; indexing first %d "
            "(rest skipped — narrow the scan target for full coverage)",
            len(roots), _MAX_RENDER_ROOTS, _MAX_RENDER_ROOTS,
        )
        roots = roots[:_MAX_RENDER_ROOTS]

    # A rendered stream loses per-object source files, so we key documents by
    # render root (relative to target) with a synthetic .rendered.yaml path.
    # pass 1: definitions; pass 2: references. Both keyed to the same doc.
    definitions: dict[str, tuple[str, str, str]] = {}  # symbol -> (ns,kind,name)
    per_doc_defs: dict[str, list[Occurrence]] = {}
    per_doc_refs: dict[str, list[tuple[str, str, str, int]]] = {}
    doc_objects: dict[str, list[dict[str, Any]]] = {}

    any_rendered = False
    for root, rkind in roots:
        rendered = _render(root, rkind)
        if not rendered:
            continue
        objs = _load_objects(rendered)
        if not objs:
            continue
        any_rendered = True
        rel = root.relative_to(target).as_posix() if root != target else "."
        doc_path = f"{rel}/__rendered__.yaml"
        doc_objects[doc_path] = objs

    if not any_rendered:
        return None

    # pass 1 — definitions. Line numbers are synthetic (index within the doc's
    # object list); precise line mapping in the rendered stream is Phase 2.
    for doc_path, objs in doc_objects.items():
        defs: list[Occurrence] = []
        for i, obj in enumerate(objs):
            kind = obj.get("kind")
            name = _obj_name(obj)
            if kind not in _DEFINITION_KINDS or not name:
                continue
            ns = _obj_ns(obj)
            sym = _symbol_for(ns, kind, name)
            definitions.setdefault(sym, (ns, kind, name))
            defs.append(Occurrence(sym, i, 0, 0, is_definition=True))
        per_doc_defs[doc_path] = defs

    # pass 2 — references. Only emit a ref if its target resolves to a known
    # definition (an unresolved ref is noise — e.g. a SA in another repo).
    for doc_path, objs in doc_objects.items():
        refs: list[Occurrence] = []
        for i, obj in enumerate(objs):
            for (tk, tn, tns) in _references(obj):
                sym = _symbol_for(tns, tk, tn)
                if sym in definitions:
                    refs.append(Occurrence(sym, i, 0, 0, is_definition=False))
        per_doc_refs[doc_path] = refs

    documents: list[Document] = []
    for doc_path in doc_objects:
        defs = per_doc_defs.get(doc_path, [])
        refs = per_doc_refs.get(doc_path, [])
        if not defs and not refs:
            continue
        documents.append(Document(
            relative_path=doc_path,
            occurrences=defs + refs,
            defined_symbols={o.symbol for o in defs},
        ))

    if not documents:
        return None

    out_path = out_dir / "k8s.scip"
    return write_index(build_index(documents, project_root=target), out_path)
