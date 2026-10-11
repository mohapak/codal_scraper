"""
Caching layer for Codal Scraper

This module provides file-based caching for API responses to reduce
redundant requests and improve performance.
"""

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .exceptions import CacheError


logger = logging.getLogger(__name__)

#: On-disk record schema version. Bumped when the record shape changes so a
#: reader can tell entries written by another version apart.
CACHE_RECORD_VERSION = 2

#: Marker written into every record this cache creates. Only records carrying
#: it are ever served, replaced or deleted -- see :meth:`FileCache.invalidate`
#: for why that matters on a user-supplied directory.
CACHE_OWNER = "codal_scraper.FileCache"


@dataclass
class CacheConfig:
    """
    Configuration for the cache system.
    
    Attributes:
        cache_dir: Directory to store cache files
        default_ttl: Default time-to-live in seconds
        max_size_mb: Maximum cache size in megabytes
        enabled: Whether caching is enabled
        cleanup_on_init: Whether to cleanup expired entries on initialization
    """
    cache_dir: str = ".codal_cache"
    default_ttl: int = 3600  # 1 hour
    max_size_mb: int = 100
    enabled: bool = True
    cleanup_on_init: bool = True


class FileCache:
    """
    Simple file-based cache for API responses.
    
    This cache stores responses as JSON files with metadata about
    expiration times. It's suitable for caching API responses that
    don't change frequently.
    
    Example:
        >>> cache = FileCache()
        >>> cache.set("https://api.example.com/data", {"key": "value"})
        >>> data = cache.get("https://api.example.com/data")
        >>> print(data)
        {'key': 'value'}
    """
    
    def __init__(self, config: Optional[CacheConfig] = None):
        """
        Initialize the file cache.
        
        Args:
            config: Cache configuration (uses defaults if None)
        """
        self.config = config or CacheConfig()
        self.cache_dir = Path(self.config.cache_dir)
        self._stats = {
            'hits': 0,
            'misses': 0,
            'writes': 0,
            'errors': 0
        }
        
        # Why an entry that exists on disk was not served, counted per reason,
        # so an operator can see that the cache is protecting the dataset
        # rather than silently feeding it stale data.
        self._refusals: Dict[str, int] = {}
        # One lock per cache key. Two workers of the same client (threads, or
        # asyncio tasks) must not interleave writes to the same entry.
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

        if self.config.enabled:
            self._initialize_cache_dir()
            if self.config.cleanup_on_init:
                self.cleanup()
    
    def _initialize_cache_dir(self) -> None:
        """Create cache directory if it doesn't exist"""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"Failed to create cache directory: {e}")
            self.config.enabled = False
    
    def _get_cache_key(self, url: str, params: Optional[Dict] = None) -> str:
        """
        Generate a unique cache key from URL and params.
        
        Args:
            url: Request URL
            params: Request parameters
            
        Returns:
            MD5 hash as cache key
        """
        key_data = url
        if params:
            # Sort params for consistent hashing
            sorted_params = json.dumps(params, sort_keys=True)
            key_data += sorted_params
        
        return hashlib.md5(key_data.encode('utf-8')).hexdigest()
    
    def _get_cache_path(self, key: str) -> Path:
        """Get file path for a cache key"""
        return self.cache_dir / f"{key}.json"

    # ============== record integrity ==============

    def _get_lock(self, key: str) -> threading.Lock:
        """Return the lock guarding one cache key."""
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _record_refusal(self, key: str, reason: str, path: Optional[Path] = None) -> None:
        """Count and log a refusal to serve an entry that exists on disk."""
        self._refusals[reason] = self._refusals.get(reason, 0) + 1
        log = logger.debug if reason in ('expired', 'miss') else logger.warning
        log(f"Cache entry {key} not served ({reason}): {path}")

    def _read_record(self, path: Path) -> Optional[Dict[str, Any]]:
        """Read a JSON record, or None if the file is missing or not a dict."""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                record = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        except OSError:
            return None
        return record if isinstance(record, dict) else None

    def _is_owned(self, record: Dict[str, Any]) -> bool:
        """True when this cache wrote the record (and may therefore touch it)."""
        return record.get('owner') == CACHE_OWNER

    def _owned_cache_files(self) -> list:
        """Cache files this cache wrote -- the only ones it may delete."""
        owned = []
        for path in self.cache_dir.glob("*.json"):
            record = self._read_record(path)
            if record is not None and self._is_owned(record):
                owned.append(path)
        return owned

    def _validate(self, record: Dict[str, Any]) -> Optional[str]:
        """
        Decide whether a stored record may be served.

        Returns None when the record is trustworthy, otherwise the reason it is
        not. This is the check that stops a stale or partial scrape being served
        as fresh: the metadata recorded at write time (HTTP status, the API's
        `Total`, ``len(Letters)``, the item count the page was expected to hold
        and the harvest provenance) is compared against what the payload
        actually holds.
        """
        if not self._is_owned(record):
            # Written by something other than this cache (an older version, or
            # a file that merely happens to live in the directory).
            return 'untrusted'
        if 'data' not in record or 'expires_at' not in record:
            return 'invalid'
        if record.get('version') != CACHE_RECORD_VERSION:
            return 'stale_format'
        if time.time() >= record.get('expires_at', 0):
            # `ttl=0` lands here by design: "expire now".
            return 'expired'
        meta = record.get('meta') or {}
        if meta.get('complete') is False:
            # A scrape that did not collect everything it should have.
            return 'incomplete'
        if meta.get('status') not in (None, 200):
            return 'http_status'
        data = record.get('data')
        if isinstance(data, dict) and meta.get('letters') is not None:
            letters = data.get('Letters')
            letters = len(letters) if isinstance(letters, list) else None
            if letters is None:
                return 'payload_changed'
            if letters != meta['letters']:
                return 'letters_mismatch'
            total = data.get('Total')
            if meta.get('total') is not None and total is not None and total != meta['total']:
                return 'total_mismatch'
            if total is not None and total < letters:
                return 'total_mismatch'
            if meta.get('expected') is not None and letters < meta['expected']:
                return 'truncated'
        if self._harvest_incomplete(meta):
            return 'incomplete_harvest'
        return None

    # ============== harvest provenance ==============

    def _harvest_path(self, query: str) -> Path:
        """Where the provenance of a query's last harvest is recorded."""
        key = self._get_cache_key('codal_scraper:harvest', {'query': query})
        return self._get_cache_path(key)

    def _harvest_incomplete(self, meta: Dict[str, Any]) -> bool:
        """True when the harvest this page belongs to is recorded incomplete."""
        query = meta.get('query')
        if not query:
            return False
        record = self._read_record(self._harvest_path(query))
        if record is None or not self._is_owned(record):
            return False
        provenance = record.get('data')
        return isinstance(provenance, dict) and provenance.get('complete') is False

    def record_harvest(self, query: str, provenance: Dict[str, Any]) -> bool:
        """
        Record what a harvest of `query` collected.

        Page entries cached while harvesting `query` are refused from then on if
        the harvest is recorded incomplete, so a partial crawl can never be
        handed back as complete on a later run.
        """
        if not query:
            return False
        now = time.time()
        record = {
            'version': CACHE_RECORD_VERSION,
            'owner': CACHE_OWNER,
            'kind': 'harvest',
            'url': query,
            'params': None,
            'data': dict(provenance),
            'created_at': now,
            'expires_at': now + self.config.default_ttl,
            'meta': {},
        }
        return self._write_record(self._harvest_path(query), record)

    def harvest_provenance(self, query: str) -> Optional[Dict[str, Any]]:
        """Return the recorded harvest provenance for a query, if any."""
        record = self._read_record(self._harvest_path(query))
        if record is None or not self._is_owned(record):
            return None
        return record.get('data')

    # ============== writing ==============

    def _write_record(self, path: Path, record: Dict[str, Any]) -> bool:
        """
        Write a record atomically, under a per-key lock.

        `tempfile` + `os.replace` means a reader (or a kill, or a full disk)
        never observes a half-written entry: the bytes are either the previous
        record or the complete new one. Files are written inside the cache
        directory and only under the name the cache owns.
        """
        try:
            payload = json.dumps(record, ensure_ascii=False, indent=None)
        except (TypeError, ValueError) as e:
            logger.warning(f"Cache write error (serialisation): {e}")
            self._stats['errors'] += 1
            return False

        with self._get_lock(path.name):
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(self.cache_dir), prefix=f"{path.name}.", suffix=".tmp"
                )
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
                tmp_path = None
                return True
            except OSError as e:
                logger.warning(f"Cache write error: {e}")
                self._stats['errors'] += 1
                return False
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
    
    def get(
        self, 
        url: str, 
        params: Optional[Dict] = None
    ) -> Optional[Any]:
        """
        Get cached value if it exists and is not expired.
        
        Args:
            url: Request URL
            params: Request parameters
            
        Returns:
            Cached data, or None if the entry is missing, expired, or was
            written by a scrape this cache cannot vouch for (a non-200 status,
            a `Letters` count that disagrees with what was recorded, a
            truncated page, or a harvest recorded as incomplete). None always
            means "fetch it": a refusal never raises at the caller.
        """
        if not self.config.enabled:
            return None
        
        key = self._get_cache_key(url, params)
        cache_path = self._get_cache_path(key)
        
        if not cache_path.exists():
            self._stats['misses'] += 1
            return None
        
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            
            # Check expiration and integrity: refuse anything this cache cannot
            # vouch for (see _validate). A refusal returns None, so the caller
            # refetches -- it never raises into the caller's face.
            reason = self._validate(cached)
            if reason is not None:
                self._record_refusal(key, reason, cache_path)
                if reason == 'expired':
                    self._delete_cache_file(cache_path)
                self._stats['misses'] += 1
                return None
            
            self._stats['hits'] += 1
            logger.debug(f"Cache hit for {url}")
            return cached.get('data')
            
        except json.JSONDecodeError as e:
            # Not deleted: a truncated or foreign file is not this cache's to
            # remove. Refusing it is enough -- the caller refetches and the
            # atomic write replaces the entry by key.
            logger.warning(f"Cache file unreadable, refusing it: {cache_path} - {e}")
            self._record_refusal(key, 'corrupt', cache_path)
            self._stats['errors'] += 1
            return None
        except IOError as e:
            logger.warning(f"Cache read error: {e}")
            self._stats['errors'] += 1
            return None
    
    def set(
        self, 
        url: str, 
        data: Any, 
        params: Optional[Dict] = None, 
        ttl: Optional[int] = None,
        *,
        status: Optional[int] = None,
        total: Optional[int] = None,
        letters: Optional[int] = None,
        expected: Optional[int] = None,
        complete: Optional[bool] = None,
        query: Optional[str] = None,
        harvest: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Store value in cache.
        
        Args:
            url: Request URL
            data: Data to cache
            params: Request parameters
            ttl: Time-to-live in seconds. None uses `default_ttl`; 0 writes an
                entry that is already expired ("expire now", so it is never
                served); a negative value writes nothing at all ("do not
                cache").
            status: HTTP status the payload came from. A non-200 entry is never
                served.
            total: the API's `Total` for the query.
            letters: how many items the payload's `Letters` held when written
                (derived from `data` when omitted).
            expected: how many items this page should have held. A payload
                holding fewer is recorded as incomplete and never served.
            complete: explicit completeness. False marks an entry that must
                never be served as complete.
            query: the query URL this page belongs to, so the entry can be tied
                to a harvest record.
            harvest: harvest provenance (PR #3 `PaginationResult.provenance()`).

        Returns:
            True if successful, False otherwise
        """
        if not self.config.enabled:
            return False
        
        if ttl is not None and ttl < 0:
            logger.debug(f"ttl={ttl} for {url}: not caching by request")
            return False
        # `ttl or default_ttl` used to swallow `ttl=0`, making "expire now"
        # inexpressible: 0 silently became 3600 s. None means "use the
        # configured default"; 0 means "expired the moment it is written".
        ttl = self.config.default_ttl if ttl is None else ttl

        key = self._get_cache_key(url, params)
        cache_path = self._get_cache_path(key)
        now = time.time()

        # Record what makes the payload trustworthy, so a later read can refuse
        # it if the numbers no longer agree (see _validate).
        if letters is None and isinstance(data, dict) and isinstance(data.get('Letters'), list):
            letters = len(data['Letters'])
        if total is None and isinstance(data, dict) and isinstance(data.get('Total'), int):
            total = data['Total']
        if expected is not None and letters is not None and letters < expected:
            # This page arrived short of what it should hold. Cache the fact,
            # never the fiction: _validate refuses it, so the next run refetches.
            complete = False
            logger.warning(
                f"Refusing to treat {url} as complete: {letters} item(s) "
                f"arrived, {expected} expected; caching it marked incomplete"
            )
        elif complete is None and total is not None and letters is not None and letters >= total:
            complete = True
        
        try:
            cache_data = {
                'version': CACHE_RECORD_VERSION,
                'owner': CACHE_OWNER,
                'kind': 'page',
                'url': url,
                'params': params,
                'data': data,
                'created_at': now,
                'expires_at': now + ttl,
                'meta': {
                    'status': status,
                    'total': total,
                    'letters': letters,
                    'expected': expected,
                    'complete': complete,
                    'query': query,
                    'harvest': harvest,
                },
            }
            
            # Atomic write under a per-key lock: tempfile + os.replace, so a
            # reader never observes a half-written entry and two workers
            # cannot interleave writes to the same key.
            if not self._write_record(cache_path, cache_data):
                return False

            self._stats['writes'] += 1
            logger.debug(
                f"Cached {url} (status={status} total={total} "
                f"letters={letters} complete={complete})"
            )
            return True
        except (IOError, TypeError) as e:
            logger.warning(f"Cache write error: {e}")
            self._stats['errors'] += 1
            return False
    
    def invalidate(
        self, 
        url: Optional[str] = None, 
        params: Optional[Dict] = None,
        bulk: bool = False
    ) -> int:
        """
        Invalidate cache entries.
        
        Args:
            url: Specific URL to invalidate
            params: Request parameters for specific entry
            bulk: Required to mean "every entry". With no `url` and no
                `bulk=True` this raises rather than deleting everything, and
                even bulk deletion only removes entries this cache wrote.
            
        Returns:
            Number of entries invalidated

        Raises:
            ValueError: if neither `url` nor `bulk=True` is given.
        """
        if not self.config.enabled:
            return 0
        
        count = 0
        
        if url:
            key = self._get_cache_key(url, params)
            cache_path = self._get_cache_path(key)
            record = self._read_record(cache_path) if cache_path.exists() else None
            if record is not None and self._is_owned(record):
                if self._delete_cache_file(cache_path):
                    logger.info("Invalidated 1 cache entry")
                    return 1
            logger.debug(f"Nothing to invalidate for {url}")
            return 0

        if not bulk:
            raise ValueError(
                "invalidate() with no url used to delete every *.json in "
                f"{str(self.cache_dir)!r} -- on a user-supplied cache_dir that "
                "deletes files this cache never wrote. Pass bulk=True to mean "
                "'everything', or url=... / params=... for one entry."
            )

        for cache_file in self._owned_cache_files():
            if self._delete_cache_file(cache_file):
                count += 1
        logger.info(f"Invalidated {count} cache entries (bulk)")
        return count
    
    def cleanup(self) -> int:
        """
        Remove expired cache entries.
        
        Returns:
            Number of entries removed
        """
        if not self.config.enabled:
            return 0
        
        now = time.time()
        removed = 0
        
        for cache_file in self._owned_cache_files():
            record = self._read_record(cache_file)
            if record is None:
                # Unreadable, so ownership cannot be established: not ours to
                # delete. get() refuses it, which is what actually matters.
                logger.debug(f"Cache cleanup: skipping unreadable file {cache_file}")
                continue
            if now >= record.get('expires_at', 0):
                if self._delete_cache_file(cache_file):
                    removed += 1
        
        if removed > 0:
            logger.info(f"Cache cleanup: removed {removed} expired entries")
        
        return removed
    
    def _delete_cache_file(self, path: Path) -> bool:
        """
        Delete a cache file, but only inside this cache's own directory.

        `cache_dir` is user-supplied, so a path that resolves anywhere else is
        never unlinked: the cache deletes what it wrote, nothing more.
        """
        try:
            resolved = Path(path).resolve()
            if resolved.parent != self.cache_dir.resolve():
                logger.warning(
                    f"Refusing to delete {resolved}: outside cache_dir "
                    f"{self.cache_dir.resolve()}"
                )
                return False
            resolved.unlink()
            return True
        except OSError:
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.
        
        Returns:
            Dictionary with cache statistics
        """
        if not self.config.enabled:
            return {'enabled': False}
        
        cache_files = list(self.cache_dir.glob("*.json"))
        total_size = sum(f.stat().st_size for f in cache_files)
        
        hit_rate = 0
        total_requests = self._stats['hits'] + self._stats['misses']
        if total_requests > 0:
            hit_rate = self._stats['hits'] / total_requests * 100
        
        return {
            'enabled': True,
            'entries': len(cache_files),
            'size_mb': round(total_size / 1024 / 1024, 2),
            'max_size_mb': self.config.max_size_mb,
            'cache_dir': str(self.cache_dir),
            'default_ttl': self.config.default_ttl,
            'hits': self._stats['hits'],
            'misses': self._stats['misses'],
            'writes': self._stats['writes'],
            'errors': self._stats['errors'],
            'hit_rate_percent': round(hit_rate, 2),
            'refusals': dict(self._refusals)
        }
    
    def reset_stats(self) -> None:
        """Reset cache statistics"""
        self._stats = {
            'hits': 0,
            'misses': 0,
            'writes': 0,
            'errors': 0
        }
    
    def is_cached(self, url: str, params: Optional[Dict] = None) -> bool:
        """
        Check if a URL is cached without retrieving data.

        Args:
            url: Request URL
            params: Request parameters

        Returns:
            True only if an entry exists that would actually be served: the
            same integrity test as :meth:`get` (so an expired, truncated,
            incomplete or unowned entry is not "cached").
        """
        if not self.config.enabled:
            return False
        key = self._get_cache_key(url, params)
        cache_path = self._get_cache_path(key)
        record = self._read_record(cache_path) if cache_path.exists() else None
        if record is None:
            return False
        return self._validate(record) is None

    def get_or_set(
        self,
        url: str,
        fetch_func: callable,
        params: Optional[Dict] = None,
        ttl: Optional[int] = None
    ) -> Any:
        """
        Get from cache or fetch and cache.
        
        This is a convenience method that combines get and set operations.
        
        Args:
            url: Request URL
            fetch_func: Callable that returns data to cache
            params: Request parameters
            ttl: Time-to-live in seconds
            
        Returns:
            Cached or freshly fetched data
        """
        cached = self.get(url, params)
        if cached is not None:
            return cached
        
        data = fetch_func()
        if data is not None:
            self.set(url, data, params, ttl)
        
        return data


class MemoryCache:
    """
    Simple in-memory cache for session-level caching.
    
    This cache is faster than FileCache but doesn't persist
    between program runs.
    """
    
    def __init__(self, max_entries: int = 1000, default_ttl: int = 300):
        """
        Initialize memory cache.
        
        Args:
            max_entries: Maximum number of entries to store
            default_ttl: Default time-to-live in seconds
        """
        self.max_entries = max_entries
        self.default_ttl = default_ttl
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._stats = {'hits': 0, 'misses': 0}
    
    def _get_key(self, url: str, params: Optional[Dict] = None) -> str:
        """Generate cache key"""
        key_data = url
        if params:
            key_data += json.dumps(params, sort_keys=True)
        return hashlib.md5(key_data.encode()).hexdigest()
    
    def get(self, url: str, params: Optional[Dict] = None) -> Optional[Any]:
        """Get cached value"""
        key = self._get_key(url, params)
        entry = self._cache.get(key)
        
        if entry is None:
            self._stats['misses'] += 1
            return None
        
        if time.time() > entry['expires_at']:
            del self._cache[key]
            self._stats['misses'] += 1
            return None
        
        self._stats['hits'] += 1
        return entry['data']
    
    def set(
        self, 
        url: str, 
        data: Any, 
        params: Optional[Dict] = None,
        ttl: Optional[int] = None
    ) -> None:
        """Set cached value"""
        # Evict oldest entries if at capacity
        if len(self._cache) >= self.max_entries:
            oldest_key = min(
                self._cache.keys(),
                key=lambda k: self._cache[k]['created_at']
            )
            del self._cache[oldest_key]
        
        key = self._get_key(url, params)
        ttl = ttl or self.default_ttl
        
        self._cache[key] = {
            'data': data,
            'created_at': time.time(),
            'expires_at': time.time() + ttl
        }
    
    def clear(self) -> None:
        """Clear all cached entries"""
        self._cache.clear()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        total = self._stats['hits'] + self._stats['misses']
        hit_rate = self._stats['hits'] / total * 100 if total > 0 else 0
        
        return {
            'entries': len(self._cache),
            'max_entries': self.max_entries,
            'hits': self._stats['hits'],
            'misses': self._stats['misses'],
            'hit_rate_percent': round(hit_rate, 2)
        }