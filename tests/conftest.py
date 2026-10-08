"""
Pytest configuration and fixtures for Codal Scraper tests
"""

import json
import pytest
from pathlib import Path
from typing import Dict, List
from unittest.mock import Mock, MagicMock

from codal_scraper.types import BoardMemberData


# ============== Sample Data Fixtures ==============

@pytest.fixture
def sample_letter() -> Dict:
    """Single sample letter/announcement"""
    return {
        "Url": "/Reports/Decision.aspx?LetterSerial=123456",
        "TracingNo": "123456",
        "Symbol": "فولاد",
        "CompanyName": "فولاد مبارکه اصفهان",
        "Title": "معرفی/تغییر در ترکیب اعضای هیئت مدیره",
        "LetterCode": "ن-45",
        "SentDateTime": "1402/06/15 10:30:00",
        "PublishDateTime": "1402/06/15 10:35:00",
        "HasExcel": True,
        "HasPdf": True,
        "Audited": True,
        "Consolidated": False,
        "CompanyType": 1
    }


@pytest.fixture
def sample_letters(sample_letter) -> List[Dict]:
    """List of sample letters"""
    letters = [sample_letter.copy()]
    
    # Add more letters with variations
    letter2 = sample_letter.copy()
    letter2.update({
        "TracingNo": "123457",
        "Symbol": "خودرو",
        "CompanyName": "ایران خودرو",
        "PublishDateTime": "1402/06/16 11:00:00"
    })
    letters.append(letter2)
    
    letter3 = sample_letter.copy()
    letter3.update({
        "TracingNo": "123458",
        "Symbol": "شپنا",
        "CompanyName": "پالایش نفت اصفهان",
        "LetterCode": "ن-10",
        "PublishDateTime": "1402/06/17 09:00:00"
    })
    letters.append(letter3)
    
    return letters


@pytest.fixture
def sample_api_response(sample_letters) -> Dict:
    """Sample API response"""
    return {
        "Letters": sample_letters,
        "Total": 100,
        "Page": 10,
        "IsSuccess": True
    }


@pytest.fixture
def sample_board_member() -> BoardMemberData:
    return {
        'year': '1403',
        'date': '14030101',
        'month': '01',
        'assembly_date': '1403/01/01',
        'company': 'نماد: تست',
        'has_previous': False,
        'has_next': False,
        'url': 'https://codal.ir/test',

        'prev_member': 'عضو قدیم',
        'new_member': 'عضو جدید',
        'member_id': '1234567890',
        'prev_representative': 'نماینده قدیم',
        'new_representative': 'نماینده جدید',
        'national_id': '0012345678',
        'position': 'عضو هیئت مدیره',
        'is_independent': True,
        'degree': 'دکترا',
        'major': 'مدیریت',
        'experience': '۳ سال عضو هیئت مدیره شرکت X',

        'has_multiple_executive': False,
        'has_multiple_non_executive': False,
        'has_corporate_declaration': True,
        'has_position_acceptance': True,
        'verification_status': 'تایید صلاحیت شده',

        'ceo_name': 'مدیرعامل تستی',
        'ceo_national_id': '0011223344',
        'ceo_degree': 'کارشناسی ارشد',
        'ceo_major': 'حسابداری',
        'scrape_timestamp': '2024-01-01T00:00:00'
    }


@pytest.fixture
def sample_board_members(sample_board_member) -> List[Dict]:
    """List of sample board members"""
    members = [sample_board_member.copy()]
    
    member2 = sample_board_member.copy()
    member2.update({
        "new_member": "سارا رضایی",
        "national_id": "0011223344",
        "position": "نایب رئیس",
        "is_independent": False
    })
    members.append(member2)
    
    member3 = sample_board_member.copy()
    member3.update({
        "company": "خودرو",
        "new_member": "حسین کاظمی",
        "national_id": "0055667788",
        "position": "عضو هیئت مدیره"
    })
    members.append(member3)
    
    return members


# ============== Mock Fixtures ==============

@pytest.fixture
def mock_response():
    """Create a mock response object"""
    def _create_response(
        json_data: Dict = None,
        status_code: int = 200,
        content: bytes = None,
        headers: Dict = None
    ):
        response = Mock()
        response.status_code = status_code
        response.headers = headers or {'content-type': 'application/json'}
        response.json.return_value = json_data or {}
        response.text = json.dumps(json_data) if json_data else ""
        response.content = content or b""
        response.raise_for_status = Mock()
        
        if status_code >= 400:
            from requests.exceptions import HTTPError
            response.raise_for_status.side_effect = HTTPError(f"HTTP {status_code}")
        
        return response
    
    return _create_response


@pytest.fixture
def mock_session(mock_response, sample_api_response):
    """Create a mock requests session"""
    session = MagicMock()
    session.get.return_value = mock_response(json_data=sample_api_response)
    session.headers = {}
    return session


# ============== Temporary Files/Directories ==============

@pytest.fixture
def temp_dir(tmp_path) -> Path:
    """Temporary directory for test files"""
    return tmp_path


@pytest.fixture
def sample_csv(temp_dir, sample_letters) -> Path:
    """Create a sample CSV file with URLs"""
    import pandas as pd
    
    data = [
        {
            "url": f"https://codal.ir/Reports/Decision.aspx?LetterSerial={l['TracingNo']}",
            "symbol": l["Symbol"],
            "company_name": l["CompanyName"]
        }
        for l in sample_letters
    ]
    
    csv_path = temp_dir / "test_urls.csv"
    pd.DataFrame(data).to_csv(csv_path, index=False)
    
    return csv_path


@pytest.fixture
def sample_json(temp_dir, sample_api_response) -> Path:
    """Create a sample JSON file"""
    json_path = temp_dir / "test_data.json"
    
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(sample_api_response, f, ensure_ascii=False)
    
    return json_path


@pytest.fixture
def sample_excel(temp_dir, sample_letters) -> Path:
    """Create a sample Excel file"""
    import pandas as pd
    
    excel_path = temp_dir / "test_data.xlsx"
    pd.DataFrame(sample_letters).to_excel(excel_path, index=False)
    
    return excel_path


# ============== Configuration ==============

@pytest.fixture
def cache_config():
    """Cache configuration for tests"""
    from codal_scraper import CacheConfig
    return CacheConfig(
        cache_dir=".test_cache",
        default_ttl=60,
        enabled=True
    )


@pytest.fixture
def rate_limit_config():
    """Rate limit configuration for tests"""
    from codal_scraper import RateLimitConfig
    return RateLimitConfig(
        requests_per_second=10.0,  # Fast for tests
        burst_limit=20
    )


# ============== Cleanup ==============

@pytest.fixture(autouse=True)
def cleanup_test_cache():
    """Clean up test cache after each test"""
    yield
    
    # Cleanup
    import shutil
    cache_dir = Path(".test_cache")
    if cache_dir.exists():
        shutil.rmtree(cache_dir)


# ============== Markers ==============

def pytest_configure(config):
    """Configure custom markers"""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )
    config.addinivalue_line(
        "markers", "requires_network: marks tests that require network access"
    )