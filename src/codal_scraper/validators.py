"""
Input validation utilities for Codal Scraper

This module provides comprehensive validation for all input parameters
including dates, symbols, codes, and other user inputs.
"""

import re
from typing import Any, List, Optional, Union

from jdatetime import date as jdate

from .exceptions import ValidationError


class InputValidator:
    """
    Validates input parameters for Codal queries.
    
    Provides chainable validation methods for various input types
    commonly used with the Codal API.
    
    Example:
        >>> validator = InputValidator("1402/05/15")
        >>> validator.is_date()
        True
        
        >>> InputValidator("invalid").is_date()
        ValidationError: Expected date in format yyyy/mm/dd
    """
    
    PATTERNS = {
        'date': r'^[0-9]{4}/(0?[1-9]|1[012])/(0?[1-9]|[12][0-9]|3[01])$',
        'integer_string': r'^[0-9]+$',
        'symbol': r'^[\w\u0600-\u06FF]+$',  # Persian and ASCII alphanumeric
        'isic': r'^[0-9]{4,6}$',
        'tracing_no': r'^[0-9]+$',
        'letter_code': r'^[\u0600-\u06FF]-[0-9]+$',  # e.g., ن-45
    }
    
    def __init__(self, value: Any):
        """
        Initialize validator with a value to validate.
        
        Args:
            value: The value to validate
        """
        self.value = value
    
    def is_not_none(self) -> bool:
        """Validate that value is not None"""
        if self.value is None:
            raise ValidationError("Value cannot be None")
        return True
    
    def is_not_empty(self) -> bool:
        """Validate that value is not empty"""
        if self.value is None:
            raise ValidationError("Value cannot be None")
        if isinstance(self.value, str) and not self.value.strip():
            raise ValidationError("Value cannot be empty string")
        if isinstance(self.value, (list, dict)) and len(self.value) == 0:
            raise ValidationError("Value cannot be empty collection")
        return True
    
    def is_boolean(self) -> bool:
        """Validate if value is boolean"""
        if not isinstance(self.value, bool):
            raise ValidationError(
                f"Expected boolean, got {type(self.value).__name__}: {self.value}",
                field="boolean",
                value=self.value
            )
        return True
    
    def is_integer(self) -> bool:
        """Validate if value is integer"""
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise ValidationError(
                f"Expected integer, got {type(self.value).__name__}: {self.value}",
                field="integer",
                value=self.value
            )
        return True
    
    def is_string(self) -> bool:
        """Validate if value is string"""
        if not isinstance(self.value, str):
            raise ValidationError(
                f"Expected string, got {type(self.value).__name__}: {self.value}",
                field="string",
                value=self.value
            )
        return True
    
    def is_integer_string(self) -> bool:
        """Validate if value is a string containing only digits"""
        self.is_string()
        if not re.match(self.PATTERNS['integer_string'], str(self.value)):
            raise ValidationError(
                f"Expected integer string, got: {self.value}",
                field="integer_string",
                value=self.value
            )
        return True
    
    def is_date(self, format_str: str = "yyyy/mm/dd") -> bool:
        """
        Validate if value is a valid date in Persian calendar format.
        
        Validates:
        - Format matches YYYY/MM/DD
        - Year is reasonable (1300-1500)
        - Month is 1-12
        - Day is valid for the specific month in Persian calendar
        
        Args:
            format_str: Expected format description for error messages
            
        Returns:
            True if valid
            
        Raises:
            ValidationError: If validation fails
        """
        self.is_string()
        
        value_str = str(self.value)
        
        if not re.match(self.PATTERNS['date'], value_str):
            raise ValidationError(
                f"Expected date in format {format_str}, got: {self.value}",
                field="date",
                value=self.value
            )
        
        parts = value_str.split('/')
        year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])
        
        # Year validation (Persian calendar years)
        if not (1300 <= year <= 1500):
            raise ValidationError(
                f"Year must be between 1300 and 1500, got: {year}",
                field="year",
                value=year
            )
        
        # Month validation
        if not (1 <= month <= 12):
            raise ValidationError(
                f"Month must be between 1 and 12, got: {month}",
                field="month",
                value=month
            )
        
        # Day validation based on Persian calendar rules
        max_day = self._get_max_day_for_month(year, month)
        
        if not (1 <= day <= max_day):
            raise ValidationError(
                f"Day must be between 1 and {max_day} for month {month}, got: {day}",
                field="day",
                value=day
            )
        
        # Final validation using jdatetime
        try:
            jdate(year, month, day)
        except ValueError as e:
            raise ValidationError(
                f"Invalid Persian date: {self.value} - {e}",
                field="date",
                value=self.value
            )
        
        return True
    
    def _get_max_day_for_month(self, year: int, month: int) -> int:
        """
        Get maximum valid day for a given month in Persian calendar.
        
        Persian calendar rules:
        - Months 1-6 (Farvardin to Shahrivar): 31 days
        - Months 7-11 (Mehr to Bahman): 30 days
        - Month 12 (Esfand): 29 days (30 in leap years)
        """
        if month <= 6:
            return 31
        elif month <= 11:
            return 30
        else:  # month 12
            return 30 if self._is_persian_leap_year(year) else 29
    
    @staticmethod
    def _is_persian_leap_year(year: int) -> bool:
        """
        Check if a Persian year is a leap year.
        
        Uses the 2820-year cycle algorithm.
        """
        # Simplified leap year calculation
        # More accurate: based on astronomical calculations
        a = 0.025
        b = 266
        leap_threshold = 0.5
        
        frac = ((year - 474) % 2820) * 0.24219 + a
        return (frac - int(frac)) < leap_threshold
    
    def is_symbol(self) -> bool:
        """
        Validate if value is a valid stock symbol.
        
        Valid symbols can contain:
        - Persian letters (آ-ی)
        - English letters (A-Z, a-z)
        - Digits (0-9)
        """
        self.is_string()
        
        value_str = str(self.value).strip()
        
        if not value_str:
            raise ValidationError(
                "Symbol cannot be empty",
                field="symbol",
                value=self.value
            )
        
        if not re.match(self.PATTERNS['symbol'], value_str):
            raise ValidationError(
                f"Invalid symbol format: {self.value}",
                field="symbol",
                value=self.value
            )
        
        return True
    
    def is_isic(self) -> bool:
        """Validate if value is a valid ISIC code (4-6 digits)"""
        self.is_string()
        
        if not re.match(self.PATTERNS['isic'], str(self.value)):
            raise ValidationError(
                f"Invalid ISIC code format (expected 4-6 digits): {self.value}",
                field="isic",
                value=self.value
            )
        
        return True
    
    def is_letter_code(self) -> bool:
        """Validate if value is a valid letter code (e.g., ن-45)"""
        self.is_string()
        
        if not re.match(self.PATTERNS['letter_code'], str(self.value)):
            raise ValidationError(
                f"Invalid letter code format (expected format like 'ن-45'): {self.value}",
                field="letter_code",
                value=self.value
            )
        
        return True
    
    def is_url(self) -> bool:
        """Validate if value is a valid URL"""
        self.is_string()
        
        url_pattern = r'^https?://[^\s<>"{}|\\^`\[\]]+'
        
        if not re.match(url_pattern, str(self.value)):
            raise ValidationError(
                f"Invalid URL format: {self.value}",
                field="url",
                value=self.value
            )
        
        return True
    
    def in_range(
        self, 
        min_val: Union[int, float], 
        max_val: Union[int, float]
    ) -> bool:
        """
        Validate if numeric value is within range (inclusive).
        
        Args:
            min_val: Minimum allowed value
            max_val: Maximum allowed value
        """
        if not isinstance(self.value, (int, float)):
            raise ValidationError(
                f"Expected numeric value, got {type(self.value).__name__}",
                field="range",
                value=self.value
            )
        
        if not (min_val <= self.value <= max_val):
            raise ValidationError(
                f"Value {self.value} is not in range [{min_val}, {max_val}]",
                field="range",
                value=self.value
            )
        
        return True
    
    def in_list(self, valid_values: List) -> bool:
        """
        Validate if value is in a list of valid values.
        
        Args:
            valid_values: List of acceptable values
        """
        if self.value not in valid_values:
            # Truncate list for error message if too long
            display_values = valid_values[:10]
            suffix = f"... ({len(valid_values)} total)" if len(valid_values) > 10 else ""
            
            raise ValidationError(
                f"Value '{self.value}' is not in valid values: {display_values}{suffix}",
                field="list",
                value=self.value
            )
        
        return True
    
    def matches_pattern(self, pattern: str, description: str = "pattern") -> bool:
        """
        Validate if value matches a regex pattern.
        
        Args:
            pattern: Regular expression pattern
            description: Human-readable description of expected format
        """
        self.is_string()
        
        if not re.match(pattern, str(self.value)):
            raise ValidationError(
                f"Value does not match expected {description}: {self.value}",
                field=description,
                value=self.value
            )
        
        return True
    
    def has_min_length(self, min_length: int) -> bool:
        """Validate minimum length for strings or collections"""
        if isinstance(self.value, str):
            if len(self.value) < min_length:
                raise ValidationError(
                    f"String must have at least {min_length} characters, got {len(self.value)}",
                    field="min_length",
                    value=self.value
                )
        elif hasattr(self.value, '__len__'):
            if len(self.value) < min_length:
                raise ValidationError(
                    f"Collection must have at least {min_length} items, got {len(self.value)}",
                    field="min_length",
                    value=len(self.value)
                )
        else:
            raise ValidationError(
                f"Cannot check length of {type(self.value).__name__}",
                field="min_length",
                value=self.value
            )
        
        return True
    
    def has_max_length(self, max_length: int) -> bool:
        """Validate maximum length for strings or collections"""
        if hasattr(self.value, '__len__'):
            if len(self.value) > max_length:
                raise ValidationError(
                    f"Length must not exceed {max_length}, got {len(self.value)}",
                    field="max_length",
                    value=len(self.value)
                )
        else:
            raise ValidationError(
                f"Cannot check length of {type(self.value).__name__}",
                field="max_length",
                value=self.value
            )
        
        return True


