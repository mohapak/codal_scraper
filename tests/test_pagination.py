"""
P0-3 regression tests: pagination must be correct under *either* meaning of the
API's ambiguous ``Page`` field, and must never silently truncate.

``search.codal.ir`` is unreachable from the test machine, so the response shape
is simulated rather than assumed: every test here drives a fake session that
serves a different payload per ``PageNumber``, and the two candidate meanings of
``Page`` are exercised against the same data.

Scenario (i)   ``Page`` = current page index, ``Total`` = 100, 10 items/page
Scenario (ii)  ``Page`` = total page count,    same data
Scenario (iii) ``Total`` claims more than the pages can supply -> raises
Scenario (iv)  a response implying a runaway page count -> trips the cap
Scenario (v)   single-page and normal multi-page regression cases
"""

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import pytest
from requests.exceptions import HTTPError

from codal_scraper.cache import CacheConfig
from codal_scraper.client import CodalClient
from codal_scraper.async_client import AsyncCodalClient
from codal_scraper.exceptions import (
    IncompleteResultsError,
    PaginationCapExceededError,
    ValidationError,
)
from codal_scraper.pagination import (
    MAX_PAGES_ABSOLUTE,
    PAGE_CAP_SLACK,
    PAGE_SEMANTICS_COUNT,
    PAGE_SEMANTICS_INDEX,
    estimate_page_count,
    page_cap,
)

TOTAL = 100
PAGE_SIZE = 10
EXPECTED_PAGES = 10


# ============== Fake transports (no network) ==============

def _page_number_from_url(url: str) -> int:
    """Pull PageNumber out of the query URL the client actually built."""
    query = parse_qs(urlparse(url).query)
    return int(query["PageNumber"][0])


def _letters(start: int, count: int):
    """`count` letters whose TracingNo values identify exactly which page they
    came from -- so a truncated or duplicated harvest is visible in the data."""
    return [
        {
            "Url": f"/Reports/Decision.aspx?LetterSerial={start + i}",
            "TracingNo": str(start + i),
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
            "CompanyType": 1,
        }
        for i in range(count)
    ]


def paged_responder(total, page_size, page_field, pages_available=None):
    """
    Build a responder that serves `total` items, `page_size` per page.

    Args:
        total: value to report in the response's `Total`
        page_size: items served per page
        page_field: callable(page) -> value of the response's `Page`
        pages_available: if set, pages beyond this index return nothing
    """
    def respond(page: int):
        if pages_available is not None and page > pages_available:
            return {
                "Letters": [], "Total": total,
                "Page": page_field(page), "IsSuccess": True,
            }
        start = (page - 1) * page_size + 1
        return {
            "Letters": _letters(start, page_size),
            "Total": total,
            "Page": page_field(page),
            "IsSuccess": True,
        }
    return respond


def sized_responder(total, page_sizes, page_field):
    """Serve an explicit number of items per page, then empty pages."""
    def respond(page: int):
        if page > len(page_sizes):
            return {
                "Letters": [], "Total": total,
                "Page": page_field(page), "IsSuccess": True,
            }
        start = sum(page_sizes[:page - 1]) + 1
        return {
            "Letters": _letters(start, page_sizes[page - 1]),
            "Total": total,
            "Page": page_field(page),
            "IsSuccess": True,
        }
    return respond


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200 if payload is not None else 500
        self.headers = {"content-type": "application/json"}
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""
        self.content = self.text.encode()

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HTTPError(f"HTTP {self.status_code}")


class _FakeSession:
    """Stands in for requests.Session; records every page requested."""

    def __init__(self, responder):
        self._responder = responder
        self.calls = []
        self.headers = {}

    def get(self, url, timeout=None):
        page = _page_number_from_url(url)
        self.calls.append(page)
        return _FakeResponse(self._responder(page))


class _FakeAsyncResponse:
    def __init__(self, payload):
        self.status = 200 if payload is not None else 500
        self.headers = {"content-type": "application/json"}
        self._payload = payload

    async def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status >= 400:  # pragma: no cover - not exercised here
            raise HTTPError(f"HTTP {self.status}")


class _FakeAsyncContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class _FakeAsyncSession:
    def __init__(self, responder):
        self._responder = responder
        self.calls = []

    def get(self, url):
        page = _page_number_from_url(url)
        self.calls.append(page)
        return _FakeAsyncContext(_FakeAsyncResponse(self._responder(page)))

    async def close(self):  # pragma: no cover - nothing to close
        pass


