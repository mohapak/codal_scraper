"""
Type definitions for Codal Scraper

This module contains all TypedDict, dataclass, and Enum definitions
used throughout the library for better type safety.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TypedDict, Optional, List, Dict, Union, Any


# ============== Enums ==============

class CompanyType(Enum):
    """Company listing type on Iranian stock exchanges"""
    BOURSE = "1"           # بورس
    FARABOURSE = "2"       # فرابورس
    PAYE = "3"             # پایه
    ALL = "-1"
    
    @classmethod
    def from_persian(cls, name: str) -> 'CompanyType':
        """Convert Persian name to enum"""
        mapping = {
            "بورس": cls.BOURSE,
            "فرابورس": cls.FARABOURSE,
            "پایه": cls.PAYE,
            "همه": cls.ALL
        }
        return mapping.get(name, cls.ALL)


class AuditStatus(Enum):
    """Audit status for financial statements"""
    AUDITED = "audited"
    NOT_AUDITED = "not_audited"
    BOTH = "both"


class ScrapingErrorType(Enum):
    """Types of scraping errors for categorization"""
    TIMEOUT = "timeout"
    NAVIGATION = "navigation"
    SELECTOR_NOT_FOUND = "selector_not_found"
    PARSING = "parsing"
    NETWORK = "network"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    UNKNOWN = "unknown"


class ExportFormat(Enum):
    """Supported export formats"""
    CSV = "csv"
    EXCEL = "xlsx"
    JSON = "json"
    PARQUET = "parquet"


# ============== TypedDicts ==============

class LetterData(TypedDict, total=False):
    """
    Type definition for letter/announcement data from Codal API
    
    All fields are optional (total=False) as API responses may vary.
    """
    Url: str
    TracingNo: str
    Symbol: str
    CompanyName: str
    Title: str
    LetterCode: str
    SentDateTime: str
    PublishDateTime: str
    HasExcel: bool
    ExcelUrl: Optional[str]
    HasHtml: bool
    HtmlUrl: Optional[str]
    HasPdf: bool
    PdfUrl: Optional[str]
    HasXbrl: bool
    XbrlUrl: Optional[str]
    HasAttachment: bool
    AttachmentUrl: Optional[str]
    IsEstimate: bool
    Audited: bool
    Consolidated: bool
    CompanyState: int
    CompanyType: int


class BoardMemberData(TypedDict):
    """Type definition for board member data extracted from pages"""
    year: str
    date: str
    month: str
    assembly_date: Optional[str]
    company: str
    has_previous: bool
    has_next: bool
    url: str

    prev_member: str
    new_member: str
    member_id: str
    prev_representative: str
    new_representative: str
    national_id: str
    position: str
    is_independent: bool
    degree: str
    major: str
    experience: str  # اهم سوابق (3 سال اخیر)

    # Q12 & Q13 – multiple roles
    has_multiple_executive: bool      # >1 ناشر/نهاد مالی با سمت اجرایی؟
    has_multiple_non_executive: bool  # >3 شرکت ثبت‌شده، عضو هیئت‌مدیره؟

    # Q14–Q16 – governance & qualification
    has_corporate_declaration: bool   # اقرارنامه ماده ۴ حاکمیت شرکتی؟
    has_position_acceptance: bool     # قبولی سمت به کمیته انتصابات؟
    verification_status: str          # وضعیت تایید صلاحیت مدیران نهاد مالی

    ceo_name: str
    ceo_national_id: str
    ceo_degree: str
    ceo_major: str
    scrape_timestamp: str



class QueryParams(TypedDict, total=False):
    """Type definition for API query parameters"""
    PageNumber: int
    Symbol: Union[str, int]
    PublisherStatus: int
    Category: int
    CompanyType: Union[str, int]
    CompanyState: int
    LetterType: int
    Subject: Union[str, int]
    TracingNo: Union[str, int]
    LetterCode: Union[str, int]
    Length: int
    FromDate: Union[str, int]
    ToDate: Union[str, int]
    Audited: str
    NotAudited: str
    Consolidatable: str
    NotConsolidatable: str
    Childs: str
    Mains: str
    AuditorRef: int
    YearEndToDate: Union[str, int]
    Publisher: str
    Isic: Union[str, int]


class APIResponse(TypedDict):
    """Type definition for Codal API response"""
    Letters: List[LetterData]
    Total: int
    Page: int
    IsSuccess: bool


class CacheEntry(TypedDict):
    """Type definition for cache entries"""
    url: str
    params: Optional[Dict[str, Any]]
    data: Any
    created_at: float
    expires_at: float


# ============== Dataclasses ==============

@dataclass
class DateRange:
    """
    Date range for queries with automatic validation
    
    Attributes:
        from_date: Start date in YYYY/MM/DD format (Persian calendar)
        to_date: End date in YYYY/MM/DD format (Persian calendar)
    
    Example:
        >>> date_range = DateRange("1402/01/01", "1402/12/29")
        >>> print(date_range.from_date)
        1402/01/01
    """
    from_date: str
    to_date: str
    
    def __post_init__(self):
        """Validate dates on creation"""
        from .validators import parse_jalali_date
        from .exceptions import ValidationError
        
        # Compare parsed Jalali dates, not strings: "1402/5/15" sorts after
        # "1402/10/15" as a string even though it is the earlier date.
        parsed_from = parse_jalali_date(self.from_date)
        parsed_to = parse_jalali_date(self.to_date)
        
        if parsed_from > parsed_to:
            raise ValidationError(
                f"from_date ({self.from_date}) must be <= to_date ({self.to_date})",
                field="date_range",
            )
    
    def to_dict(self) -> Dict[str, str]:
        """Convert to dictionary for API params"""
        return {
            "FromDate": self.from_date,
            "ToDate": self.to_date
        }
    
    def __iter__(self):
        """Allow unpacking: from_date, to_date = date_range"""
        yield self.from_date
        yield self.to_date


@dataclass
class ScrapingResult:
    """
    Result of a scraping operation
    
    Attributes:
        url: The URL that was scraped
        success: Whether scraping succeeded
        data: Extracted data (if successful)
        error: Error information (if failed)
        duration_ms: Time taken in milliseconds
    """
    url: str
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional['ScrapingErrorInfo'] = None
    duration_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ScrapingErrorInfo:
    """
    Detailed information about a scraping error
    
    Attributes:
        url: The URL that failed
        error_type: Category of error
        message: Human-readable error message
        recoverable: Whether the error can be retried
        attempt: Which attempt number this was
    """
    url: str
    error_type: ScrapingErrorType
    message: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    recoverable: bool = True
    attempt: int = 1
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            'url': self.url,
            'error_type': self.error_type.value,
            'message': self.message,
            'timestamp': self.timestamp,
            'recoverable': self.recoverable,
            'attempt': self.attempt
        }


@dataclass
class PaginationInfo:
    """Pagination information from API response"""
    current_page: int
    total_pages: int
    total_results: int
    results_per_page: int = 10
    
    @property
    def has_next(self) -> bool:
        return self.current_page < self.total_pages
    
    @property
    def has_previous(self) -> bool:
        return self.current_page > 1


@dataclass
class QueryStats:
    """Statistics for a query operation"""
    total_results: int
    total_pages: int
    pages_fetched: int
    items_fetched: int
    failed_pages: int
    duration_seconds: float
    cache_hits: int = 0
    cache_misses: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'total_results': self.total_results,
            'total_pages': self.total_pages,
            'pages_fetched': self.pages_fetched,
            'items_fetched': self.items_fetched,
            'failed_pages': self.failed_pages,
            'duration_seconds': round(self.duration_seconds, 2),
            'cache_hits': self.cache_hits,
            'cache_misses': self.cache_misses,
            'success_rate': round(
                (self.pages_fetched - self.failed_pages) / max(self.pages_fetched, 1) * 100, 2
            )
        }