def validate_input(
    value: Any,
    validation_type: str,
    **kwargs
) -> bool:
    """
    Convenience function for input validation.
    
    Args:
        value: The value to validate
        validation_type: Type of validation to perform
        **kwargs: Additional arguments for specific validation types
    
    Returns:
        True if valid
    
    Raises:
        ValidationError: If validation fails
        ValueError: If unknown validation type
        
    Example:
        >>> validate_input("1402/05/15", "date")
        True
        
        >>> validate_input("فولاد", "symbol")
        True
        
        >>> validate_input(50, "range", min_val=0, max_val=100)
        True
    """
    validator = InputValidator(value)
    
    validation_methods = {
        'boolean': validator.is_boolean,
        'integer': validator.is_integer,
        'string': validator.is_string,
        'integer_string': validator.is_integer_string,
        'date': validator.is_date,
        'symbol': validator.is_symbol,
        'isic': validator.is_isic,
        'letter_code': validator.is_letter_code,
        'url': validator.is_url,
        'not_none': validator.is_not_none,
        'not_empty': validator.is_not_empty,
        'range': lambda: validator.in_range(
            kwargs.get('min_val', float('-inf')),
            kwargs.get('max_val', float('inf'))
        ),
        'list': lambda: validator.in_list(kwargs.get('valid_values', [])),
        'pattern': lambda: validator.matches_pattern(
            kwargs.get('pattern', '.*'),
            kwargs.get('description', 'pattern')
        ),
        'min_length': lambda: validator.has_min_length(kwargs.get('min_length', 0)),
        'max_length': lambda: validator.has_max_length(kwargs.get('max_length', float('inf'))),
    }
    
    if validation_type not in validation_methods:
        raise ValueError(f"Unknown validation type: {validation_type}")
    
    return validation_methods[validation_type]()