class _NoopRateLimiter:
    async def acquire(self):
        return None


# ============== Fixtures ==============

@pytest.fixture
def make_client():
    """A CodalClient wired to a fake session serving per-page payloads.

    Caching is off so that a second client in the same test is not served its
    pages from the first client's cache -- otherwise a "how many requests did
    the client make?" assertion would measure the cache, not the pagination.
    """
    def _make(responder):
        client = CodalClient(
            enable_rate_limit=False,
            enable_cache=False,
        )
        session = _FakeSession(responder)
        client._session = session
        return client, session
    return _make


@pytest.fixture
def make_async_client(tmp_path):
    def _make(responder):
        client = AsyncCodalClient(
            enable_cache=False,
            cache_config=CacheConfig(cache_dir=str(tmp_path / "async_cache")),
        )
        session = _FakeAsyncSession(responder)
        client._session = session
        client.rate_limiter = _NoopRateLimiter()
        return client, session
    return _make


def _tracing_numbers(letters):
    return [letter["TracingNo"] for letter in letters]


# ============== The mock really does serve different pages ==============

class TestFakeTransport:
    def test_each_page_serves_a_different_payload(self, make_client):
        """Guard the guard: if the fake served page 1 forever, every test below
        would pass for the wrong reason."""
        responder = paged_responder(TOTAL, PAGE_SIZE, lambda p: p)
        client, session = make_client(responder)

        client.fetch_page(1)
        client.fetch_page(2)
        client.fetch_page(10)

        assert session.calls == [1, 2, 10]
        # Page 1 -> TracingNo 1..10, page 2 -> 11..20, page 10 -> 91..100 and
        # all three are disjoint, so a truncated harvest cannot look complete.
        first = _tracing_numbers(responder(1)["Letters"])
        second = _tracing_numbers(responder(2)["Letters"])
        tenth = _tracing_numbers(responder(10)["Letters"])
        assert first == [str(i) for i in range(1, 11)]
        assert second == [str(i) for i in range(11, 21)]
        assert tenth == [str(i) for i in range(91, 101)]
        assert not set(first) & set(second) & set(tenth)

    def test_client_actually_requests_the_page_it_is_told_to(self, make_client):
        responder = paged_responder(TOTAL, PAGE_SIZE, lambda p: p)
        client, session = make_client(responder)
        letters = client.fetch_page(4)
        assert session.calls == [4]
        assert _tracing_numbers(letters) == [str(i) for i in range(31, 41)]


# ============== (i) and (ii): both `Page` semantics collect everything ======

class TestBothPageSemantics:
    def test_page_is_current_index_collects_every_item(self, make_client):
        """P0-3 as-shipped reading: `Page` is the current page index. Under the
        old code page 1 reported Page=1, so `pages_to_fetch` became 1 and only
        the first page ever came back."""
        responder = paged_responder(TOTAL, PAGE_SIZE, lambda p: p)
        client, session = make_client(responder)

        letters = client.fetch_all_pages()

        assert len(letters) == TOTAL
        assert len(set(_tracing_numbers(letters))) == TOTAL
        assert session.calls == list(range(1, EXPECTED_PAGES + 1))
        assert letters.expected_total == TOTAL
        assert letters.arrived == TOTAL
        assert letters.complete is True
        assert letters.pages_fetched == EXPECTED_PAGES
        assert letters.page_field_mode == PAGE_SEMANTICS_INDEX

    def test_page_is_total_page_count_collects_every_item(self, make_client):
        """The other candidate reading: `Page` is the page count. Same data,
        same number of pages, same items."""
        responder = paged_responder(TOTAL, PAGE_SIZE, lambda p: EXPECTED_PAGES)
        client, session = make_client(responder)

        letters = client.fetch_all_pages()

        assert len(letters) == TOTAL
        assert len(set(_tracing_numbers(letters))) == TOTAL
        assert session.calls == list(range(1, EXPECTED_PAGES + 1))
        assert letters.expected_total == TOTAL
        assert letters.complete is True
        assert letters.pages_fetched == EXPECTED_PAGES
        assert letters.page_field_mode == PAGE_SEMANTICS_COUNT

    def test_both_semantics_yield_identical_data(self, make_client):
        index_client, index_session = make_client(
            paged_responder(TOTAL, PAGE_SIZE, lambda p: p)
        )
        count_client, count_session = make_client(
            paged_responder(TOTAL, PAGE_SIZE, lambda p: EXPECTED_PAGES)
        )

        from_index = index_client.fetch_all_pages()
        from_count = count_client.fetch_all_pages()

        assert list(from_index) == list(from_count)
        assert index_session.calls == count_session.calls

    def test_async_client_matches_sync_behaviour(self, make_async_client):
        """The async client must be behaviourally identical (same items, same
        page requests) under the ambiguous `Page` field."""
        async_index, index_session = make_async_client(
            paged_responder(TOTAL, PAGE_SIZE, lambda p: p)
        )
        async_count, count_session = make_async_client(
            paged_responder(TOTAL, PAGE_SIZE, lambda p: EXPECTED_PAGES)
        )

        from_index = asyncio.run(async_index.fetch_all_pages(async_index.params))
        from_count = asyncio.run(async_count.fetch_all_pages(async_count.params))

        assert len(from_index) == TOTAL
        assert list(from_index) == list(from_count)
        assert sorted(index_session.calls) == list(range(1, EXPECTED_PAGES + 1))
        assert sorted(count_session.calls) == list(range(1, EXPECTED_PAGES + 1))
        assert from_index.page_field_mode == PAGE_SEMANTICS_INDEX
        assert from_count.page_field_mode == PAGE_SEMANTICS_COUNT


