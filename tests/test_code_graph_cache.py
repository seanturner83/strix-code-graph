"""Unit tests for the code_graph cache abstraction (SEC-6848 W1.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from strix_code_graph.cache import (
    CacheKey,
    FilesystemCache,
    NullCache,
    from_env,
)

# ---------------------------------------------------------------------------
# CacheKey validation
# ---------------------------------------------------------------------------


def test_cache_key_round_trip() -> None:
    key = CacheKey(repo="seedcx/portal-api", head_sha="d2936abc1234567890abcdef1234567890abcdef")
    assert key.relative_path == (
        "seedcx/portal-api/d2936abc1234567890abcdef1234567890abcdef.sqlite"
    )


def test_cache_key_rejects_path_traversal_in_sha() -> None:
    with pytest.raises(ValueError, match="invalid head_sha"):
        CacheKey(repo="seedcx/portal-api", head_sha="..\\evil")


def test_cache_key_rejects_slash_in_sha() -> None:
    with pytest.raises(ValueError, match="invalid head_sha"):
        CacheKey(repo="seedcx/portal-api", head_sha="abc/def0123")


def test_cache_key_rejects_path_traversal_in_repo() -> None:
    with pytest.raises(ValueError, match="invalid repo"):
        CacheKey(repo="seedcx/../etc", head_sha="d2936abc1")


def test_cache_key_rejects_short_sha() -> None:
    with pytest.raises(ValueError, match="head_sha too short"):
        CacheKey(repo="seedcx/x", head_sha="abc")


# ---------------------------------------------------------------------------
# NullCache
# ---------------------------------------------------------------------------


def test_null_cache_always_misses(tmp_path: Path) -> None:
    cache = NullCache()
    key = CacheKey(repo="r/x", head_sha="abcdef0123")
    dest = tmp_path / "out.sqlite"
    assert cache.get(key, dest) is False
    assert not dest.exists()


def test_null_cache_put_is_noop(tmp_path: Path) -> None:
    src = tmp_path / "in.sqlite"
    src.write_bytes(b"x" * 32)
    NullCache().put(CacheKey(repo="r/x", head_sha="abcdef0123"), src)
    # No exception, no extra files written.
    assert list(tmp_path.iterdir()) == [src]


# ---------------------------------------------------------------------------
# FilesystemCache
# ---------------------------------------------------------------------------


def _make_index(path: Path, payload: bytes = b"SCIP-SQLITE-FAKE") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def test_filesystem_cache_miss_then_hit(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    cache = FilesystemCache(cache_root)
    key = CacheKey(repo="seedcx/portal-api", head_sha="d2936abc1234")

    # First get → miss.
    dest1 = workspace / "first.sqlite"
    assert cache.get(key, dest1) is False
    assert not dest1.exists()

    # Build + put.
    built = workspace / "built.sqlite"
    _make_index(built, b"index-v1")
    cache.put(key, built)

    # Second get on a fresh dest → hit, bytes match.
    dest2 = workspace / "second.sqlite"
    assert cache.get(key, dest2) is True
    assert dest2.read_bytes() == b"index-v1"


def test_filesystem_cache_segregates_by_sha(tmp_path: Path) -> None:
    cache = FilesystemCache(tmp_path / "cache")
    key_a = CacheKey(repo="seedcx/x", head_sha="a" * 40)
    key_b = CacheKey(repo="seedcx/x", head_sha="b" * 40)

    src_a = tmp_path / "a.sqlite"
    src_b = tmp_path / "b.sqlite"
    _make_index(src_a, b"AAAA")
    _make_index(src_b, b"BBBB")
    cache.put(key_a, src_a)
    cache.put(key_b, src_b)

    out_a = tmp_path / "out_a.sqlite"
    out_b = tmp_path / "out_b.sqlite"
    assert cache.get(key_a, out_a) is True
    assert cache.get(key_b, out_b) is True
    assert out_a.read_bytes() == b"AAAA"
    assert out_b.read_bytes() == b"BBBB"


def test_filesystem_cache_segregates_by_repo(tmp_path: Path) -> None:
    cache = FilesystemCache(tmp_path / "cache")
    key_x = CacheKey(repo="seedcx/portal-api", head_sha="d" * 40)
    key_y = CacheKey(repo="seedcx/trade-api", head_sha="d" * 40)

    src_x = tmp_path / "x.sqlite"
    src_y = tmp_path / "y.sqlite"
    _make_index(src_x, b"PORTAL")
    _make_index(src_y, b"TRADE")
    cache.put(key_x, src_x)
    cache.put(key_y, src_y)

    out = tmp_path / "out.sqlite"
    cache.get(key_x, out)
    assert out.read_bytes() == b"PORTAL"
    cache.get(key_y, out)
    assert out.read_bytes() == b"TRADE"


def test_filesystem_cache_put_missing_source_does_not_raise(tmp_path: Path) -> None:
    cache = FilesystemCache(tmp_path / "cache")
    key = CacheKey(repo="r/x", head_sha="abcdef0123")
    # Source doesn't exist — put must not raise.
    cache.put(key, tmp_path / "does-not-exist.sqlite")
    # And subsequent get is still a miss.
    out = tmp_path / "out.sqlite"
    assert cache.get(key, out) is False


# ---------------------------------------------------------------------------
# from_env selector
# ---------------------------------------------------------------------------


def test_from_env_returns_null_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRIX_CODE_GRAPH_CACHE_DIR", raising=False)
    assert isinstance(from_env(), NullCache)


def test_from_env_returns_filesystem_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STRIX_CODE_GRAPH_CACHE_DIR", str(tmp_path / "cache"))
    cache = from_env()
    assert isinstance(cache, FilesystemCache)
    assert (tmp_path / "cache").exists()


def test_from_env_empty_string_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # docker_runtime sets STRIX_CODE_GRAPH_CACHE_DIR="" when host env is
    # unset; the indexer subprocess should see that as "no cache", not
    # a FilesystemCache rooted at the empty string.
    monkeypatch.setenv("STRIX_CODE_GRAPH_CACHE_DIR", "")
    assert isinstance(from_env(), NullCache)
