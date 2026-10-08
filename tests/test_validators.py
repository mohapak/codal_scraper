"""
Tests for validators module
"""

import pytest
from jdatetime import date as jdate

from codal_scraper.validators import (
    InputValidator,
    parse_jalali_date,
    validate_date_range,
    validate_input,
    ValidationError,
)
from codal_scraper.types import DateRange


class TestInputValidator:
    """Tests for InputValidator class"""
    
    def test_is_string_valid(self):
        """Test valid string"""
        assert InputValidator("hello").is_string() is True
    
    def test_is_string_invalid(self):
        """Test invalid string"""
        with pytest.raises(ValidationError):
            InputValidator(123).is_string()
    
    def test_is_integer_valid(self):
        """Test valid integer"""
        assert InputValidator(42).is_integer() is True
    
    def test_is_integer_invalid(self):
        """Test invalid integer"""
        with pytest.raises(ValidationError):
            InputValidator("42").is_integer()
    
    def test_is_integer_bool(self):
        """Test that bool is not accepted as integer"""
        with pytest.raises(ValidationError):
            InputValidator(True).is_integer()
    
    def test_is_boolean_valid(self):
        """Test valid boolean"""
        assert InputValidator(True).is_boolean() is True
        assert InputValidator(False).is_boolean() is True
    
    def test_is_boolean_invalid(self):
        """Test invalid boolean"""
        with pytest.raises(ValidationError):
            InputValidator(1).is_boolean()


class TestDateValidation:
    """Tests for date validation"""
    
    @pytest.mark.parametrize("valid_date", [
        "1402/01/01",
        "1402/06/31",
        "1402/12/29",
        "1400/07/15",
        "1403/1/1",  # Without leading zeros
    ])
    def test_valid_dates(self, valid_date):
        """Test valid Persian dates"""
        assert InputValidator(valid_date).is_date() is True
    
    @pytest.mark.parametrize("invalid_date,reason", [
        ("2024/01/01", "Gregorian year"),
        ("1402/13/01", "Invalid month"),
        ("1402/00/01", "Zero month"),
        ("1402/01/32", "Day too high"),
        ("1402-01-01", "Wrong separator"),
        ("14020101", "No separator"),
        ("1402/01", "Missing day"),
        ("abc", "Not a date"),
    ])
    def test_invalid_dates(self, invalid_date, reason):
        """Test invalid dates"""
        with pytest.raises(ValidationError):
            InputValidator(invalid_date).is_date()
    
    def test_month_day_limits(self):
        """Test month-specific day limits"""
        # Month 1-6 can have 31 days
        assert InputValidator("1402/06/31").is_date() is True
        
        # Month 7-11 max 30 days
        with pytest.raises(ValidationError):
            InputValidator("1402/07/31").is_date()
        
        # Month 12 max 29 days (non-leap year)
        # This depends on leap year calculation


class TestSymbolValidation:
    """Tests for symbol validation"""
    
    @pytest.mark.parametrize("valid_symbol", [
        "فولاد",
        "خودرو",
        "FOLD",  # English
        "فولاد1",  # Mixed
        "ABC123",
    ])
    def test_valid_symbols(self, valid_symbol):
        """Test valid symbols"""
        assert InputValidator(valid_symbol).is_symbol() is True
    
    @pytest.mark.parametrize("invalid_symbol", [
        "",  # Empty
        "  ",  # Whitespace only
        "فولاد@",  # Special char
        "test!",  # Special char
    ])
    def test_invalid_symbols(self, invalid_symbol):
        """Test invalid symbols"""
        with pytest.raises(ValidationError):
            InputValidator(invalid_symbol).is_symbol()


class TestISICValidation:
    """Tests for ISIC code validation"""
    
    @pytest.mark.parametrize("valid_isic", [
        "1234",
        "12345",
        "123456",
    ])
    def test_valid_isic(self, valid_isic):
        """Test valid ISIC codes"""
        assert InputValidator(valid_isic).is_isic() is True
    
    @pytest.mark.parametrize("invalid_isic", [
        "123",  # Too short
        "1234567",  # Too long
        "abcd",  # Not numeric
        "12ab",  # Mixed
    ])
    def test_invalid_isic(self, invalid_isic):
        """Test invalid ISIC codes"""
        with pytest.raises(ValidationError):
            InputValidator(invalid_isic).is_isic()


class TestRangeValidation:
    """Tests for range validation"""
    
    def test_in_range_valid(self):
        """Test value in range"""
        assert InputValidator(50).in_range(0, 100) is True
        assert InputValidator(0).in_range(0, 100) is True
        assert InputValidator(100).in_range(0, 100) is True
    
    def test_in_range_invalid(self):
        """Test value out of range"""
        with pytest.raises(ValidationError):
            InputValidator(101).in_range(0, 100)
        
        with pytest.raises(ValidationError):
            InputValidator(-1).in_range(0, 100)


class TestListValidation:
    """Tests for list membership validation"""
    
    def test_in_list_valid(self):
        """Test value in list"""
        assert InputValidator("a").in_list(["a", "b", "c"]) is True
    
    def test_in_list_invalid(self):
        """Test value not in list"""
        with pytest.raises(ValidationError):
            InputValidator("d").in_list(["a", "b", "c"])


class TestValidateInputFunction:
    """Tests for validate_input convenience function"""
    
    def test_validate_date(self):
        """Test date validation"""
        assert validate_input("1402/06/15", "date") is True
    
    def test_validate_symbol(self):
        """Test symbol validation"""
        assert validate_input("فولاد", "symbol") is True
    
    def test_validate_range(self):
        """Test range validation"""
        assert validate_input(50, "range", min_val=0, max_val=100) is True
    
    def test_validate_list(self):
        """Test list validation"""
        assert validate_input("ن-45", "list", valid_values=["ن-45", "ن-10"]) is True
    
    def test_unknown_validation_type(self):
        """Test unknown validation type"""
        with pytest.raises(ValueError):
            validate_input("test", "unknown_type")

# ==================== Regression tests, P1-9 ====================

class TestValidateDateRangeParsesDates:
    """P1-9: ranges are compared as parsed Jalali dates, not as strings."""

    def test_unpadded_range_is_accepted(self):
        """'1402/5/15' > '1402/10/15' as strings, but not as dates."""
        assert validate_date_range("1402/5/15", "1402/10/15") is True

    def test_reversed_unpadded_range_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_date_range("1402/10/15", "1402/5/15")

    def test_equal_dates_are_allowed(self):
        assert validate_date_range("1402/5/15", "1402/5/15") is True

    def test_range_across_a_year_boundary(self):
        assert validate_date_range("1402/12/29", "1403/01/05") is True

    def test_parse_jalali_date_pads_and_validates(self):
        assert parse_jalali_date("1402/5/15") == jdate(1402, 5, 15)
        with pytest.raises(ValidationError):
            parse_jalali_date("1402/51/15")


class TestDateRangeType:
    """P1-9: DateRange raises ValidationError, not a bare ValueError."""

    def test_reversed_range_raises_validation_error(self):
        with pytest.raises(ValidationError):
            DateRange("1402/10/15", "1402/5/15")

    def test_valid_unpadded_range_is_kept_verbatim(self):
        date_range = DateRange("1402/5/15", "1402/10/15")
        assert date_range.to_dict() == {"FromDate": "1402/5/15", "ToDate": "1402/10/15"}
