"""
Custom exceptions for Codal Scraper

This module defines a hierarchy of exceptions for better error handling
throughout the library.
"""

from typing import Optional, Dict, Any


class CodalScraperError(Exception):
    """Base exception for all Codal Scraper errors"""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)
    
    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert exception to dictionary for logging/serialization"""
        return {
            "error_type": self.__class__.__name__,
            "message": self.message,
            "details": self.details
        }


class ValidationError(CodalScraperError):
    """Raised when input validation fails"""
    
    def __init__(self, message: str, field: Optional[str] = None, value: Any = None):
        details = {}
        if field:
            details["field"] = field
        if value is not None:
            details["value"] = str(value)[:100]  # Truncate long values
        super().__init__(message, details)
        self.field = field
        self.value = value


class APIError(CodalScraperError):
    """Raised when API request fails"""
    
    def __init__(
        self, 
        message: str, 
        url: Optional[str] = None,
        status_code: Optional[int] = None,
        response_text: Optional[str] = None
    ):
        details = {}
        if url:
            details["url"] = url
        if status_code:
            details["status_code"] = status_code
        if response_text:
            details["response"] = response_text[:500]  # Truncate
        super().__init__(message, details)
        self.url = url
        self.status_code = status_code
        self.response_text = response_text


class RateLimitError(APIError):
    """Raised when rate limit is exceeded"""
    
    def __init__(
        self, 
        message: str = "Rate limit exceeded",
        retry_after: Optional[float] = None,
        **kwargs
    ):
        super().__init__(message, **kwargs)
        self.retry_after = retry_after
        if retry_after:
            self.details["retry_after"] = retry_after


class ScrapingError(CodalScraperError):
    """Raised when web scraping fails"""
    
    def __init__(
        self,
        message: str,
        url: Optional[str] = None,
        selector: Optional[str] = None,
        recoverable: bool = True
    ):
        details = {}
        if url:
            details["url"] = url
        if selector:
            details["selector"] = selector
        details["recoverable"] = recoverable
        super().__init__(message, details)
        self.url = url
        self.selector = selector
        self.recoverable = recoverable


class CacheError(CodalScraperError):
    """Raised when cache operations fail"""
    
    def __init__(self, message: str, cache_key: Optional[str] = None):
        details = {}
        if cache_key:
            details["cache_key"] = cache_key
        super().__init__(message, details)
        self.cache_key = cache_key


class ConfigurationError(CodalScraperError):
    """Raised when configuration is invalid"""
    pass


class NetworkError(CodalScraperError):
    """Raised when network connection fails"""
    
    def __init__(self, message: str, url: Optional[str] = None, cause: Optional[Exception] = None):
        details = {}
        if url:
            details["url"] = url
        if cause:
            details["cause"] = str(cause)
        super().__init__(message, details)
        self.url = url
        self.cause = cause


class ParseError(CodalScraperError):
    """Raised when parsing response data fails"""
    
    def __init__(self, message: str, content_type: Optional[str] = None):
        details = {}
        if content_type:
            details["content_type"] = content_type
        super().__init__(message, details)
        self.content_type = content_type

class PaginationError(CodalScraperError):
    """
    Raised when pagination cannot be completed consistently with the API's own
    metadata (see P0-3: the response's ``Page`` field is ambiguous).

    Carries the numbers needed to judge the damage: ``expected`` (what the API
    said existed) and ``collected`` (what actually arrived).
    """

    def __init__(
        self,
        message: str,
        expected: Optional[int] = None,
        collected: Optional[int] = None,
        pages_fetched: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        details = dict(details or {})
        if expected is not None:
            details["expected"] = expected
        if collected is not None:
            details["collected"] = collected
        if pages_fetched is not None:
            details["pages_fetched"] = pages_fetched
        super().__init__(message, details)
        self.expected = expected
        self.collected = collected
        self.pages_fetched = pages_fetched


class IncompleteResultsError(PaginationError):
    """Fewer items arrived than the API's ``Total`` claimed."""


class PaginationCapExceededError(PaginationError):
    """A page cap was hit before the reported items could be collected."""
