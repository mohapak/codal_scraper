"""
Main client for interacting with Codal API

This module provides a synchronous client for querying the Codal.ir API,
the official disclosure platform for Iranian listed companies.
"""

import json
import logging
import re
import time
import urllib.parse as urlparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlencode

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.exceptions import RequestException

from .constants import (
    SEARCH_API_URL, REPORT_LIST_URL, DEFAULT_HEADERS, DOWNLOAD_HEADERS,
    PERIOD_LENGTHS, COMPANY_TYPES, CDN_TSETMC_API, DEFAULT_TIMEOUT,
    DEFAULT_RETRY_COUNT
)
from .utils import clean_dict, clean_symbol, build_full_url
from .validators import InputValidator, validate_date_range
from .exceptions import APIError, RateLimitError, ValidationError, NetworkError, ParseError
from .cache import FileCache, CacheConfig
from .rate_limiter import RateLimiter, RateLimitConfig
from .types import QueryParams, LetterData, QueryStats, PaginationInfo
from .pagination import (
    MAX_PAGES_ABSOLUTE, PaginationCapExceededError, assert_within_cap,
    detect_page_semantics,
    estimate_page_count, finalize_harvest, new_result, page_cap, response_total
)


logger = logging.getLogger(__name__)


class CodalClient:
    """
    Client for querying Codal.ir API.
    
    This client provides a clean interface for searching and fetching
    announcements and reports from the Iranian stock market disclosure system.
    
    Features:
        - Fluent API with method chaining
        - Automatic retry with exponential backoff
        - Rate limiting to avoid overwhelming the server
        - Response caching for improved performance
        - Context manager support for proper resource cleanup
    
    Example:
        >>> with CodalClient() as client:
        ...     letters = (client
        ...         .set_letter_code("ن-45")
        ...         .set_date_range("1402/01/01", "1402/12/29")
        ...         .fetch_all_pages())
        ...     print(f"Found {len(letters)} announcements")
    
    Attributes:
        retry_count: Number of retry attempts for failed requests
        timeout: Request timeout in seconds
        total_results: Total number of results from last query
        total_pages: Total number of pages from last query
    """
    
    def __init__(
        self,
        retry_count: int = DEFAULT_RETRY_COUNT,
        timeout: int = DEFAULT_TIMEOUT,
        cache_config: Optional[CacheConfig] = None,
        rate_limit_config: Optional[RateLimitConfig] = None,
        enable_cache: bool = True,
        enable_rate_limit: bool = True
    ):
        """
        Initialize Codal client.
        
        Args:
            retry_count: Number of retry attempts for failed requests
            timeout: Request timeout in seconds
            cache_config: Cache configuration (uses defaults if None)
            rate_limit_config: Rate limit configuration (uses defaults if None)
            enable_cache: Whether to enable response caching
            enable_rate_limit: Whether to enable rate limiting
        """
        self.retry_count = retry_count
        self.timeout = timeout
        
        # Lazy session initialization
        self._session: Optional[requests.Session] = None
        
        # Cache setup
        if enable_cache:
            cache_config = cache_config or CacheConfig()
            self.cache = FileCache(cache_config)
        else:
            self.cache = None
        
        # Rate limiter setup
        if enable_rate_limit:
            rate_limit_config = rate_limit_config or RateLimitConfig()
            self.rate_limiter = RateLimiter(rate_limit_config)
        else:
            self.rate_limiter = None
        
        # Query parameters with default values
        self._reset_params()
        
        # Response tracking
        self.total_results: Optional[int] = None
        self.total_pages: Optional[int] = None
        self.current_page: int = 1
        
        # Statistics
        self._stats = {
            'requests_made': 0,
            'requests_failed': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'total_items_fetched': 0
        }
    
    @property
    def session(self) -> requests.Session:
        """Lazy session initialization"""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(DEFAULT_HEADERS)
        return self._session
    
    def __enter__(self) -> 'CodalClient':
        """Context manager entry"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Context manager exit - cleanup resources"""
        self.close()
        return False
    
    def close(self) -> None:
        """Close the session and release resources"""
        if self._session is not None:
            try:
                self._session.close()
            except Exception as e:
                logger.warning(f"Error closing session: {e}")
            finally:
                self._session = None
            logger.debug("Session closed")
    
    def _reset_params(self) -> None:
        """Reset all query parameters to default values"""
        self.params: QueryParams = {
            "PageNumber": 1,
            "Symbol": -1,
            "PublisherStatus": -1,
            "Category": -1,
            "CompanyType": -1,
            "CompanyState": -1,
            "LetterType": -1,
            "Subject": -1,
            "TracingNo": -1,
            "LetterCode": -1,
            "Length": -1,
            "FromDate": -1,
            "ToDate": -1,
            "Audited": "true",
            "NotAudited": "true",
            "Consolidatable": "true",
            "NotConsolidatable": "true",
            "Childs": "true",
            "Mains": "true",
            "AuditorRef": -1,
            "YearEndToDate": -1,
            "Publisher": "false"
        }
    
    def reset_params(self) -> 'CodalClient':
        """
        Reset all query parameters to default values.
        
        Returns:
            Self for method chaining
        """
        self._reset_params()
        return self
    
    # ============== Parameter Setters ==============
    
    def set_symbol(self, symbol: Optional[str]) -> 'CodalClient':
        """
        Set stock symbol for query.
        
        Args:
            symbol: Stock symbol (e.g., "فولاد", "خودرو")
                   Pass None or empty string to clear
        
        Returns:
            Self for method chaining
            
        Raises:
            ValidationError: If symbol format is invalid
        """
        if symbol:
            symbol = clean_symbol(symbol)
            InputValidator(symbol).is_symbol()
            self.params['Symbol'] = symbol
        else:
            self.params['Symbol'] = -1
        return self
    
    def set_company_type(self, company_type: Optional[str]) -> 'CodalClient':
        """
        Set company type filter.
        
        Args:
            company_type: One of:
                - Persian: 'بورس', 'فرابورس', 'پایه'
                - English: 'bourse', 'farabourse', 'otc'
                - Code: '1', '2', '3'
                - None to clear filter
        
        Returns:
            Self for method chaining
        """
        if company_type:
            type_code = COMPANY_TYPES.get(company_type, company_type)
            self.params['CompanyType'] = type_code
        else:
            self.params['CompanyType'] = -1
        return self
    
    def set_isic(self, isic: Optional[str]) -> 'CodalClient':
        """
        Set ISIC (industry classification) code.
        
        Args:
            isic: ISIC code (4-6 digits)
        
        Returns:
            Self for method chaining
        """
        if isic:
            InputValidator(isic).is_isic()
            self.params['Isic'] = isic
        else:
            self.params.pop('Isic', None)
        return self
    
    def set_subject(self, subject: Optional[str]) -> 'CodalClient':
        """
        Set announcement subject filter.
        
        Args:
            subject: Subject text to search for
        
        Returns:
            Self for method chaining
        """
        if subject:
            self.params["Subject"] = subject
        else:
            self.params["Subject"] = -1
        return self
    
    def set_tracing_number(self, tracing_no: Optional[str]) -> 'CodalClient':
        """
        Set specific tracing number to fetch.
        
        Args:
            tracing_no: Tracing number of the announcement
        
        Returns:
            Self for method chaining
        """
        if tracing_no:
            self.params["TracingNo"] = tracing_no
        else:
            self.params["TracingNo"] = -1
        return self
    
    def set_letter_code(self, code: Optional[str]) -> 'CodalClient':
        """
        Set letter code filter (e.g., 'ن-45').
        
        Common letter codes:
            - ن-10: Financial statements
            - ن-30: Monthly activity report
            - ن-45: Board member changes
            - ن-52: Annual general meeting decisions
        
        Args:
            code: Letter code
        
        Returns:
            Self for method chaining
        """
        if code:
            self.params["LetterCode"] = code
        else:
            self.params["LetterCode"] = -1
        return self
    
    def set_period_length(self, period: Optional[Union[int, str]]) -> 'CodalClient':
        """
        Set reporting period length filter.
        
        Args:
            period: Period length in months (1-12) or Persian numeral
        
        Returns:
            Self for method chaining
        """
        if period is not None:
            period_value = PERIOD_LENGTHS.get(period, -1)
            self.params["Length"] = period_value
        else:
            self.params["Length"] = -1
        return self
    
    def set_date_range(
        self, 
        from_date: Optional[str] = None, 
        to_date: Optional[str] = None
    ) -> 'CodalClient':
        """
        Set date range for query.
        
        Args:
            from_date: Start date in YYYY/MM/DD format (Persian calendar)
            to_date: End date in YYYY/MM/DD format (Persian calendar)
        
        Returns:
            Self for method chaining
            
        Raises:
            ValidationError: If date format is invalid
            
        Example:
            >>> client.set_date_range("1402/01/01", "1402/12/29")
        """
        if from_date:
            InputValidator(from_date).is_date()
            self.params["FromDate"] = from_date
        else:
            self.params["FromDate"] = -1
        
        if to_date:
            InputValidator(to_date).is_date()
            self.params["ToDate"] = to_date
        
        if from_date and to_date:
            # Ordering is checked on parsed Jalali dates (validators.py): a raw
            # string comparison rejects valid ranges such as 1402/5/15 ->
            # 1402/10/15.
            validate_date_range(from_date, to_date)
        else:
            self.params["ToDate"] = -1
        
        return self
    
    def set_audit_status(
        self, 
        audited: bool = True, 
        not_audited: bool = True
    ) -> 'CodalClient':
        """
        Set audit status filters.
        
        Args:
            audited: Include audited statements
            not_audited: Include non-audited statements
        
        Returns:
            Self for method chaining
        """
        self.params["Audited"] = str(audited).lower()
        self.params["NotAudited"] = str(not_audited).lower()
        return self
    
    def set_consolidation_status(
        self, 
        consolidated: bool = True, 
        not_consolidated: bool = True
    ) -> 'CodalClient':
        """
        Set consolidation status filters.
        
        Args:
            consolidated: Include consolidated statements
            not_consolidated: Include non-consolidated statements
        
        Returns:
            Self for method chaining
        """
        self.params["Consolidatable"] = str(consolidated).lower()
        self.params["NotConsolidatable"] = str(not_consolidated).lower()
        return self
    
    def set_entity_type(
        self, 
        include_childs: bool = True, 
        include_mains: bool = True
    ) -> 'CodalClient':
        """
        Set entity type filters.
        
        Args:
            include_childs: Include subsidiary companies
            include_mains: Include main/parent companies
        
        Returns:
            Self for method chaining
        """
        self.params["Childs"] = str(include_childs).lower()
        self.params["Mains"] = str(include_mains).lower()
        return self
    
    def set_year_end(self, year_end_date: Optional[str]) -> 'CodalClient':
        """
        Set fiscal year end date filter.
        
        Args:
            year_end_date: Fiscal year end date (e.g., "1402/12/29")
        
        Returns:
            Self for method chaining
        """
        if year_end_date:
            InputValidator(year_end_date).is_date()
            self.params["YearEndToDate"] = year_end_date
        else:
            self.params["YearEndToDate"] = -1
        return self
    
    def set_publisher_only(self, publisher_only: bool = False) -> 'CodalClient':
        """
        Set whether to include only organization-published announcements.
        
        Args:
            publisher_only: If True, only show publisher announcements
        
        Returns:
            Self for method chaining
        """
        self.params["Publisher"] = str(publisher_only).lower()
        return self
    
    def set_page_number(self, page: int) -> 'CodalClient':
        """
        Set the page number for the next query.

        Args:
            page: Page number (1-based)

        Returns:
            Self for method chaining

        Raises:
            ValidationError: if `page` is not a positive integer. An invalid
                page used to be ignored silently, which left whatever page
                happened to be current in place (so `fetch_page(0)` quietly
                re-fetched the previous page).
        """
        if isinstance(page, bool) or not isinstance(page, int):
            raise ValidationError(
                "Page number must be an integer", field="page", value=page
            )
        if page < 1:
            raise ValidationError(
                "Page number must be >= 1 (pages are 1-based)",
                field="page", value=page
            )
        self.params["PageNumber"] = page
        self.current_page = page
        return self

    # ============== URL Generation ==============
    
    def get_query_url(self, use_api: bool = True) -> str:
        """
        Generate query URL with current parameters.
        
        Args:
            use_api: If True, generate API URL; if False, generate web URL
        
        Returns:
            Complete query URL
        """
        base_url = SEARCH_API_URL if use_api else REPORT_LIST_URL
        cleaned_params = clean_dict(self.params)
        
        url_parts = list(urlparse.urlparse(base_url))
        query = dict(urlparse.parse_qsl(url_parts[4]))
        query.update(cleaned_params)
        
        if use_api:
            url_parts[4] = f"{urlencode(query)}&search=true"
        else:
            url_parts[4] = f"search&{urlencode(query)}"
        
        return urlparse.urlunparse(url_parts)
    
    # ============== Data Fetching ==============
    
    def _make_request(
        self, 
        url: str, 
        use_cache: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        Make HTTP request with retry logic, caching, and rate limiting.
        
        Args:
            url: URL to request
            use_cache: Whether to use cache
        
        Returns:
            Response data as dictionary or None if failed
            
        Raises:
            RateLimitError: If rate limit exceeded and retries exhausted
            APIError: If API returns an error
            NetworkError: If network connection fails
        """
        # Check cache first
        if use_cache and self.cache:
            cached = self.cache.get(url, self.params)
            if cached is not None:
                self._stats['cache_hits'] += 1
                logger.debug(f"Cache hit for {url}")
                return cached
            self._stats['cache_misses'] += 1
        
        last_error = None
        
        for attempt in range(self.retry_count):
            try:
                # Apply rate limiting
                if self.rate_limiter:
                    self.rate_limiter.acquire()
                
                self._stats['requests_made'] += 1
                
                response = self.session.get(url, timeout=self.timeout)
                
                # Handle rate limiting response
                if response.status_code == 429:
                    retry_after = float(response.headers.get('Retry-After', 60))
                    logger.warning(f"Rate limited (429), waiting {retry_after}s")
                    time.sleep(retry_after)
                    continue
                
                response.raise_for_status()
                
                # Parse response
                content_type = response.headers.get('content-type', '')
                if 'application/json' in content_type:
                    data = response.json()
                else:
                    try:
                        data = json.loads(response.text)
                    except json.JSONDecodeError:
                        raise ParseError(
                            f"Failed to parse response as JSON",
                            content_type=content_type
                        )
                
                # Cache successful response
                if use_cache and self.cache and data:
                    self.cache.set(url, data, self.params)
                
                return data
                
            except requests.exceptions.Timeout as e:
                last_error = NetworkError(f"Request timed out: {e}", url=url, cause=e)
                logger.warning(f"Request attempt {attempt + 1} timed out")
                
            except requests.exceptions.ConnectionError as e:
                last_error = NetworkError(f"Connection failed: {e}", url=url, cause=e)
                logger.warning(f"Request attempt {attempt + 1} connection failed: {e}")
                
            except RequestException as e:
                last_error = APIError(
                    f"Request failed: {e}",
                    url=url,
                    status_code=getattr(e.response, 'status_code', None) if hasattr(e, 'response') else None
                )
                logger.warning(f"Request attempt {attempt + 1} failed: {e}")
                
            except json.JSONDecodeError as e:
                last_error = ParseError(f"Failed to parse JSON: {e}")
                logger.error(f"JSON parse error: {e}")
                self._stats['requests_failed'] += 1
                return None
            
            # Exponential backoff
            if attempt < self.retry_count - 1:
                wait_time = min(2 ** attempt, 30)  # Cap at 30 seconds
                logger.info(f"Retrying in {wait_time}s...")
                time.sleep(wait_time)
        
        self._stats['requests_failed'] += 1
        logger.error(f"All {self.retry_count} request attempts failed for URL: {url}")
        
        if last_error:
            logger.error(f"Last error: {last_error}")
        
        return None
    
    def _fetch_page_response(
        self,
        page: Optional[int] = None,
        use_cache: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch one raw API response page and refresh pagination metadata.

        Args:
            page: 1-based page number to fetch (None = the page currently set)
            use_cache: Whether to use cache

        Returns:
            The parsed response dict (with its `Total`, `Page` and `Letters`),
            or None if the request failed.
        """
        if page is None:
            page = self.params.get("PageNumber") or 1
        self.set_page_number(page)
        url = self.get_query_url(use_api=True)
        logger.info(f"Fetching page {self.params['PageNumber']}")
        response = self._make_request(url, use_cache=use_cache)
        if not response:
            return None

        letters = response.get("Letters") or []
        self.total_results = response_total(response) or 0
        # P0-3: never derive the page count from the response's `Page` field.
        # It may be the *current page index*, in which case page 1 reports a
        # page count of 1 and every query looks like it fits on one page.
        # Estimate from `Total` and the page size actually returned instead,
        # and never let the estimate shrink as we page forward.
        estimated_pages = estimate_page_count(self.total_results, len(letters))
        if self.total_pages is None or estimated_pages > self.total_pages:
            self.total_pages = estimated_pages
        self.current_page = self.params["PageNumber"]
        self._stats['total_items_fetched'] += len(letters)
        return response

    def fetch_page(
        self,
        page: Optional[int] = None,
        use_cache: bool = True
    ) -> Optional[List[LetterData]]:
        """
        Fetch a single page of results.

        Args:
            page: 1-based page number to fetch (None = the page currently set)
            use_cache: Whether to use cache

        Returns:
            List of letters/announcements or None if failed

        Raises:
            ValidationError: if `page` is not a positive integer.
        """
        response = self._fetch_page_response(page, use_cache=use_cache)
        if not response:
            return None
        return response.get("Letters") or []

    def fetch_all_pages(
        self,
        max_pages: Optional[int] = None,
        use_cache: bool = True,
        show_progress: bool = True,
        allow_partial: bool = False
    ) -> List[LetterData]:
        """
        Fetch every page of the current query, whichever way the API's `Page`
        field behaves.

        The number of pages is derived from the response's `Total` and the page
        size the API *actually returned* -- never from its `Page` field, which
        may be either the current page index or the page count (P0-3). Paging
        stops once `Total` items have been collected or a page returns zero
        items, and the harvest is asserted against `Total` before it is
        returned, so a shortfall cannot pass silently.

        Args:
            max_pages: Hard bound on pages fetched. It also caps the sanity
                limit, so a query that genuinely needs more pages than this
                raises instead of quietly returning less than `Total`.
            use_cache: Whether to use cache
            show_progress: Whether to log per-page progress
            allow_partial: Return what arrived when it falls short of `Total`,
                instead of raising. The returned list still records
                `expected_total`, `arrived`, `shortfall` and `provenance()`.

        Returns:
            Combined list of all letters/announcements. It is a plain list, but
            a `pagination.PaginationResult` one, carrying provenance.

        Raises:
            PaginationCapExceededError: the response implies more pages than the
                cap allows (e.g. a runaway `Total`).
            IncompleteResultsError: fewer items arrived than `Total` claimed and
                `allow_partial` is False.
        """
        start_time = time.time()
        first_response = self._fetch_page_response(1, use_cache=use_cache)
        if not first_response:
            logger.warning("Failed to fetch first page")
            return []

        first_letters = list(first_response.get("Letters") or [])
        total = self.total_results or None
        page_size = len(first_letters)
        pages_needed = estimate_page_count(total, page_size)
        if total is None:
            # No usable `Total` to aim at: page until a page comes back empty,
            # bounded only by the absolute cap (or the caller's own bound).
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
        page = 2
        while page <= cap:
            if total is not None:
                if len(collected) >= total:
                    break
            elif not page_size:
                break  # no `Total` and an empty first page: nothing to page
            collected.pages_requested += 1
            response = self._fetch_page_response(page, use_cache=use_cache)
            if not response:
                failed_pages += 1
                logger.warning(f"Failed to fetch page {page}")
                page += 1
                continue
            letters = list(response.get("Letters") or [])
            if not letters:
                logger.info(f"Page {page} returned zero items; stopping")
                break
            collected.extend(letters)
            collected.pages_fetched += 1
            if show_progress:
                logger.info(
                    f"Fetched page {page}/{cap} ({len(letters)} items, "
                    f"{len(collected)} collected)"
                )
            page += 1

        collected.failed_pages = failed_pages
        elapsed = time.time() - start_time
        logger.info(
            f"Fetched {len(collected)} item(s) from {collected.pages_fetched} "
            f"page(s) in {elapsed:.2f}s"
        )
        return finalize_harvest(
            collected, allow_partial=allow_partial, query=self.get_query_url()
        )

    def fetch_tsetmc_data(
        self, 
        data_type: str, 
        code: str, 
        date: str
    ) -> Optional[Dict]:
        """
        Fetch data from TSE TMC CDN API.
        
        Args:
            data_type: Type of data to fetch
            code: Stock code
            date: Date for the data
        
        Returns:
            Response data or None if failed
        """
        url = CDN_TSETMC_API.format(data=data_type, code=code, date=date)
        return self._make_request(url, use_cache=True)
    
    # ============== Convenience Methods ==============
    
    def search_board_changes(
        self, 
        from_date: str, 
        to_date: str,
        company_type: Optional[str] = None,
        max_pages: Optional[int] = None
    ) -> List[LetterData]:
        """
        Search for board of directors changes (letter code ن-45).
        
        Args:
            from_date: Start date (Persian calendar)
            to_date: End date (Persian calendar)
            company_type: Optional company type filter
            max_pages: Maximum pages to fetch
        
        Returns:
            List of board change announcements
            
        Example:
            >>> letters = client.search_board_changes("1402/01/01", "1402/12/29")
        """
        self.reset_params()
        self.set_letter_code('ن-45')
        self.set_date_range(from_date, to_date)
        
        if company_type:
            self.set_company_type(company_type)
        
        self.set_entity_type(include_childs=False, include_mains=True)
        
        return self.fetch_all_pages(max_pages=max_pages)
    
    def search_financial_statements(
        self,
        from_date: str,
        to_date: str,
        period_length: int = 12,
        audited_only: bool = True,
        max_pages: Optional[int] = None
    ) -> List[LetterData]:
        """
        Search for financial statements.
        
        Args:
            from_date: Start date (Persian calendar)
            to_date: End date (Persian calendar)
            period_length: Period length in months (default: 12 for annual)
            audited_only: If True, only return audited statements
            max_pages: Maximum pages to fetch
        
        Returns:
            List of financial statement announcements
        """
        self.reset_params()
        self.set_letter_code('ن-10')
        self.set_date_range(from_date, to_date)
        self.set_period_length(period_length)
        
        if audited_only:
            self.set_audit_status(audited=True, not_audited=False)
        
        return self.fetch_all_pages(max_pages=max_pages)
    
    def search_monthly_reports(
        self,
        from_date: str,
        to_date: str,
        symbol: Optional[str] = None,
        max_pages: Optional[int] = None
    ) -> List[LetterData]:
        """
        Search for monthly activity reports (letter code ن-30).
        
        Args:
            from_date: Start date (Persian calendar)
            to_date: End date (Persian calendar)
            symbol: Optional specific symbol to search
            max_pages: Maximum pages to fetch
        
        Returns:
            List of monthly report announcements
        """
        self.reset_params()
        self.set_letter_code('ن-30')
        self.set_date_range(from_date, to_date)
        
        if symbol:
            self.set_symbol(symbol)
        
        return self.fetch_all_pages(max_pages=max_pages)
    
    def search_by_symbol(
        self, 
        symbol: str, 
        from_date: Optional[str] = None, 
        to_date: Optional[str] = None,
        max_pages: Optional[int] = None
    ) -> List[LetterData]:
        """
        Search all announcements for a specific symbol.
        
        Args:
            symbol: Stock symbol
            from_date: Optional start date
            to_date: Optional end date
            max_pages: Maximum pages to fetch
        
        Returns:
            List of announcements for the symbol
        """
        self.reset_params()
        self.set_symbol(symbol)
        
        if from_date and to_date:
            self.set_date_range(from_date, to_date)
        
        return self.fetch_all_pages(max_pages=max_pages)
    
    def get_summary_stats(self) -> Dict[str, Any]:
        """
        Get summary statistics from queries.
        
        Returns:
            Dictionary with query and cache statistics
        """
        stats = {
            'query': {
                'total_results': self.total_results,
                'total_pages': self.total_pages,
                'current_page': self.current_page,
            },
            'requests': self._stats.copy(),
            'query_params': clean_dict(self.params)
        }
        
        if self.cache:
            stats['cache'] = self.cache.get_stats()
        
        return stats
    
    def get_pagination_info(self) -> Optional[PaginationInfo]:
        """
        Get pagination information from last query.
        
        Returns:
            PaginationInfo or None if no query made
        """
        if self.total_results is None:
            return None
        
        return PaginationInfo(
            current_page=self.current_page,
            total_pages=self.total_pages or 0,
            total_results=self.total_results or 0
        )
    
    # ============== URL Extraction ==============
    
    def extract_letter_urls(self, letters: List[LetterData]) -> List[str]:
        """
        Extract letter URLs from API response.
        
        Args:
            letters: List of letter dictionaries from API
        
        Returns:
            List of letter URLs
        """
        urls = []
        
        for letter in letters:
            try:
                url = self._extract_url_from_letter(letter)
                if url:
                    urls.append(url)
            except Exception as e:
                logger.warning(f"Failed to extract URL from letter: {e}")
        
        return urls
    
    def _extract_url_from_letter(self, letter: LetterData) -> Optional[str]:
        """Extract URL from a single letter"""
        # Check for URL field
        if 'Url' in letter and letter['Url']:
            return build_full_url(letter['Url'])
        
        # Alternative: construct URL from TracingNo
        if 'TracingNo' in letter and letter['TracingNo']:
            tracing_no = letter['TracingNo']
            return f"https://codal.ir/Reports/Decision.aspx?LetterSerial={tracing_no}"
        
        return None
    
    def save_urls_to_csv(
        self, 
        letters: List[LetterData], 
        file_path: str = 'letter_urls.csv'
    ) -> int:
        """
        Save letter URLs to CSV file for further processing.
        
        Args:
            letters: List of letter dictionaries from API
            file_path: Output CSV file path
            
        Returns:
            Number of URLs saved
        """
        data = []
        
        for letter in letters:
            try:
                url = self._extract_url_from_letter(letter)
                
                if url:
                    data.append({
                        'url': url,
                        'symbol': letter.get('Symbol', ''),
                        'company_name': letter.get('CompanyName', ''),
                        'tracing_no': letter.get('TracingNo', ''),
                        'letter_code': letter.get('LetterCode', ''),
                        'sent_date': letter.get('SentDateTime', ''),
                        'publish_date': letter.get('PublishDateTime', '')
                    })
            except Exception as e:
                logger.warning(f"Failed to process letter for CSV: {e}")
        
        if data:
            df = pd.DataFrame(data)
            df.to_csv(file_path, index=False, encoding='utf-8-sig')
            logger.info(f"Saved {len(data)} URLs to {file_path}")
            return len(data)
        else:
            logger.warning("No URLs to save")
            return 0
    
    # ============== File Download ==============
    
    def download_file(self, url: str, file_path: Path) -> bool:
        """
        Download a file from URL.
        
        Args:
            url: File URL
            file_path: Local file path to save
        
        Returns:
            True if successful, False otherwise
        """
        try:
            if self.rate_limiter:
                self.rate_limiter.acquire()
            
            # Use download headers
            response = self.session.get(
                url, 
                timeout=self.timeout,
                headers=DOWNLOAD_HEADERS
            )
            response.raise_for_status()
            
            # Create parent directories
            file_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(file_path, 'wb') as f:
                f.write(response.content)
            
            logger.info(f"Downloaded: {file_path.name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to download {url}: {e}")
            return False
    
    def _extract_excel_links(
        self, 
        html_content: str, 
        base_url: str = "https://codal.ir"
    ) -> List[Dict[str, str]]:
        """
        Extract Excel file links from HTML content.
        
        Args:
            html_content: HTML content from announcement page
            base_url: Base URL for resolving relative links
        
        Returns:
            List of dictionaries with file info (url, filename)
        """
        excel_links = []
        
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            patterns = [r'\.xlsx$', r'\.xls$', r'excel', r'Excel']
            
            for link in soup.find_all('a', href=True):
                href = link.get('href', '')
                text = link.get_text(strip=True)
                
                for pattern in patterns:
                    if re.search(pattern, href, re.IGNORECASE) or \
                       re.search(pattern, text, re.IGNORECASE):
                        
                        full_url = build_full_url(href, base_url)
                        filename = href.split('/')[-1] or text[:50]
                        
                        excel_links.append({
                            'url': full_url,
                            'filename': filename,
                            'text': text
                        })
                        break
            
            # Also check iframes and embeds
            for element in soup.find_all(['iframe', 'embed']):
                src = element.get('src', '')
                if re.search(r'\.xlsx?$', src, re.IGNORECASE):
                    excel_links.append({
                        'url': build_full_url(src, base_url),
                        'filename': src.split('/')[-1],
                        'text': ''
                    })
        
        except Exception as e:
            logger.error(f"Failed to extract Excel links: {e}")
        
        return excel_links
    
    def download_financial_excel_files(
        self,
        from_date: str,
        to_date: str,
        period_length: int = 12,
        audited_only: bool = True,
        output_dir: Union[str, Path] = "financial_excel",
        max_files: Optional[int] = None
    ) -> List[str]:
        """
        Download Excel files from financial statement announcements.
        
        Args:
            from_date: Start date for financial statements
            to_date: End date for financial statements
            period_length: Period length in months (default: 12)
            audited_only: If True, only download from audited statements
            output_dir: Directory to save downloaded files
            max_files: Maximum number of files to download
        
        Returns:
            List of successfully downloaded file paths
        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        # Search for financial statements
        logger.info("Searching for financial statements...")
        announcements = self.search_financial_statements(
            from_date=from_date,
            to_date=to_date,
            period_length=period_length,
            audited_only=audited_only
        )
        
        logger.info(f"Found {len(announcements)} financial statement announcements")
        
        downloaded_files = []
        file_count = 0
        
        for i, announcement in enumerate(announcements):
            if max_files and file_count >= max_files:
                logger.info(f"Reached maximum file limit ({max_files})")
                break
            
            try:
                # Check for Excel URL
                excel_url = announcement.get('ExcelUrl') or announcement.get('excel_url')
                has_excel = announcement.get('HasExcel') or announcement.get('has_excel')
                
                if not excel_url and not has_excel:
                    continue
                
                if excel_url:
                    full_url = build_full_url(excel_url)
                    
                    # Generate filename
                    symbol = announcement.get('Symbol', 'Unknown')
                    date = announcement.get('PublishDateTime', '').replace('/', '-')[:10]
                    filename = f"{symbol}_{date}_{file_count + 1}.xls"
                    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
                    
                    file_path = output_path / filename
                    
                    logger.info(f"[{i+1}/{len(announcements)}] Downloading: {filename}")
                    
                    if self.download_file(full_url, file_path):
                        downloaded_files.append(str(file_path))
                        file_count += 1
                
            except Exception as e:
                logger.error(f"Failed to process announcement: {e}")
                continue
        
        logger.info(f"Downloaded {len(downloaded_files)} Excel files to {output_path}")
        return downloaded_files
    
    def clear_cache(self) -> int:
        """
        Clear all cached responses.
        
        Returns:
            Number of cache entries cleared
        """
        if self.cache:
            return self.cache.invalidate()
        return 0
    
    def reset_stats(self) -> None:
        """Reset all statistics"""
        self._stats = {
            'requests_made': 0,
            'requests_failed': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'total_items_fetched': 0
        }
        if self.cache:
            self.cache.reset_stats()