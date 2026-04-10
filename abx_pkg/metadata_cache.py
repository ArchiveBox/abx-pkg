"""Persistent on-disk cache for binary resolution metadata.

Modern package managers (uv, pnpm, yarn) achieve speed by avoiding
redundant work.  The most expensive part of ``BinProvider.load()`` is
shelling out to ``binary --version`` (typically ~100ms per binary).
For a project that manages 20+ binaries, that's 2+ seconds on every
cold start.

This module provides a lightweight JSON-backed cache that persists
resolved binary metadata (abspath, version, sha256) across process
invocations.  Entries are validated by checking the binary file's
mtime and size — if the file hasn't changed on disk, the cached
version/sha256 are still valid.

Usage::

    from abx_pkg.metadata_cache import metadata_cache

    # Check cache before expensive --version call
    entry = metadata_cache.get("pip", "yt-dlp", install_root)
    if entry:
        abspath, version, sha256 = entry

    # Store after successful resolution
    metadata_cache.set("pip", "yt-dlp", install_root, abspath, version, sha256)

    # Invalidate after install/update/uninstall
    metadata_cache.invalidate("pip", "yt-dlp", install_root)
"""

__package__ = "abx_pkg"

import json
import os
import time
from pathlib import Path
from typing import Any

from .base_types import ABX_PKG_CACHE_DIR

_CACHE_FILE = ABX_PKG_CACHE_DIR / "metadata_cache.json"

# Don't persist entries older than 7 days without revalidation
_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _cache_key(provider_name: str, bin_name: str, install_root: Path | None) -> str:
    return f"{provider_name}:{bin_name}:{install_root or 'global'}"


def _file_fingerprint(abspath: Path | str) -> tuple[float, int] | None:
    """Return (mtime, size) for a binary, or None if it doesn't exist."""
    try:
        st = os.stat(str(abspath))
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


class BinaryMetadataCache:
    """JSON-backed persistent cache for binary resolution results.

    Thread-safety: not thread-safe.  Each process gets its own instance.
    Concurrent processes may race on the JSON file, but the worst case
    is a stale read or a lost write — both are harmless because the
    cache is purely advisory (callers always fall back to live resolution).
    """

    def __init__(self, cache_file: Path = _CACHE_FILE) -> None:
        self._cache_file = cache_file
        self._data: dict[str, Any] | None = None
        self._dirty = False

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        try:
            self._data = json.loads(self._cache_file.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            self._data = {}
        return self._data

    def _save(self) -> None:
        if not self._dirty:
            return
        data = self._load()
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str))
            tmp.replace(self._cache_file)
            self._dirty = False
        except OSError:
            pass

    def get(
        self,
        provider_name: str,
        bin_name: str,
        install_root: Path | None = None,
    ) -> tuple[Path, str, str] | None:
        """Look up cached (abspath, version, sha256) for a binary.

        Returns ``None`` if:
        - No cache entry exists
        - The binary file no longer exists at the cached path
        - The binary's mtime or size has changed (stale entry)
        - The entry has expired (older than _MAX_AGE_SECONDS)
        """
        data = self._load()
        key = _cache_key(provider_name, bin_name, install_root)
        entry = data.get(key)
        if not entry:
            return None

        abspath = entry.get("abspath")
        if not abspath:
            return None

        # Check age
        cached_at = entry.get("cached_at", 0)
        if time.time() - cached_at > _MAX_AGE_SECONDS:
            return None

        # Validate fingerprint (mtime + size)
        fp = _file_fingerprint(abspath)
        if fp is None:
            return None
        cached_mtime = entry.get("mtime")
        cached_size = entry.get("size")
        if cached_mtime is None or cached_size is None:
            return None
        if abs(fp[0] - cached_mtime) > 0.01 or fp[1] != cached_size:
            return None

        version = entry.get("version")
        sha256 = entry.get("sha256", "unknown")
        if not version:
            return None

        return (Path(abspath), version, sha256)

    def set(
        self,
        provider_name: str,
        bin_name: str,
        install_root: Path | None,
        abspath: Path | str,
        version: str,
        sha256: str = "unknown",
    ) -> None:
        """Store resolved binary metadata in the persistent cache."""
        data = self._load()
        key = _cache_key(provider_name, bin_name, install_root)

        fp = _file_fingerprint(abspath)
        if fp is None:
            return

        data[key] = {
            "abspath": str(abspath),
            "version": str(version),
            "sha256": sha256,
            "mtime": fp[0],
            "size": fp[1],
            "cached_at": time.time(),
        }
        self._dirty = True
        self._save()

    def invalidate(
        self,
        provider_name: str,
        bin_name: str,
        install_root: Path | None = None,
    ) -> None:
        """Remove a specific entry from the cache."""
        data = self._load()
        key = _cache_key(provider_name, bin_name, install_root)
        if key in data:
            del data[key]
            self._dirty = True
            self._save()

    def invalidate_provider(self, provider_name: str) -> None:
        """Remove all entries for a given provider."""
        data = self._load()
        prefix = f"{provider_name}:"
        keys_to_remove = [k for k in data if k.startswith(prefix)]
        for key in keys_to_remove:
            del data[key]
        if keys_to_remove:
            self._dirty = True
            self._save()

    def clear(self) -> None:
        """Remove all cached entries."""
        self._data = {}
        self._dirty = True
        self._save()

    def prune_stale(self) -> int:
        """Remove entries whose binaries no longer exist or have changed.

        Returns the number of entries removed.
        """
        data = self._load()
        stale_keys: list[str] = []
        now = time.time()

        for key, entry in data.items():
            abspath = entry.get("abspath")
            if not abspath:
                stale_keys.append(key)
                continue

            # Expired
            if now - entry.get("cached_at", 0) > _MAX_AGE_SECONDS:
                stale_keys.append(key)
                continue

            # File gone or changed
            fp = _file_fingerprint(abspath)
            if fp is None:
                stale_keys.append(key)
                continue
            cached_mtime = entry.get("mtime")
            cached_size = entry.get("size")
            if cached_mtime is None or cached_size is None:
                stale_keys.append(key)
                continue
            if abs(fp[0] - cached_mtime) > 0.01 or fp[1] != cached_size:
                stale_keys.append(key)

        for key in stale_keys:
            del data[key]
        if stale_keys:
            self._dirty = True
            self._save()
        return len(stale_keys)


# Module-level singleton. Providers import and use this directly.
metadata_cache = BinaryMetadataCache()
