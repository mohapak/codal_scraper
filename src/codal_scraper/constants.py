"""
Constants and configuration for Codal Scraper

This module contains all constant values, URLs, mappings, and configuration
used throughout the library.
"""

from functools import lru_cache
from typing import Dict, Tuple
from jdatetime import date as jd
from jdatetime import datetime as jdt


# ============== DYNAMIC DATE FUNCTIONS ==============

@lru_cache(maxsize=1)
def get_current_persian_date() -> str:
    """
    Get current date in Persian calendar format.
    
    Returns:
        Current date as string in YYYY/MM/DD format
        
    Example:
        >>> get_current_persian_date()
        '1403/05/15'
    """
    return jdt.now().strftime("%Y/%m/%d")


def get_current_persian_year() -> int:
    """
    Get current Persian calendar year.
    
    Returns:
        Current year as integer
        
    Example:
        >>> get_current_persian_year()
        1403
    """
    return jdt.now().year


def get_year_end_date(year: int) -> str:
    """
    Last day of a Persian (Jalali) calendar year, in YYYY/MM/DD format.

    A Jalali year ends on 12/29, or on 12/30 when it is a leap year (1403, for
    instance, is a leap year).  The leap rule comes from ``jdatetime`` rather
    than from a hand-rolled arithmetic test.

    Args:
        year: Persian calendar year

    Returns:
        End date as YYYY/MM/DD

    Example:
        >>> get_year_end_date(1402)
        '1402/12/29'
        >>> get_year_end_date(1403)
        '1403/12/30'
    """
    # jdatetime exposes isleap() on an instance, not as a class method.
    end_day = 30 if jd(year, 1, 1).isleap() else 29

    return f"{year}/12/{end_day:02d}"


def generate_year_ranges(
    start_year: int = 1390, 
    end_year: int = None
) -> Dict[int, Tuple[str, str]]:
    """
    Generate year date ranges from start_year to end_year.
    
    Args:
        start_year: First year to include (default: 1390)
        end_year: Last year to include (default: current year)
    
    Returns:
        Dictionary mapping year to (start_date, end_date) tuple
        
    Example:
        >>> ranges = generate_year_ranges(1402, 1403)
        >>> ranges[1402]
        ('1402/01/01', '1402/12/29')
        >>> ranges[1403]
        ('1403/01/01', '1403/12/30')
    """
    if end_year is None:
        end_year = get_current_persian_year()
    
    return {
        year: (f"{year}/01/01", get_year_end_date(year))
        for year in range(start_year, end_year + 1)
    }


# Pre-generated year ranges (can be regenerated with generate_year_ranges())
YEAR_RANGES: Dict[int, Tuple[str, str]] = generate_year_ranges(1390)


# ============== URL CONSTANTS ==============

BASE_URL = "codal.ir"
SEARCH_BASE_URL = f"search.{BASE_URL}"

# API endpoints
SEARCH_API_URL = f"https://{SEARCH_BASE_URL}/api/search/v2/q?"
REPORT_LIST_URL = f"https://{BASE_URL}/ReportList.aspx?"
REPORT_PAGE_URL = f"https://{BASE_URL}/Reports/Decision.aspx"

# TSE TMC CDN API
CDN_TSETMC_API = "http://cdn.tsetmc.com/api/{data}/{code}/{date}"

# Alternative endpoints
ATTACHMENT_BASE_URL = f"https://{BASE_URL}/Reports/Attachment.aspx"
EXCEL_BASE_URL = f"https://excel.{BASE_URL}"


# ============== HTTP HEADERS ==============

DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'Origin': f'https://{BASE_URL}',
    'Referer': f'https://{BASE_URL}/',
    'Connection': 'keep-alive',
}

# Headers for downloading files
DOWNLOAD_HEADERS = {
    **DEFAULT_HEADERS,
    'Accept': 'application/octet-stream, application/vnd.ms-excel, application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, */*',
}


# ============== COMPANY TYPES ==============

COMPANY_TYPES: Dict[str, str] = {
    "بورس": "1",
    "فرابورس": "2",
    "پایه": "3",
    "all": "-1",
    "همه": "-1",
    # English aliases
    "bourse": "1",
    "farabourse": "2",
    "otc": "3",
}


