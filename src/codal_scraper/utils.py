"""
Utility functions for data processing and conversion

This module provides helper functions for text normalization, date conversion,
data cleaning, and other common operations.
"""

import re
import logging
from typing import Any, Dict, Iterator, List, Optional, Sized, Tuple, Union

import pandas as pd
from jdatetime import date as jd
from jdatetime import datetime as jdt

from .constants import (
    FA_TO_EN_DIGITS,
    AR_TO_EN_DIGITS,
    AR_TO_FA_LETTER,
    get_year_end_date,
)


logger = logging.getLogger(__name__)


# ============== Dictionary Utilities ==============

def clean_dict(dictionary: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remove None values from dictionary.

    ``-1`` is a meaningful sentinel in the Codal query API ("all" - see
    ``constants.COMPANY_TYPES``), so only ``None`` is dropped.  The identity
    test also avoids the ambiguous ``ValueError`` that ``v not in [...]``
    raises for numpy arrays.
    
    Args:
        dictionary: Input dictionary to clean
        
    Returns:
        New dictionary without None or -1 values
        
    Example:
        >>> clean_dict({'a': 1, 'b': -1, 'c': None})
        {'a': 1, 'b': -1}
    """
    return {k: v for k, v in dictionary.items() if v is not None}


def safe_get(dictionary: Dict, *keys, default: Any = None) -> Any:
    """
    Safely get nested dictionary values.
    
    Args:
        dictionary: The dictionary to search
        *keys: Keys to traverse
        default: Default value if key not found
    
    Returns:
        Value at the nested key or default
        
    Example:
        >>> d = {'a': {'b': {'c': 1}}}
        >>> safe_get(d, 'a', 'b', 'c')
        1
        >>> safe_get(d, 'a', 'x', default='not found')
        'not found'
    """
    value = dictionary
    for key in keys:
        if isinstance(value, dict):
            value = value.get(key, default)
        else:
            return default
    return value


def merge_dicts(*dicts: Dict) -> Dict:
    """
    Merge multiple dictionaries, later ones override earlier.
    
    Args:
        *dicts: Dictionaries to merge
        
    Returns:
        Merged dictionary
    """
    result = {}
    for d in dicts:
        if d:
            result.update(d)
    return result


# ============== Text Normalization ==============

def normalize_persian_text(text: str) -> str:
    """
    Normalize Persian/Arabic characters for consistency.
    
    This function:
    - Converts Arabic characters to Persian equivalents
    - Turns the zero-width non-joiner (U+200C) into a space, so ZWNJ and
      plain-space spellings normalise identically
    - Removes the remaining zero-width / bidi-control characters
    - Normalizes whitespace
    
    Args:
        text: Input text to normalize
        
    Returns:
        Normalized text
        
    Example:
        >>> normalize_persian_text("كتاب يوسف")
        'کتاب یوسف'
    """
    if not text:
        return ''
    
    text = str(text)
    
    # Convert Arabic to Persian letters
    for ar, fa in AR_TO_FA_LETTER.items():
        text = text.replace(ar, fa)
    
    # ZWNJ is a word separator in Persian, not noise: replacing it with a
    # space keeps "غیر\u200cموظف" equal to "غیر موظف".  The other zero-width
    # and bidi-control characters carry no meaning, so they are dropped.
    text = text.replace('\u200c', ' ')
    for char in ['\u200f', '\u200e', '\u200d', '\u200b', '\ufeff']:
        text = text.replace(char, '')
    
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text


def persian_to_english_digits(text: str) -> str:
    """
    Convert Persian/Arabic digits to English (ASCII) digits.
    
    Args:
        text: Input text with Persian/Arabic digits
        
    Returns:
        Text with English digits
        
    Example:
        >>> persian_to_english_digits("۱۳۹۸/۰۵/۱۵")
        '1398/05/15'
    """
    if not text:
        return text
    
    text = str(text)
    
    # Convert Persian digits
    for fa, en in FA_TO_EN_DIGITS.items():
        text = text.replace(fa, en)
    
    # Convert Arabic digits
    for ar, en in AR_TO_EN_DIGITS.items():
        text = text.replace(ar, en)
    
    return text


def english_to_persian_digits(text: str) -> str:
    """
    Convert English digits to Persian digits.
    
    Args:
        text: Input text with English digits
        
    Returns:
        Text with Persian digits
        
    Example:
        >>> english_to_persian_digits("1398/05/15")
        '۱۳۹۸/۰۵/۱۵'
    """
    if not text:
        return text
    
    text = str(text)
    en_to_fa = {v: k for k, v in FA_TO_EN_DIGITS.items()}
    
    for en, fa in en_to_fa.items():
        text = text.replace(en, fa)
    
    return text


def clean_symbol(symbol: str) -> str:
    """
    Clean and normalize stock symbol.
    
    Args:
        symbol: Stock symbol to clean
        
    Returns:
        Cleaned symbol
        
    Example:
        >>> clean_symbol("  فولاد۱  ")
        'فولاد1'
    """
    if not symbol:
        return ""
    
    # Strip whitespace
    symbol = symbol.strip()
    
    # Convert Persian digits to English
    symbol = persian_to_english_digits(symbol)
    
    # Normalize Persian text
    symbol = normalize_persian_text(symbol)
    
    # Remove special characters (keep alphanumeric and Persian letters)
    symbol = re.sub(r'[^\w\u0600-\u06FF]+', '', symbol)
    
    # Uppercase only English letters (Persian doesn't have case)
    return ''.join(
        c.upper() if c.isascii() and c.isalpha() else c
        for c in symbol
    )


def is_independent_duty(duty_text: str) -> bool:
    """
    True when a board-member duty cell marks the member as non-executive
    ("غیر موظف").

    Codal may render the duty with a plain space or with a ZWNJ, so the text is
    normalised first (ZWNJ -> space, spaces then removed for the comparison),
    which makes both spellings match.  The "غیر" negation is part of the matched
    token, so a plain "موظف" (executive) is not classified as independent.

    Args:
        duty_text: Raw duty cell text

    Returns:
        True if the member is non-executive / independent

    Example:
        >>> is_independent_duty("غیر موظف")
        True
        >>> is_independent_duty("غير‌موظف")
        True
        >>> is_independent_duty("موظف")
        False
    """
    if not duty_text:
        return False

    compact = normalize_persian_text(str(duty_text)).replace(' ', '')

    return 'غیرموظف' in compact


# ============== Date Utilities ==============

# Accepted Jalali input shapes.  Each component is captured separately so it
# can be validated before a timestamp is assembled - no field is ever invented.
_JALALI_DATETIME_RE = re.compile(
    r'^\s*(?P<year>\d{4})[/\-\.](?P<month>\d{1,2})[/\-\.](?P<day>\d{1,2})'
    r'(?:[ T]+(?P<hour>\d{1,2}):(?P<minute>\d{1,2})(?::(?P<second>\d{1,2}))?)?\s*$'
)
_JALALI_COMPACT_DATETIME_RE = re.compile(
    r'^(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})'
    r'(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})$'
)
_JALALI_COMPACT_DATE_RE = re.compile(
    r'^(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})$'
)


def datetime_to_num(dt: Union[str, None]) -> Optional[int]:
    """
    Convert a Jalali datetime string to numeric format (YYYYMMDDHHmmss).

    The string is parsed field by field and validated against the Persian
    calendar before the timestamp is assembled, so a component is never
    invented: an unparseable or impossible date (e.g. ``"1402/51/15"``, which
    used to return the month-51 value ``14025115000000``) returns ``None``.

    Unpadded components are accepted and zero-padded (``"1402/5/15"`` ->
    ``14020515000000``), matching ``InputValidator.is_date``, which accepts the
    unpadded form too.

    Args:
        dt: Datetime string - ``YYYY/MM/DD[ HH:MM[:SS]]``, ``YYYYMMDD`` or
            ``YYYYMMDDHHmmss``

    Returns:
        Integer representation or None if conversion fails

    Example:
        >>> datetime_to_num("1402/05/15 10:30:00")
        14020515103000
        >>> datetime_to_num("1402/5/15")
        14020515000000
        >>> datetime_to_num("1402/51/15") is None
        True
    """
    if not dt or dt == "":
        return None

    text = str(dt).strip()

    match = None
    for pattern in (
        _JALALI_DATETIME_RE,
        _JALALI_COMPACT_DATETIME_RE,
        _JALALI_COMPACT_DATE_RE,
    ):
        match = pattern.match(text)
        if match:
            break

    if not match:
        logger.warning(f"Unparseable Jalali datetime {dt!r}: returning None")
        return None

    parts = match.groupdict()
    year = int(parts['year'])
    month = int(parts['month'])
    day = int(parts['day'])

    # Calendar validation - this is what stops month 51 / day 32 from ever
    # becoming part of a dataset value.
    try:
        jd(year, month, day)
    except ValueError as e:
        logger.warning(f"Invalid Jalali date {dt!r}: {e}")
        return None

    hour = int(parts.get('hour') or 0)
    minute = int(parts.get('minute') or 0)
    second = int(parts.get('second') or 0)

    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        logger.warning(f"Invalid time component in Jalali datetime {dt!r}: returning None")
        return None

    return int(f"{year:04d}{month:02d}{day:02d}{hour:02d}{minute:02d}{second:02d}")


def year_month_from_date(dt: Union[str, None]) -> Tuple[str, str]:
    """
    Derive the (year, month) of a Jalali date/time string, zero-padded.

    Returns ``("", "")`` when the value cannot be parsed or is not a real
    Jalali date, so a malformed cell can never contribute an invented year or
    month (the ``"1402/5/15" -> month 51`` class of bug) to a dataset row.

    Args:
        dt: Datetime string

    Returns:
        Tuple of (year, month) as zero-padded strings, or ("", "")

    Example:
        >>> year_month_from_date("1402/5/15")
        ('1402', '05')
        >>> year_month_from_date("1402/51/15")
        ('', '')
    """
    num = datetime_to_num(dt)
    if num is None:
        return "", ""

    digits = f"{num:014d}"
    year, month = digits[:4], digits[4:6]

    if not (1 <= int(month) <= 12):
        return "", ""

    return year, month


def num_to_datetime(
    num: Union[int, str],
    include_time: bool = True,
    date_sep: str = "/",
    time_sep: str = ":",
    dt_sep: str = " "
) -> str:
    """
    Convert numeric datetime to string format.
    
    Args:
        num: Numeric datetime (YYYYMMDDHHmmss)
        include_time: If True, include time component
        date_sep: Separator for date components
        time_sep: Separator for time components
        dt_sep: Separator between date and time
    
    Returns:
        Formatted datetime string
        
    Example:
        >>> num_to_datetime(14020515103000)
        '1402/05/15 10:30:00'
    """
    num_str = str(num).zfill(14)
    
    date_part = f"{num_str[0:4]}{date_sep}{num_str[4:6]}{date_sep}{num_str[6:8]}"
    
    if include_time:
        time_part = f"{num_str[8:10]}{time_sep}{num_str[10:12]}{time_sep}{num_str[12:14]}"
        return f"{date_part}{dt_sep}{time_part}"
    
    return date_part


def gregorian_to_shamsi(date: Union[str, int]) -> str:
    """
    Convert Gregorian date (YYYYMMDD) to Shamsi (Persian) calendar.
    
    Args:
        date: Gregorian date as YYYYMMDD string or integer
        
    Returns:
        Persian date as YYYY/MM/DD string
        
    Example:
        >>> gregorian_to_shamsi("20230815")
        '1402/05/24'
    """
    date_str = str(date)
    
    if len(date_str) != 8:
        raise ValueError(f"Date must be in YYYYMMDD format, got: {date_str}")
    
    year = int(date_str[:4])
    month = int(date_str[4:6])
    day = int(date_str[6:8])
    
    shamsi_date = jd.fromgregorian(day=day, month=month, year=year)
    return shamsi_date.strftime("%Y/%m/%d")


def shamsi_to_gregorian(date: str) -> str:
    """
    Convert Shamsi (Persian) date to Gregorian (YYYYMMDD).
    
    Args:
        date: Persian date as YYYY/MM/DD or YYYYMMDD string
        
    Returns:
        Gregorian date as YYYYMMDD string
        
    Example:
        >>> shamsi_to_gregorian("1402/05/24")
        '20230815'
    """
    # Remove separators
    date_clean = date.replace('/', '')
    
    # Parse the Shamsi date
    date_obj = jdt.strptime(date_clean, "%Y%m%d")
    
    # Convert to Gregorian
    greg_date = date_obj.togregorian()
    
    return greg_date.strftime("%Y%m%d")


def calculate_date_range(year: int) -> tuple:
    """
    Calculate the start and end dates for a Persian calendar year.
    
    Args:
        year: Persian calendar year
    
    Returns:
        Tuple of (start_date, end_date) in YYYY/MM/DD format
        
    Example:
        >>> calculate_date_range(1402)
        ('1402/01/01', '1402/12/29')
        >>> calculate_date_range(1403)
        ('1403/01/01', '1403/12/30')
    """
    start_date = f"{year}/01/01"
    end_date = get_year_end_date(year)
    
    return start_date, end_date


def is_valid_persian_date(date_str: str) -> bool:
    """
    Check if a string is a valid Persian date.
    
    Args:
        date_str: Date string to validate
        
    Returns:
        True if valid, False otherwise
    """
    try:
        from .validators import InputValidator
        InputValidator(date_str).is_date()
        return True
    except Exception:
        return False


# ============== Value Conversion ==============

def value_to_float(value: Union[str, int, float]) -> float:
    """
    Convert formatted values to float.
    Handles K (thousands), M (millions), B (billions) suffixes.
    
    Args:
        value: Value to convert
        
    Returns:
        Float value, or ``float("nan")`` when the value cannot be parsed.
        Returning NaN (rather than 0.0) keeps a missing or malformed figure
        distinct from a genuine zero in the resulting dataset.
        
    Example:
        >>> value_to_float("1.5M")
        1500000.0
        >>> value_to_float("invalid")
        nan
    """
    if isinstance(value, (int, float)):
        return float(value)
    
    if not isinstance(value, str):
        return float("nan")
    
    # Remove commas and whitespace
    value = value.replace(',', '').replace(' ', '').strip()
    
    if not value:
        return float("nan")
    
    # Handle suffixes
    multipliers = {
        'K': 1_000,
        'M': 1_000_000,
        'B': 1_000_000_000,
        'T': 1_000_000_000_000,
        'k': 1_000,
        'm': 1_000_000,
        'b': 1_000_000_000,
        't': 1_000_000_000_000,
    }
    
    for suffix, multiplier in multipliers.items():
        if value.endswith(suffix):
            try:
                number = float(value[:-1].strip())
                return number * multiplier
            except ValueError:
                return float("nan")
    
    try:
        return float(value)
    except ValueError:
        return float("nan")


def format_number(number: Union[int, float], decimal_places: int = 0) -> str:
    """
    Format number with thousand separators (Persian style).
    
    Args:
        number: Number to format
        decimal_places: Number of decimal places
    
    Returns:
        Formatted number string
        
    Example:
        >>> format_number(1234567)
        '1,234,567'
    """
    if pd.isna(number):
        return ""
    
    try:
        if decimal_places > 0:
            return f"{float(number):,.{decimal_places}f}"
        else:
            return f"{int(number):,}"
    except (ValueError, TypeError):
        return str(number)


# ============== String Utilities ==============

def to_snake_case(name: str) -> str:
    """
    Convert CamelCase or PascalCase to snake_case.
    
    Args:
        name: String to convert
        
    Returns:
        snake_case string
        
    Example:
        >>> to_snake_case("PublishDateTime")
        'publish_date_time'
    """
    # Insert underscore before uppercase letters
    name = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    name = re.sub('__([A-Z])', r'_\1', name)
    name = re.sub('([a-z0-9])([A-Z])', r'\1_\2', name)
    
    return name.lower()


def to_camel_case(name: str) -> str:
    """
    Convert snake_case to camelCase.
    
    Args:
        name: String to convert
        
    Returns:
        camelCase string
        
    Example:
        >>> to_camel_case("publish_date_time")
        'publishDateTime'
    """
    components = name.split('_')
    return components[0] + ''.join(x.title() for x in components[1:])


# ============== DataFrame Utilities ==============

def dataframe_columns_to_snake_case(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert all DataFrame column names to snake_case.
    
    Args:
        df: DataFrame to process
        
    Returns:
        DataFrame with snake_case column names
    """
    df = df.copy()
    df.columns = [to_snake_case(col) for col in df.columns]
    return df


def parse_codal_response(response: Dict) -> pd.DataFrame:
    """
    Parse Codal API response into a DataFrame.
    
    Args:
        response: API response dictionary
    
    Returns:
        DataFrame with parsed data
    """
    if not response or 'Letters' not in response:
        return pd.DataFrame()
    
    letters = response['Letters']
    
    if not letters:
        return pd.DataFrame()
    
    # Convert to DataFrame
    df = pd.DataFrame(letters)
    
    # Convert column names to snake_case
    df = dataframe_columns_to_snake_case(df)
    
    return df


# ============== Collection Utilities ==============

def chunk_list(lst: List, chunk_size: int) -> Iterator[List]:
    """
    Split a list into chunks of specified size.
    
    Args:
        lst: List to split
        chunk_size: Size of each chunk
        
    Yields:
        Chunks of the list
        
    Example:
        >>> list(chunk_list([1, 2, 3, 4, 5], 2))
        [[1, 2], [3, 4], [5]]
    """
    for i in range(0, len(lst), chunk_size):
        yield lst[i:i + chunk_size]


def flatten_list(nested_list: List[List]) -> List:
    """
    Flatten a nested list.
    
    Args:
        nested_list: List of lists
        
    Returns:
        Flattened list
    """
    return [item for sublist in nested_list for item in sublist]


def unique_preserve_order(lst: List) -> List:
    """
    Remove duplicates from list while preserving order.
    
    Args:
        lst: List with potential duplicates
        
    Returns:
        List with duplicates removed
    """
    seen = set()
    result = []
    for item in lst:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


# ============== Progress Utilities ==============

def progress_iterator(
    items: Sized,
    desc: str = "Processing",
    disable: bool = False,
    **kwargs
) -> Iterator:
    """
    Wrap iterable with progress bar if tqdm is available.
    
    Args:
        items: Iterable to wrap
        desc: Progress bar description
        disable: If True, disable progress bar
        **kwargs: Additional arguments for tqdm
        
    Returns:
        Iterator (with or without progress bar)
    """
    try:
        from tqdm import tqdm
        return tqdm(items, desc=desc, disable=disable, **kwargs)
    except ImportError:
        return iter(items)


# ============== URL Utilities ==============

def build_full_url(path: str, base_url: str = "https://codal.ir") -> str:
    """
    Build full URL from a path.
    
    Args:
        path: URL path (may be relative or absolute)
        base_url: Base URL to prepend if path is relative
        
    Returns:
        Full URL
    """
    if not path:
        return ""
    
    if path.startswith('http://') or path.startswith('https://'):
        return path
    
    if path.startswith('/'):
        return f"{base_url}{path}"
    
    return f"{base_url}/{path}"


def extract_tracing_no_from_url(url: str) -> Optional[str]:
    """
    Extract tracing number from Codal URL.
    
    Args:
        url: Codal report URL
        
    Returns:
        Tracing number or None
    """
    if not url:
        return None
    
    patterns = [
        r'LetterSerial=(\d+)',
        r'TracingNo=(\d+)',
        r'/(\d+)$'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None