# ============== (iii) shortfall raises, never a short list ==============

class TestShortfallIsLoud:
    def test_total_beyond_the_available_pages_raises(self, make_client):
        """`Total`=100 but only 4 pages of 10 exist: the old code returned 40
        items as if the query were complete."""
        responder = paged_responder(
            TOTAL, PAGE_SIZE, lambda p: p, pages_available=4
        )
        client, session = make_client(responder)

        with pytest.raises(IncompleteResultsError) as excinfo:
            client.fetch_all_pages()

        error = excinfo.value
        assert error.expected == TOTAL
        assert error.collected == 40
        assert error.pages_fetched == 4
        assert "100" in str(error) and "40" in str(error)
        assert error.to_dict()["details"]["collected"] == 40

    def test_async_total_beyond_the_available_pages_raises(self, make_async_client):
        responder = paged_responder(
            TOTAL, PAGE_SIZE, lambda p: EXPECTED_PAGES, pages_available=4
        )
        client, session = make_async_client(responder)

        with pytest.raises(IncompleteResultsError) as excinfo:
            asyncio.run(client.fetch_all_pages(client.params))

        assert excinfo.value.expected == TOTAL
        assert excinfo.value.collected == 40

    def test_allow_partial_returns_short_harvest_with_provenance(self, make_client):
        responder = paged_responder(
            TOTAL, PAGE_SIZE, lambda p: p, pages_available=4
        )
        client, _ = make_client(responder)

        letters = client.fetch_all_pages(allow_partial=True)

        assert len(letters) == 40  # explicit opt-in: partial data is returned
        assert letters.complete is False
        assert letters.expected_total == TOTAL
        assert letters.arrived == 40
        assert letters.shortfall == 60
        assert letters.provenance()["complete"] is False
        assert letters.provenance()["arrived"] == 40


# ============== (iv) a runaway page count trips the cap ==============