def parse_jalali_date(value: str) -> jdate:
    """
    Parse a Jalali date string into a ``jdatetime.date``.

    The string is validated first, so a malformed date raises
    ``ValidationError`` (not a bare ``ValueError``).  Unpadded components are
    accepted ("1402/5/15"), because ``InputValidator.is_date`` accepts them.

    Args:
        value: Date string in YYYY/MM/DD format

    Returns:
        jdatetime.date

    Raises:
        ValidationError: If the value is not a valid Jalali date
    """
    InputValidator(value).is_date()

    year, month, day = (int(part) for part in str(value).split('/'))

    return jdate(year, month, day)


def validate_date_range(from_date: str, to_date: str) -> bool:
    """
    Validate a date range.
    
    Args:
        from_date: Start date
        to_date: End date
        
    Returns:
        True if valid
        
    Raises:
        ValidationError: If dates are invalid or from_date > to_date
    """
    parsed_from = parse_jalali_date(from_date)
    parsed_to = parse_jalali_date(to_date)
    
    # Compare the parsed dates: as strings, "1402/5/15" > "1402/10/15" even
    # though the first date is the earlier one.
    if parsed_from > parsed_to:
        raise ValidationError(
            f"from_date ({from_date}) must be before or equal to to_date ({to_date})",
            field="date_range"
        )
    
    return True


# Backward compatibility
class BadValueInput:
    """Legacy validator class for backward compatibility"""
    
    def __init__(self, value):
        self.value = value
        self.validator = InputValidator(value)
    
    def boolean_type(self):
        return self.validator.is_boolean()
    
    def integer_type(self):
        return self.validator.is_integer()
    
    def string_type(self):
        return self.validator.is_string()
    
    def int_str_type(self):
        return self.validator.is_integer_string()
    
    def date_type(self):
        return self.validator.is_date()