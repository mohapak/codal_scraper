"""
Tests for utils module
"""

import math

import pytest
import pandas as pd

from codal_scraper.constants import YEAR_RANGES, generate_year_ranges
from codal_scraper.utils import (
    calculate_date_range,
    clean_dict,
    normalize_persian_text,
    is_independent_duty,
    year_month_from_date,
    persian_to_english_digits,
    datetime_to_num,
    num_to_datetime,
    clean_symbol,
    to_snake_case,
    dataframe_columns_to_snake_case,
    value_to_float,
    chunk_list,
    safe_get,
    build_full_url,
)


class TestCleanDict:
    """Tests for clean_dict function"""
    
    def test_removes_none(self):
        """Test removal of None values"""
        result = clean_dict({'a': 1, 'b': None, 'c': 3})
        assert result == {'a': 1, 'c': 3}
    
    def test_keeps_minus_one_sentinel(self):
        """-1 is the API "all" sentinel, so it must survive cleaning (P2)"""
        result = clean_dict({'a': 1, 'b': -1, 'c': 3})
        assert result == {'a': 1, 'b': -1, 'c': 3}
    
    def test_keeps_zero(self):
        """Test that 0 is kept"""
        result = clean_dict({'a': 0, 'b': -1})
        assert result == {'a': 0, 'b': -1}
    
    def test_empty_dict(self):
        """Test with empty dictionary"""
        result = clean_dict({})
        assert result == {}


class TestNormalizePersianText:
    """Tests for normalize_persian_text function"""
    
    def test_arabic_to_persian(self):
        """Test Arabic to Persian conversion"""
        result = normalize_persian_text("كتاب يوسف")
        assert result == "کتاب یوسف"
    
    def test_removes_zero_width(self):
        """Test removal of zero-width characters"""
        text = "تست\u200cمتن"  # With ZWNJ
        result = normalize_persian_text(text)
        assert '\u200c' not in result
    
    def test_normalizes_whitespace(self):
        """Test whitespace normalization"""
        result = normalize_persian_text("  تست    متن  ")
        assert result == "تست متن"
    
    def test_empty_string(self):
        """Test with empty string"""
        assert normalize_persian_text("") == ""
        assert normalize_persian_text(None) == ""


class TestPersianToEnglishDigits:
    """Tests for persian_to_english_digits function"""
    
    def test_persian_digits(self):
        """Test Persian digit conversion"""
        result = persian_to_english_digits("۱۳۹۸/۰۵/۱۵")
        assert result == "1398/05/15"
    
    def test_mixed_digits(self):
        """Test mixed Persian and English digits"""
        result = persian_to_english_digits("۱۲3۴5")
        assert result == "12345"
    
    def test_no_digits(self):
        """Test text without digits"""
        result = persian_to_english_digits("متن تست")
        assert result == "متن تست"
    
    def test_empty_string(self):
        """Test with empty string"""
        assert persian_to_english_digits("") == ""


class TestDatetimeToNum:
    """Tests for datetime_to_num function"""
    
    def test_full_datetime(self):
        """Test full datetime conversion"""
        result = datetime_to_num("1402/05/15 10:30:00")
        assert result == 14020515103000
    
    def test_date_only(self):
        """Test date only (no time)"""
        result = datetime_to_num("1402/05/15")
        assert str(result).startswith("14020515")
    
    def test_empty_input(self):
        """Test empty input"""
        assert datetime_to_num("") is None
        assert datetime_to_num(None) is None


class TestNumToDatetime:
    """Tests for num_to_datetime function"""
    
    def test_with_time(self):
        """Test conversion with time"""
        result = num_to_datetime(14020515103000)
        assert result == "1402/05/15 10:30:00"
    
    def test_without_time(self):
        """Test conversion without time"""
        result = num_to_datetime(14020515103000, include_time=False)
        assert result == "1402/05/15"
    
    def test_custom_separators(self):
        """Test with custom separators"""
        result = num_to_datetime(
            14020515103000,
            date_sep="-",
            time_sep=".",
            dt_sep=" @ "
        )
        assert result == "1402-05-15 @ 10.30.00"