class TestPageCap:
    def test_runaway_total_raises_before_hammering_the_api(self, make_client):
        """`Total`=1e9 at 10 items/page implies 1e8 pages. The cap must stop
        that before it becomes a crawl."""
        responder = paged_responder(10 ** 9, PAGE_SIZE, lambda p: p)
        client, session = make_client(responder)

        with pytest.raises(PaginationCapExceededError) as excinfo:
            client.fetch_all_pages()

        error = excinfo.value
        assert error.expected == 10 ** 8
        assert f"{MAX_PAGES_ABSOLUTE}" in str(error)
        # One request (page 1, which carries the metadata) and no more.
        assert session.calls == [1]

    def test_runaway_total_still_raises_with_allow_partial(self, make_client):
        """The absolute guard is not softened by `allow_partial`: politeness
        to codal.ir comes first."""
        responder = paged_responder(10 ** 9, PAGE_SIZE, lambda p: p)
        client, session = make_client(responder)

        with pytest.raises(PaginationCapExceededError):
            client.fetch_all_pages(allow_partial=True)

        assert session.calls == [1]

    def test_cap_is_needed_pages_plus_slack(self, make_client):
        assert page_cap(EXPECTED_PAGES, None) == EXPECTED_PAGES + PAGE_CAP_SLACK
        assert page_cap(EXPECTED_PAGES, 2000) == EXPECTED_PAGES + PAGE_CAP_SLACK
        # A caller's own bound still raises the ceiling when it is the larger.
        assert page_cap(MAX_PAGES_ABSOLUTE + 100, MAX_PAGES_ABSOLUTE + 100) == (
            MAX_PAGES_ABSOLUTE + 100
        )
        assert estimate_page_count(101, 10) == 11
        assert estimate_page_count(None, 10) == 0

    def test_a_bound_too_small_raises_rather_than_truncating(self, make_client):
        """max_pages below what `Total` needs used to be a silent truncation."""
        responder = paged_responder(TOTAL, PAGE_SIZE, lambda p: p)
        client, session = make_client(responder)

        with pytest.raises(PaginationCapExceededError):
            client.fetch_all_pages(max_pages=3)

        assert session.calls == [1]

    def test_a_bound_too_small_still_honours_allow_partial(self, make_client):
        responder = paged_responder(TOTAL, PAGE_SIZE, lambda p: p)
        client, _ = make_client(responder)

        letters = client.fetch_all_pages(max_pages=3, allow_partial=True)

        assert len(letters) == 30
        assert letters.expected_total == TOTAL
        assert letters.arrived == 30


# ============== (v) regressions: single page and normal multi-page ==============

class TestRegressions:
    def test_single_page_query_fetches_once(self, make_client):
        responder = sized_responder(3, [3], lambda p: 1)
        client, session = make_client(responder)

        letters = client.fetch_all_pages()

        assert len(letters) == 3
        assert session.calls == [1]
        assert letters.complete is True
        assert letters.pages_fetched == 1

    def test_multi_page_query_with_a_short_last_page(self, make_client):
        """25 items at 10/page: pages of 10, 10, 5 -- stop at `Total`."""
        responder = sized_responder(25, [10, 10, 5], lambda p: 3)
        client, session = make_client(responder)

        letters = client.fetch_all_pages()

        assert len(letters) == 25
        assert len(set(_tracing_numbers(letters))) == 25
        assert letters.expected_total == 25
        assert letters.complete is True
        assert session.calls == [1, 2, 3]
        assert letters.pages_fetched == 3

    def test_a_page_that_over_delivers_is_not_truncated(self, make_client):
        """More items than `Total` claims is not a shortfall: nothing is lost,
        and `complete` is still True."""
        responder = sized_responder(25, [10, 10, 10], lambda p: 3)
        client, session = make_client(responder)

        letters = client.fetch_all_pages()

        assert len(letters) == 30
        assert letters.expected_total == 25
        assert letters.complete is True
        assert session.calls == [1, 2, 3]

    def test_first_page_failure_still_returns_empty(self, make_client):
        """Unchanged behaviour (P0-4 covers provenance of failures)."""
        client, session = make_client(lambda page: None)
        assert client.fetch_all_pages() == []
        # Only page 1 is ever requested; the retries are the client's own
        # (retry_count=3), not extra pages.
        assert set(session.calls) == {1}

    def test_fetch_page_zero_is_rejected(self, make_client):
        """`fetch_page(0)` used to be falsy, so it silently re-fetched whatever
        page happened to be current."""
        responder = paged_responder(TOTAL, PAGE_SIZE, lambda p: p)
        client, session = make_client(responder)

        client.fetch_page(3)
        with pytest.raises(ValidationError):
            client.fetch_page(0)
        assert session.calls == [3]
        assert client.params["PageNumber"] == 3

    def test_fetch_page_updates_metadata_from_total_not_page(self, make_client):
        responder = paged_responder(TOTAL, PAGE_SIZE, lambda p: p)
        client, _ = make_client(responder)

        client.fetch_page(1)

        assert client.total_results == TOTAL
        assert client.total_pages == EXPECTED_PAGES  # ceil(100/10), not Page=1
        # Paging forward must not shrink the bound.
        client.fetch_page(2)
        assert client.total_pages == EXPECTED_PAGES