# ============== LETTER CODES ==============

LETTER_CODES: Dict[str, str] = {
    # Financial Statements
    "ن-10": "صورت های مالی میان دوره ای",
    "ن-11": "گزارش فعالیت هیئت مدیره",
    "ن-12": "گزارش کنترل داخلی",
    "ن-13": "زمان بندی پرداخت سود",
    
    # Disclosures
    "ن-20": "افشای اطلاعات با اهمیت",
    "ن-21": "شفاف سازی در خصوص شایعه، خبر یا گزارش منتشر شده",
    "ن-22": "شفاف سازی در خصوص نوسان قیمت سهام",
    "ن-23": "اطلاعات حاصل از برگزاری کنفرانس اطلاع رسانی",
    "ن-24": "درخواست ارایه مهلت جهت رعایت ماده ۱۹ مکرر ۱/ ۱۲ مکرر ۴ دستورالعمل اجرایی",
    "ن-25": "برنامه ناشر جهت خروج از شمول ماده ۱۴۱ لایحه قانونی اصلاح قسمتی از قانون تجارت",
    "ن-26": "توضیحات در خصوص اطلاعات و صورت های مالی منتشر شده",
    
    # Activity Reports
    "ن-30": "گزارش فعالیت ماهانه",
    
    # Corporate Governance
    "ن-41": "مشخصات کمیته حسابرسی و واحد حسابرسی داخلی",
    "ن-42": "آگهی ثبت تصمیمات مجمع عادی سالیانه",
    "ن-43": "اساسنامه شرکت مصوب مجمع عمومی فوق العاده",
    "ن-45": "معرفی/تغییر در ترکیب اعضای هیئت‌مدیره",
    
    # General Assemblies
    "ن-50": "آگهی دعوت به مجمع عمومی عادی سالیانه",
    "ن-51": "خلاصه تصمیمات مجمع عمومی عادی سالیانه",
    "ن-52": "تصمیمات مجمع عمومی عادی سالیانه",
    "ن-53": "آگهی دعوت به مجمع عمومی عادی سالیانه نوبت دوم",
    "ن-54": "آگهی دعوت به مجمع عمومی عادی بطور فوق العاده",
    "ن-55": "تصمیمات مجمع عمومی عادی بطور فوق العاده",
    "ن-56": "آگهی دعوت به مجمع عمومی فوق العاده",
    "ن-57": "تصمیمات مجمع عمومی فوق‌العاده",
    "ن-58": "لغو آگهی (اطلاعیه) دعوت به مجمع عمومی",
    "ن-59": "مجوز بانک مرکزی جهت برگزرای مجمع عمومی عادی سالیانه",
    
    # Capital Increase
    "ن-60": "پیشنهاد هیئت مدیره به مجمع عمومی فوق العاده در خصوص افزایش سرمایه",
    "ن-61": "اظهارنظر حسابرس و بازرس قانونی نسبت به گزارش توجیهی هیئت مدیره",
    "ن-62": "مدارک و مستندات درخواست افزایش سرمایه",
    "ن-63": "تمدید مهلت استفاده از مجوز افزایش سرمایه",
    "ن-64": "مهلت استفاده از حق تقدم خرید سهام",
    "ن-65": "اعلامیه پذیره نویسی عمومی",
    "ن-66": "نتایج حاصل از فروش حق تقدم های استفاده نشده",
    "ن-67": "آگهی ثبت افزایش سرمایه",
    "ن-69": "توزیع گواهی نامه نقل و انتقال و سپرده سهام",
    "ن-70": "تصمیم هیئت مدیره به انجام افزایش سرمایه تفویض شده",
    "ن-71": "زمان تشکیل جلسه هیئت‌مدیره در خصوص افزایش سرمایه",
    "ن-72": "لغو اطلاعیه زمان تشکیل جلسه هیئت مدیره در خصوص افزایش سرمایه",
    "ن-73": "تصمیمات هیئت‌مدیره در خصوص افزایش سرمایه",
    
    # Miscellaneous
    "ن-80": "تغییر نشانی",
    "ن-81": "درخواست تکمیل مشخصات سهامداران",
}

