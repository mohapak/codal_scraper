"""
Async client for high-performance Codal API queries

This module provides an asynchronous client for fetching data from
the Codal API with concurrent requests for improved performance.
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlencode

import aiohttp

from .constants import (
    SEARCH_API_URL, DEFAULT_HEADERS, PERIOD_LENGTHS, COMPANY_TYPES,
    DEFAULT_TIMEOUT, DEFAULT_RETRY_COUNT, DEFAULT_MAX_CONCURRENT
)
from .utils import clean_dict, clean_symbol
from .validators import InputValidator
from .exceptions import APIError, RateLimitError, NetworkError
from .cache import FileCache, CacheConfig
from .rate_limiter import AsyncRateLimiter
from .types import LetterData, QueryParams, QueryStats
from .pagination import (
    MAX_PAGES_ABSOLUTE, PaginationCapExceededError, assert_within_cap,
    detect_page_semantics,
    estimate_page_count, finalize_harvest, new_result, page_cap, response_total
)


logger = logging.getLogger(__name__)


class AsyncCodalClient:
    """
    Async client for high-performance Codal API queries.
    
    Use this client when you need to fetch many pages concurrently
    for significantly improved performance over the synchronous client.
    
    Features:
        - Concurrent page fetching
        - Async rate limiting
        - Response caching
        - Automatic retry with backoff
        - Context manager support
    
    Example:
        >>> async with AsyncCodalClient() as client:
        ...     letters = await client.fetch_board_changes(
        ...         "1402/01/01", "1402/12/29"
        ...     )
        ...     print(f"Found {len(letters)} announcements")
    
    Note:
        Requires aiohttp to be installed: pip install aiohttp
    """
    
    def __init__(
        self,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        timeout: int = DEFAULT_TIMEOUT,
        retry_count: int = DEFAULT_RETRY_COUNT,
        requests_per_second: float = 2.0,
        cache_config: Optional[CacheConfig] = None,
        enable_cache: bool = True
    ):
        """
        Initialize async client.
        
        Args:
            max_concurrent: Maximum concurrent requests
            timeout: Request timeout in seconds
            retry_count: Number of retry attempts
            requests_per_second: Rate limit for requests
            cache_config: Cache configuration
            enable_cache: Whether to enable caching
        """
        self.max_concurrent = max_concurrent
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.retry_count = retry_count
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self.rate_limiter = AsyncRateLimiter(
            requests_per_second=requests_per_second
        )
        
        # Cache setup
        if enable_cache:
            cache_config = cache_config or CacheConfig()
            self.cache = FileCache(cache_config)
        else:
            self.cache = None
        
        # Default query parameters
        self._reset_params()
        
        # Statistics
        self._stats = {
            'requests_made': 0,
            'requests_failed': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'total_items': 0,
            'total_time': 0.0
        }
    
    def _reset_params(self) -> None:
        """Reset query parameters to defaults"""
        self.params: QueryParams = {
            "PageNumber": 1,
            "Symbol": -1,
            "CompanyType": -1,
            "LetterCode": -1,
            "FromDate": -1,
            "ToDate": -1,
            "Length": -1,
            "Audited": "true",
            "NotAudited": "true",
            "Consolidatable": "true",
            "NotConsolidatable": "true",
            "Childs": "true",
            "Mains": "true",
        }
    
    async def __aenter__(self) -> 'AsyncCodalClient':
        """Async context manager entry"""
        self._session = aiohttp.ClientSession(
            timeout=self.timeout,
            headers=DEFAULT_HEADERS
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Async context manager exit"""
        await self.close()
        return False
    
    async def close(self) -> None:
        """Close the session and release resources"""
        if self._session:
            await self._session.close()
            self._session = None
            logger.debug("Async session closed")
    
    def _build_url(self, params: Dict, page: int = 1) -> str:
        """Build API URL from parameters"""
        params = {**params, 'PageNumber': page}
        cleaned = clean_dict(params)
        query = urlencode(cleaned)
        return f"{SEARCH_API_URL}{query}&search=true"
    
    async def _fetch_page(
        self, 
        url: str, 
        use_cache: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch a single page with rate limiting and retries.
        
        Args:
            url: URL to fetch
            use_cache: Whether to use cache
            
        Returns:
            Response data or None if failed
        """
        # Check cache first
        if use_cache and self.cache:
            cached = self.cache.get(url)
            if cached is not None:
                self._stats['cache_hits'] += 1
                return cached
            self._stats['cache_misses'] += 1
        
        async with self._semaphore:
            await self.rate_limiter.acquire()
            
            for attempt in range(self.retry_count):
                try:
                    self._stats['requests_made'] += 1
                    
                    async with self._session.get(url) as response:
                        if response.status == 429:
                            retry_after = float(
                                response.headers.get('Retry-After', 60)
                            )
                            logger.warning(f"Rate limited, waiting {retry_after}s")
                            await asyncio.sleep(retry_after)
                            continue
                        
                        response.raise_for_status()
                        data = await response.json()
                        
                        # Cache successful response
                        if use_cache and self.cache and data:
                            self.cache.set(url, data)
                        
                        return data
                        
                except aiohttp.ClientResponseError as e:
                    logger.warning(f"HTTP error on attempt {attempt + 1}: {e}")
                    self._stats['requests_failed'] += 1
                    
                except aiohttp.ClientError as e:
                    logger.warning(f"Request error on attempt {attempt + 1}: {e}")
                    self._stats['requests_failed'] += 1
                    
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout on attempt {attempt + 1}")
                    self._stats['requests_failed'] += 1
                
                if attempt < self.retry_count - 1:
                    wait_time = min(2 ** attempt, 30)
                    await asyncio.sleep(wait_time)
            
            return None
    
    async def fetch_all_pages(
        self,
        params: Dict,
        max_pages: Optional[int] = None,
        use_cache: bool = True,
        allow_partial: bool = False
    ) -> List[LetterData]:
        """
        Fetch every page of a query, whichever way the API's `Page` field
        behaves.

        Behaviourally identical to `CodalClient.fetch_all_pages`: the page count
        comes from `Total` and the page size actually returned, never from the
        response's `Page` field (P0-3); paging stops at `Total` items or at the
        first empty page; and the harvest is asserted against `Total`.

        Args:
            params: Query parameters
            max_pages: Hard bound on pages fetched (also caps the sanity limit)
            use_cache: Whether to use cache
            allow_partial: Return what arrived when it falls short of `Total`,
                instead of raising. The returned list still records
                `expected_total`, `arrived`, `shortfall` and `provenance()`.

        Returns:
            Combined list of all letters/announcements, as a
            `pagination.PaginationResult` (a plain list carrying provenance).

        Raises:
            PaginationCapExceededError: the response implies more pages than the
                cap allows (e.g. a runaway `Total`).
            IncompleteResultsError: fewer items arrived than `Total` claimed and
                `allow_partial` is False.
        """
        start_time = time.time()
        first_url = self._build_url(params, page=1)
        first_response = await self._fetch_page(first_url, use_cache)
        if not first_response:
            logger.warning("Failed to fetch first page")
            return []

        first_letters = list(first_response.get("Letters") or [])
        total = response_total(first_response)
        page_size = len(first_letters)
        pages_needed = estimate_page_count(total, page_size)
        if total is None:
            cap = int(max_pages) if max_pages else MAX_PAGES_ABSOLUTE
        else:
            cap = page_cap(pages_needed, max_pages)
        page_field_mode = detect_page_semantics(first_response, 1, pages_needed)
        logger.info(
            f"Total: {total} item(s), page size {page_size}, "
            f"~{pages_needed} page(s) expected (cap {cap}); response 'Page' "
            f"field reads as {page_field_mode}"
        )
        # Two guards: an absolute one that always raises (a nonsensical `Total`
        # must never become a crawl), and the caller's own `max_pages`, which
        # `allow_partial` may turn into a graceful truncation.
        ceiling = (
            max(MAX_PAGES_ABSOLUTE, int(max_pages)) if max_pages
            else MAX_PAGES_ABSOLUTE
        )
        assert_within_cap(pages_needed, ceiling, total=total, page_size=page_size)
        if pages_needed > cap and not allow_partial:
            raise PaginationCapExceededError(
                f"max_pages={max_pages} allows {cap} page(s) but the response "
                f"implies {pages_needed} page(s) for Total={total}; pass a larger "
                f"max_pages, or allow_partial=True to accept a partial harvest.",
                expected=pages_needed,
                collected=0,
                details={
                    "total": total, "page_size": page_size,
                    "page_cap": cap, "max_pages": max_pages,
                }
            )

        collected = new_result(
            first_response, first_letters,
            page_cap_value=cap, page_field_mode=page_field_mode
        )
        failed_pages = 0

        async def fetch(page_number: int) -> Optional[Dict[str, Any]]:
            return await self._fetch_page(
                self._build_url(params, page=page_number), use_cache
            )

        # Pages 2..estimate are fetched concurrently; the client's semaphore and
        # rate limiter bound how hard this hits codal.ir. Pages beyond the
        # estimate are fetched one at a time, so a wrong estimate costs a single
        # extra request rather than another fan-out.
        estimated_last = min(pages_needed, cap)
        if estimated_last >= 2:
            pages = list(range(2, estimated_last + 1))
            collected.pages_requested += len(pages)
            results = await asyncio.gather(
                *(fetch(p) for p in pages), return_exceptions=True
            )
            for p, result in zip(pages, results):
                if isinstance(result, BaseException) or not isinstance(result, dict):
                    failed_pages += 1
                    logger.error(f"Page {p} failed: {result}")
                    continue
                letters = list(result.get("Letters") or [])
                if not letters:
                    logger.info(f"Page {p} returned zero items")
                    continue
                collected.extend(letters)
                collected.pages_fetched += 1

        page = max(2, estimated_last + 1)
        while page <= cap:
            if total is not None:
                if len(collected) >= total:
                    break
            elif not page_size:
                break  # no `Total` and an empty first page: nothing to page
            collected.pages_requested += 1
            result = await fetch(page)
            if not isinstance(result, dict):
                failed_pages += 1
                logger.warning(f"Failed to fetch page {page}")
                page += 1
                continue
            letters = list(result.get("Letters") or [])
            if not letters:
                logger.info(f"Page {page} returned zero items; stopping")
                break
            collected.extend(letters)
            collected.pages_fetched += 1
            page += 1

        collected.failed_pages = failed_pages
        elapsed = time.time() - start_time
        self._stats['total_time'] = elapsed
        self._stats['total_items'] = len(collected)
        logger.info(
            f"Fetched {len(collected)} item(s) from {collected.pages_fetched} "
            f"page(s) in {elapsed:.2f}s"
        )
        return finalize_harvest(collected, allow_partial=allow_partial)

    # ============== Convenience Methods ==============
    
    async def fetch_board_changes(
        self,
        from_date: str,
        to_date: str,
        company_type: Optional[str] = None,
        max_pages: Optional[int] = None
    ) -> List[LetterData]:
        """
        Fetch board of directors changes (ن-45).
        
        Args:
            from_date: Start date (Persian calendar)
            to_date: End date (Persian calendar)
            company_type: Optional company type filter
            max_pages: Maximum pages to fetch
            
        Returns:
            List of board change announcements
        """
        InputValidator(from_date).is_date()
        InputValidator(to_date).is_date()
        
        params: QueryParams = {
            "LetterCode": "ن-45",
            "FromDate": from_date,
            "ToDate": to_date,
            "Childs": "false",
            "Mains": "true",
            "Audited": "true",
            "NotAudited": "true",
        }
        
        if company_type:
            params["CompanyType"] = COMPANY_TYPES.get(company_type, company_type)
        
        return await self.fetch_all_pages(params, max_pages)
    
    async def fetch_financial_statements(
        self,
        from_date: str,
        to_date: str,
        period_length: int = 12,
        audited_only: bool = True,
        max_pages: Optional[int] = None
    ) -> List[LetterData]:
        """
        Fetch financial statements (ن-10).
        
        Args:
            from_date: Start date (Persian calendar)
            to_date: End date (Persian calendar)
            period_length: Period length in months
            audited_only: Only fetch audited statements
            max_pages: Maximum pages to fetch
            
        Returns:
            List of financial statement announcements
        """
        InputValidator(from_date).is_date()
        InputValidator(to_date).is_date()
        
        params: QueryParams = {
            "LetterCode": "ن-10",
            "FromDate": from_date,
            "ToDate": to_date,
            "Length": PERIOD_LENGTHS.get(period_length, -1),
        }
        
        if audited_only:
            params["Audited"] = "true"
            params["NotAudited"] = "false"
        
        return await self.fetch_all_pages(params, max_pages)
    
    async def fetch_monthly_reports(
        self,
        from_date: str,
        to_date: str,
        symbol: Optional[str] = None,
        max_pages: Optional[int] = None
    ) -> List[LetterData]:
        """
        Fetch monthly activity reports (ن-30).
        
        Args:
            from_date: Start date (Persian calendar)
            to_date: End date (Persian calendar)
            symbol: Optional specific symbol
            max_pages: Maximum pages to fetch
            
        Returns:
            List of monthly report announcements
        """
        InputValidator(from_date).is_date()
        InputValidator(to_date).is_date()
        
        params: QueryParams = {
            "LetterCode": "ن-30",
            "FromDate": from_date,
            "ToDate": to_date,
        }
        
        if symbol:
            params["Symbol"] = clean_symbol(symbol)
        
        return await self.fetch_all_pages(params, max_pages)
    
    async def fetch_by_symbol(
        self,
        symbol: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        max_pages: Optional[int] = None
    ) -> List[LetterData]:
        """
        Fetch all announcements for a specific symbol.
        
        Args:
            symbol: Stock symbol
            from_date: Optional start date
            to_date: Optional end date
            max_pages: Maximum pages to fetch
            
        Returns:
            List of announcements for the symbol
        """
        symbol = clean_symbol(symbol)
        InputValidator(symbol).is_symbol()
        
        params: QueryParams = {
            "Symbol": symbol,
        }
        
        if from_date:
            InputValidator(from_date).is_date()
            params["FromDate"] = from_date
        
        if to_date:
            InputValidator(to_date).is_date()
            params["ToDate"] = to_date
        
        return await self.fetch_all_pages(params, max_pages)
    
    async def fetch_multiple_symbols(
        self,
        symbols: List[str],
        from_date: str,
        to_date: str,
        max_pages_per_symbol: int = 10
    ) -> Dict[str, List[LetterData]]:
        """
        Fetch announcements for multiple symbols concurrently.
        
        Args:
            symbols: List of stock symbols
            from_date: Start date (Persian calendar)
            to_date: End date (Persian calendar)
            max_pages_per_symbol: Max pages per symbol
            
        Returns:
            Dictionary mapping symbol to list of announcements
        """
        async def fetch_symbol(symbol: str) -> tuple:
            letters = await self.fetch_by_symbol(
                symbol, from_date, to_date, max_pages_per_symbol
            )
            return symbol, letters
        
        tasks = [fetch_symbol(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        output = {}
        for result in results:
            if isinstance(result, tuple):
                symbol, letters = result
                output[symbol] = letters
            elif isinstance(result, Exception):
                logger.error(f"Failed to fetch symbol: {result}")
        
        return output
    
    def get_stats(self) -> QueryStats:
        """Get query statistics"""
        return QueryStats(
            total_results=self._stats.get('total_items', 0),
            total_pages=0,  # Not tracked per-query
            pages_fetched=self._stats['requests_made'],
            items_fetched=self._stats['total_items'],
            failed_pages=self._stats['requests_failed'],
            duration_seconds=self._stats['total_time'],
            cache_hits=self._stats['cache_hits'],
            cache_misses=self._stats['cache_misses']
        )
    
    def reset_stats(self) -> None:
        """Reset statistics"""
        self._stats = {
            'requests_made': 0,
            'requests_failed': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'total_items': 0,
            'total_time': 0.0
        }