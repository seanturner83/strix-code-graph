"""Cache abstraction for code-graph indexes.

The Strix runtime stays AWS-free: the cache is a Protocol with a
filesystem default, and the GHA workflow that wraps Strix can
optionally hydrate the filesystem cache from S3 before invoking
Strix and sync back after. This keeps boto3 out of the sandbox
image and the runtime process while still giving us "skip
re-indexing unchanged repos" for PR-time scans.

Cache key:
    (repo, head_sha) → SQLite path
where `repo` is "owner/name" and `head_sha` is the full 40-char
commit SHA. Two scans of the same HEAD must hit the cache; a
new HEAD must miss it.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheKey:
    repo: str
    head_sha: str

    def __post_init__(self) -> None:
        # Defensive: keep the path components clean. The repo name is taken
        # from the dispatcher input and the SHA from git; both should be
        # well-formed, but guard against path traversal anyway.
        if "/" in self.head_sha or ".." in self.head_sha:
            raise ValueError(f"invalid head_sha: {self.head_sha!r}")
        if ".." in self.repo:
            raise ValueError(f"invalid repo: {self.repo!r}")
        if len(self.head_sha) < 7:
            raise ValueError(f"head_sha too short to be useful: {self.head_sha!r}")

    @property
    def relative_path(self) -> str:
        # owner/name/<sha>.sqlite — flat enough to S3-sync, deep enough
        # to avoid collisions across repos.
        return f"{self.repo}/{self.head_sha}.sqlite"


class CodeGraphCache(Protocol):
    def get(self, key: CacheKey, dest: Path) -> bool:
        """Copy cached SQLite to dest, returning True on hit."""

    def put(self, key: CacheKey, source: Path) -> None:
        """Store source SQLite under key. Failures must not raise."""


class NullCache:
    """No-op cache; every get is a miss, every put is dropped. Safe default."""

    def get(self, key: CacheKey, dest: Path) -> bool:
        return False

    def put(self, key: CacheKey, source: Path) -> None:
        return


class FilesystemCache:
    """Cache rooted at a host directory. The GHA workflow can sync this
    directory from S3 before Strix starts and push it back after, giving
    us cross-run caching without bringing boto3 into the runtime."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: CacheKey) -> Path:
        return self.root / key.relative_path

    def get(self, key: CacheKey, dest: Path) -> bool:
        src = self._path_for(key)
        if not src.exists():
            return False
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            logger.info(
                "code_graph cache HIT: %s/%s → %s",
                key.repo,
                key.head_sha[:8],
                dest,
            )
        except OSError as exc:
            logger.warning("code_graph cache get failed: %s", exc)
            return False
        return True

    def put(self, key: CacheKey, source: Path) -> None:
        if not source.exists():
            logger.warning("code_graph cache put: source missing %s", source)
            return
        dst = self._path_for(key)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dst)
            logger.info(
                "code_graph cache PUT: %s → %s/%s",
                source,
                key.repo,
                key.head_sha[:8],
            )
        except OSError as exc:
            # Cache failures must never break the scan.
            logger.warning("code_graph cache put failed: %s", exc)


def from_env() -> CodeGraphCache:
    """Pick a cache impl from environment:

      STRIX_CODE_GRAPH_CACHE_DIR=/some/path → FilesystemCache
      (unset)                               → NullCache

    The GHA workflow is responsible for setting STRIX_CODE_GRAPH_CACHE_DIR
    to a path it will sync to/from S3 around the scan run.
    """
    root = os.environ.get("STRIX_CODE_GRAPH_CACHE_DIR")
    if root:
        return FilesystemCache(Path(root))
    return NullCache()