# Reverse mapping: description to code
LETTER_DESCRIPTIONS_TO_CODES: Dict[str, str] = {v: k for k, v in LETTER_CODES.items()}


# ============== PERIOD LENGTHS ==============

PERIOD_LENGTHS: Dict[any, int] = {
    # Special values
    None: -1,
    -1: -1,
    0: -1,
    'همه موارد': -1,
    'all': -1,
    
    # Persian digits
    '۱': 1, '۲': 2, '۳': 3, '۴': 4, '۵': 5, '۶': 6,
    '۷': 7, '۸': 8, '۹': 9, '۱۰': 10, '۱۱': 11, '۱۲': 12,
    
    # Arabic/English digits
    1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6,
    7: 7, 8: 8, 9: 9, 10: 10, 11: 11, 12: 12,
    
    # String versions
    '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6,
    '7': 7, '8': 8, '9': 9, '10': 10, '11': 11, '12': 12,
}


# ============== CHARACTER MAPPINGS ==============

FA_TO_EN_DIGITS: Dict[str, str] = {
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
}

AR_TO_EN_DIGITS: Dict[str, str] = {
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
}

AR_TO_FA_LETTER: Dict[str, str] = {
    "ي": "ی",
    "ك": "ک",
    "ة": "ه",
    "ؤ": "و",
    "إ": "ا",
    "أ": "ا",
    "ء": "",
}


# ============== CSS SELECTORS ==============

# Default selectors for board member pages (Codal ن-45 letters)
BOARD_MEMBER_SELECTORS: Dict[str, str] = {
    # Data rows in the board-member grid
    # - Prefer .GridItem / .GridAlternating (your current layout)
    # - Also fall back to "all rows except .GridHeader" in case Codal changes classes
    "table": (
        "#dgAssemblyBoardMember tr.GridItem, "
        "#dgAssemblyBoardMember tr.GridAlternating, "
        "#dgAssemblyBoardMember > tbody > tr:not(.GridHeader)"
    ),

    # Fallback: all rows in the table (handy for debugging / future use)
    "table_all": (
        "#dgAssemblyBoardMember tr, "
        "#dgAssemblyBoardMember > tbody > tr"
    ),

    # Meta information (company name + symbol)
    "company": "#lblCompany, span#lblCompany",

    # CEO block under «مشخصات مدیرعامل»
    "ceo_name": (
        "#lblDMBoardMemberList, "
        "span#lblDMBoardMemberList"
    ),
    "ceo_national_id": (
        "#lblDMBoardMemberNationalCode, "
        "span#lblDMBoardMemberNationalCode"
    ),
    "ceo_degree": (
        "#lblDMBoardMemberDegreeLevel, "
        "span#lblDMBoardMemberDegreeLevel"
    ),
    "ceo_major": (
        "#lblDMBoardMemberEducationField, "
        "span#lblDMBoardMemberEducationField"
    ),

    # Dates: جلسه هیئت مدیره (txbSessionDate) و تاریخ مجمع (lblAssemblyDate)
    "date": "#txbSessionDate, span#txbSessionDate",
    "assembly_date": "#lblAssemblyDate, span#lblAssemblyDate",

    # Previous / next version links (may be missing on some pages)
    "prev_version": (
        "#ucNavigateToNextPrevLetter_hlPrevVersion, "
        "a#ucNavigateToNextPrevLetter_hlPrevVersion"
    ),
    "next_version": (
        "#ucNavigateToNextPrevLetter_hlNewVersion, "
        "a#ucNavigateToNextPrevLetter_hlNewVersion"
    ),
}




# ============== DEFAULT CONFIGURATIONS ==============

DEFAULT_TIMEOUT = 30  # seconds
DEFAULT_RETRY_COUNT = 3
DEFAULT_RATE_LIMIT = 2.0  # requests per second
DEFAULT_MAX_CONCURRENT = 5
DEFAULT_CACHE_TTL = 3600  # 1 hour
DEFAULT_CACHE_DIR = ".codal_cache"