class TestCleanSymbol:
    """Tests for clean_symbol function"""
    
    def test_strips_whitespace(self):
        """Test whitespace stripping"""
        result = clean_symbol("  فولاد  ")
        assert result == "فولاد"
    
    def test_converts_persian_digits(self):
        """Test Persian digit conversion"""
        result = clean_symbol("فولاد۱")
        assert result == "فولاد1"
    
    def test_removes_special_chars(self):
        """Test special character removal"""
        result = clean_symbol("فولاد@#!")
        assert result == "فولاد"
    
    def test_empty_string(self):
        """Test empty string"""
        assert clean_symbol("") == ""
        assert clean_symbol(None) == ""


class TestToSnakeCase:
    """Tests for to_snake_case function"""
    
    @pytest.mark.parametrize("input_str,expected", [
        ("PublishDateTime", "publish_date_time"),
        ("Symbol", "symbol"),
        ("HasExcel", "has_excel"),
        ("already_snake", "already_snake"),
        ("ABC", "a_b_c"),
    ])
    def test_conversions(self, input_str, expected):
        """Test various snake_case conversions"""
        assert to_snake_case(input_str) == expected


class TestDataframeColumnsToSnakeCase:
    """Tests for dataframe_columns_to_snake_case function"""
    
    def test_converts_columns(self):
        """Test column name conversion"""
        df = pd.DataFrame({
            'FirstName': [1],
            'LastName': [2],
            'already_snake': [3]
        })
        
        result = dataframe_columns_to_snake_case(df)
        
        assert 'first_name' in result.columns
        assert 'last_name' in result.columns
        assert 'already_snake' in result.columns


class TestValueToFloat:
    """Tests for value_to_float function"""
    
    @pytest.mark.parametrize("input_val,expected", [
        (100, 100.0),
        ("100", 100.0),
        ("1,000", 1000.0),
        ("1.5K", 1500.0),
        ("1.5M", 1500000.0),
        ("1B", 1000000000.0),
    ])
    def test_conversions(self, input_val, expected):
        """Test various value conversions"""
        assert value_to_float(input_val) == expected


class TestChunkList:
    """Tests for chunk_list function"""
    
    def test_even_chunks(self):
        """Test with even chunking"""
        result = list(chunk_list([1, 2, 3, 4, 5, 6], 2))
        assert result == [[1, 2], [3, 4], [5, 6]]
    
    def test_uneven_chunks(self):
        """Test with uneven chunking"""
        result = list(chunk_list([1, 2, 3, 4, 5], 2))
        assert result == [[1, 2], [3, 4], [5]]
    
    def test_empty_list(self):
        """Test with empty list"""
        result = list(chunk_list([], 2))
        assert result == []


class TestSafeGet:
    """Tests for safe_get function"""
    
    def test_nested_access(self):
        """Test nested dictionary access"""
        d = {'a': {'b': {'c': 1}}}
        assert safe_get(d, 'a', 'b', 'c') == 1
    
    def test_missing_key(self):
        """Test missing key"""
        d = {'a': {'b': 1}}
        assert safe_get(d, 'a', 'c', default='not found') == 'not found'
    
    def test_default_none(self):
        """Test default None"""
        d = {'a': 1}
        assert safe_get(d, 'b') is None


class TestBuildFullUrl:
    """Tests for build_full_url function"""
    
    def test_relative_path(self):
        """Test with relative path"""
        result = build_full_url("/Reports/test.aspx")
        assert result == "https://codal.ir/Reports/test.aspx"
    
    def test_full_url(self):
        """Test with full URL"""
        result = build_full_url("https://example.com/test")
        assert result == "https://example.com/test"
    
    def test_path_without_slash(self):
        """Test path without leading slash"""
        result = build_full_url("Reports/test.aspx")
        assert result == "https://codal.ir/Reports/test.aspx"
    
    def test_empty_path(self):
        """Test empty path"""
        assert build_full_url("") == ""

