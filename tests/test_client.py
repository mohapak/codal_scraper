"""
Tests for CodalClient
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

from codal_scraper import CodalClient
from codal_scraper.cache import CacheConfig
from codal_scraper.exceptions import ValidationError, APIError


class TestCodalClientInit:
    """Tests for CodalClient initialization"""
    
    def test_default_init(self):
        """Test default initialization"""
        client = CodalClient()
        
        assert client.retry_count == 3
        assert client.timeout == 30
        assert client.params['PageNumber'] == 1
        assert client.params['Symbol'] == -1
    
    def test_custom_init(self):
        """Test initialization with custom parameters"""
        client = CodalClient(retry_count=5, timeout=60)
        
        assert client.retry_count == 5
        assert client.timeout == 60
    
    def test_context_manager(self):
        """Test context manager protocol"""
        with CodalClient() as client:
            assert client is not None
            assert client._session is None  # Lazy initialization


class TestCodalClientParameters:
    """Tests for parameter setting methods"""
    
    @pytest.fixture
    def client(self):
        return CodalClient()
    
    def test_set_symbol(self, client):
        """Test setting symbol"""
        result = client.set_symbol("فولاد")
        
        assert result is client  # Method chaining
        assert client.params['Symbol'] == "فولاد"
    
    def test_set_symbol_with_cleanup(self, client):
        """Test symbol cleanup"""
        client.set_symbol("  فولاد۱  ")
        
        # Should be cleaned (Persian digits converted, stripped)
        assert client.params['Symbol'] == "فولاد1"
    
    def test_set_symbol_none(self, client):
        """Test setting symbol to None"""
        client.set_symbol("فولاد")
        client.set_symbol(None)
        
        assert client.params['Symbol'] == -1
    
    def test_set_date_range(self, client):
        """Test setting date range"""
        client.set_date_range("1402/01/01", "1402/12/29")
        
        assert client.params['FromDate'] == "1402/01/01"
        assert client.params['ToDate'] == "1402/12/29"
    
    def test_set_date_range_invalid(self, client):
        """Test invalid date format"""
        with pytest.raises(ValidationError):
            client.set_date_range("2024/01/01", "2024/12/29")  # Gregorian year
    
    def test_set_date_range_invalid_order(self, client):
        """Test from_date > to_date"""
        with pytest.raises(ValidationError):
            client.set_date_range("1402/12/29", "1402/01/01")
    
    def test_set_letter_code(self, client):
        """Test setting letter code"""
        client.set_letter_code("ن-45")
        
        assert client.params['LetterCode'] == "ن-45"
    
    def test_set_company_type(self, client):
        """Test setting company type"""
        client.set_company_type("بورس")
        
        assert client.params['CompanyType'] == "1"
    
    def test_set_period_length(self, client):
        """Test setting period length"""
        client.set_period_length(12)
        
        assert client.params['Length'] == 12
    
    def test_set_audit_status(self, client):
        """Test setting audit status"""
        client.set_audit_status(audited=True, not_audited=False)
        
        assert client.params['Audited'] == "true"
        assert client.params['NotAudited'] == "false"
    
    def test_method_chaining(self, client):
        """Test method chaining"""
        result = (client
            .set_symbol("فولاد")
            .set_date_range("1402/01/01", "1402/06/31")
            .set_letter_code("ن-45")
            .set_audit_status(audited=True, not_audited=True))
        
        assert result is client
        assert client.params['Symbol'] == "فولاد"
        assert client.params['LetterCode'] == "ن-45"
    
    def test_reset_params(self, client):
        """Test resetting parameters"""
        client.set_symbol("فولاد")
        client.set_letter_code("ن-45")
        
        client.reset_params()
        
        assert client.params['Symbol'] == -1
        assert client.params['LetterCode'] == -1


class TestCodalClientURLGeneration:
    """Tests for URL generation"""
    
    @pytest.fixture
    def client(self):
        return CodalClient()
    
    def test_get_query_url_api(self, client):
        """Test API URL generation"""
        client.set_letter_code("ن-45")
        url = client.get_query_url(use_api=True)
        
        assert "search.codal.ir" in url
        assert "LetterCode=" in url
        assert "search=true" in url
    
    def test_get_query_url_web(self, client):
        """Test web URL generation"""
        client.set_letter_code("ن-45")
        url = client.get_query_url(use_api=False)
        
        assert "codal.ir/ReportList.aspx" in url
        assert "search&" in url


class TestCodalClientFetching:
    """Tests for data fetching methods"""

    @pytest.fixture
    def client(self, mock_session, sample_api_response, tmp_path):
        # Isolate the on-disk response cache per test. With the default
        # `.codal_cache`, a response cached by an earlier test in the same
        # session is served here before the mocked session is ever consulted,
        # so this class's tests passed or failed depending on test order.
        client = CodalClient(
            cache_config=CacheConfig(cache_dir=str(tmp_path / "codal_cache"))
        )
        client._session = mock_session
        return client
    
    def test_fetch_page_success(self, client, sample_api_response):
        """Test successful page fetch"""
        letters = client.fetch_page(1)
        
        assert letters is not None
        assert len(letters) == len(sample_api_response['Letters'])
        assert client.total_results == sample_api_response['Total']
        # P0-3: total_pages must NOT be read from the response's `Page`
        # field: `Page` may be the current page index, in which case page 1
        # reports a page count of 1 and every query silently looks like a
        # single page. It is derived from `Total` and the page size the API
        # actually returned instead (ceil(100 / 3) == 34 here, not Page=10).
        assert client.total_pages == -(-sample_api_response['Total']
                                       // len(sample_api_response['Letters']))
        assert client.total_pages != sample_api_response['Page']
    
    def test_fetch_page_failure(self, client, mock_response):
        """Test failed page fetch"""
        client._session.get.return_value = mock_response(
            json_data=None,
            status_code=500
        )
        
        # Should not raise, just return None
        letters = client.fetch_page(1)
        assert letters is None
    
    def test_extract_letter_urls(self, client, sample_letters):
        """Test URL extraction from letters"""
        urls = client.extract_letter_urls(sample_letters)
        
        assert len(urls) == len(sample_letters)
        assert all(url.startswith("https://codal.ir") for url in urls)


class TestCodalClientConvenienceMethods:
    """Tests for convenience search methods"""
    
    @pytest.fixture
    def client(self, mock_session):
        client = CodalClient()
        client._session = mock_session
        return client
    
    def test_search_board_changes_params(self, client):
        """Test that search_board_changes sets correct params"""
        # Don't actually fetch, just check params
        client.reset_params()
        client.set_letter_code("ن-45")
        client.set_date_range("1402/01/01", "1402/12/29")
        client.set_entity_type(include_childs=False, include_mains=True)
        
        assert client.params['LetterCode'] == "ن-45"
        assert client.params['Childs'] == "false"
        assert client.params['Mains'] == "true"
    
    def test_search_financial_statements_params(self, client):
        """Test that search_financial_statements sets correct params"""
        client.reset_params()
        client.set_letter_code("ن-10")
        client.set_period_length(12)
        client.set_audit_status(audited=True, not_audited=False)
        
        assert client.params['LetterCode'] == "ن-10"
        assert client.params['Length'] == 12
        assert client.params['Audited'] == "true"
        assert client.params['NotAudited'] == "false"


class TestCodalClientStats:
    """Tests for statistics tracking"""
    
    def test_get_stats(self):
        """Test getting client statistics"""
        client = CodalClient()
        stats = client.get_stats()
        
        assert 'requests_made' in stats
        assert 'cache_hits' in stats
        assert 'errors' in stats
    
    def test_reset_stats(self):
        """Test resetting statistics"""
        client = CodalClient()
        client._stats['requests_made'] = 100
        
        client.reset_stats()
        
        assert client._stats['requests_made'] == 0