# ==================== Regression tests, offline correctness (P0/P1/P2) ====================

class TestDatetimeToNumValidation:
    """P0-6: a date is validated, so no month/day is ever invented."""

    def test_unpadded_date_is_padded_not_mangled(self):
        """'1402/5/15' used to produce month 51 (14025115000000)."""
        assert datetime_to_num("1402/5/15") == 14020515000000

    @pytest.mark.parametrize("bad", [
        "1402/51/15",          # month 51 - the reported defect
        "1402/13/01",          # month > 12
        "1402/00/10",          # month 0
        "1402/05/32",          # day out of range for any Jalali month
        "1402/12/30",          # 1402 is not a leap year
        "not a date",
        "1402/05/15 25:00:00",
    ])
    def test_impossible_values_are_rejected(self, bad):
        assert datetime_to_num(bad) is None

    def test_leap_year_last_day_is_accepted(self):
        assert datetime_to_num("1403/12/30") == 14031230000000


class TestYearMonthFromDate:
    """P0-6: the board-scraper year/month split cannot emit month > 12."""

    def test_padded_datetime(self):
        assert year_month_from_date("1402/05/15 10:30:00") == ("1402", "05")

    def test_unpadded_date(self):
        assert year_month_from_date("1402/5/15") == ("1402", "05")

    def test_malformed_date_yields_empty_parts(self):
        assert year_month_from_date("1402/51/15") == ("", "")
        assert year_month_from_date("1402/13/01") == ("", "")
        assert year_month_from_date("") == ("", "")
        assert year_month_from_date(None) == ("", "")


class TestLeapYearWindows:
    """P0-5: a leap Jalali year ends on 12/30, not 12/29."""

    def test_leap_year_range(self):
        assert calculate_date_range(1403) == ("1403/01/01", "1403/12/30")

    def test_non_leap_year_range(self):
        assert calculate_date_range(1402) == ("1402/01/01", "1402/12/29")

    def test_generate_year_ranges_handles_both(self):
        ranges = generate_year_ranges(1402, 1403)
        assert ranges[1402] == ("1402/01/01", "1402/12/29")
        assert ranges[1403] == ("1403/01/01", "1403/12/30")

    def test_prebuilt_table_ends_on_the_leap_day(self):
        assert YEAR_RANGES[1403][1] == "1403/12/30"


class TestZwnjHandling:
    """P0-8: the ZWNJ spelling must not lose the 'غیر موظف' match."""

    def test_zwnj_becomes_a_space(self):
        assert normalize_persian_text("غیر\u200cموظف") == "غیر موظف"

    def test_other_zero_width_characters_still_removed(self):
        assert normalize_persian_text("تست\u200fمتن") == "تستمتن"

    @pytest.mark.parametrize("duty", ["غیر موظف", "غیر\u200cموظف"])
    def test_independent_detected_for_both_spellings(self, duty):
        assert is_independent_duty(duty) is True

    @pytest.mark.parametrize("duty", ["موظف", "عضو موظف هیئت مدیره", "", None])
    def test_executive_is_not_independent(self, duty):
        assert is_independent_duty(duty) is False


class TestValueToFloatNaN:
    """P2: a failed conversion is missing data, not a zero."""

    @pytest.mark.parametrize("bad", ["invalid", "", "   ", "1.5X", None, []])
    def test_failures_are_nan(self, bad):
        assert math.isnan(value_to_float(bad))

    def test_zero_still_parses_as_zero(self):
        assert value_to_float("0") == 0.0

    def test_numeric_strings_still_parse(self):
        assert value_to_float("1,000") == 1000.0
