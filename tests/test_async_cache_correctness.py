"""
Async cache-path correctness (P1-1 parity): the async client's cache entries
must carry the same metadata the synchronous client records, reads must go
through the same refusal-on-inconsistency semantics, `use_cache`/`refresh`
must be honoured, and the atomic-write + per-key-lock guarantees must hold
when up to `max_concurrent` async workers share one cache directory.

`search.codal.ir` is unreachable from the test machine, so the response shape
is simulated rather than assumed: every test drives a fake aiohttp session
serving a different payload per ``PageNumber``.
"""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

import pytest
from requests.exceptions import HTTPError

from codal_scraper.async_client import AsyncCodalClient
from codal_scraper.cache import (
    CACHE_OWNER, CACHE_RECORD_VERSION, CacheConfig, FileCache,
)
from codal_scraper.exceptions import IncompleteResultsError

TOTAL = 100
PAGE_SIZE = 10
QUERY_PARAMS = {
    "LetterCode": "ن-45",
    "FromDate": "1402/01/01",
    "ToDate": "1402/12/29",
}


# ============== Fake transport (no network) ==============
def _page_number_from_url(url: str) -> int:
    """Pull PageNumber out of the query URL the client actually built."""
    query = parse_qs(urlparse(url).query)
    return int(query["PageNumber"][0])


def _letters(start: int, count: int):
    """`count` letters whose TracingNo values identify which page they came
    from, so a truncated or duplicated harvest is visible in the data."""
    return [
        {
            "Url": f"/Reports/Decision.aspx?LetterSerial={start + i}",
            "TracingNo": str(start + i),
        }
        for i in range(count)
    ]


def page_responder(page_sizes, total):
    """Serve an explicit number of items per page, then empty pages."""
    def respond(page: int):
        if page > len(page_sizes):
            return {"Letters": [], "Total": total, "IsSuccess": True}
        start = sum(page_sizes[:page - 1]) + 1
        return {
            "Letters": _letters(start, page_sizes[page - 1]),
            "Total": total,
            "IsSuccess": True,
        }
    return respond


class _FakeAsyncResponse:
    def __init__(self, payload):
        self.status = 200
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
    """Stands in for aiohttp.ClientSession; records every page requested."""

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


class _CountingResponder:
    """Same page shape every time, but a fresh payload per request: lets a
    test tell a refetched response from a served cache entry."""

    def __init__(self, per_page: int, total: int):
        self._per_page = per_page
        self._total = total
        self.n = 0

    def __call__(self, page: int):
        self.n += 1
        letters = [
            {"TracingNo": str(self.n * 1000 + page * 10 + i)}
            for i in range(self._per_page)
        ]
        return {"Letters": letters, "Total": self._total, "IsSuccess": True}


# ============== Helpers ==============
def make_client(tmp_path, responder, name="cache", **client_kw):
    """An AsyncCodalClient with caching on, wired to a fake session. The cache
    directory is per-test unique, so passing the same `name` twice is how two
    clients share one cache directory."""
    client = AsyncCodalClient(
        enable_cache=True,
        cache_config=CacheConfig(
            cache_dir=str(tmp_path / name), cleanup_on_init=False
        ),
        **client_kw,
    )
    client._session = _FakeAsyncSession(responder)
    client.rate_limiter = _NoopRateLimiter()
    return client, client._session


def payload(n, total=None):
    total = n if total is None else total
    return {"Total": total, "Letters": [{"Title": f"l{i}"} for i in range(n)]}


def record_for(cache, url):
    path = cache._get_cache_path(cache._get_cache_key(url))
    return json.loads(path.read_text(encoding="utf-8"))


# ============== 1. async writes record the sync metadata ==============
def test_async_writes_record_sync_metadata(tmp_path):
    responder = page_responder([PAGE_SIZE] * 10, total=TOTAL)
    client, session = make_client(tmp_path, responder)
    letters = asyncio.run(client.fetch_all_pages(dict(QUERY_PARAMS)))
    assert len(letters) == TOTAL
    assert session.calls == list(range(1, 11))

    cache = client.cache
    query = client._build_url(dict(QUERY_PARAMS), page=1)
    for page in (1, 2):
        url = client._build_url(dict(QUERY_PARAMS), page=page)
        record = record_for(cache, url)
        assert record["owner"] == CACHE_OWNER
        assert record["version"] == CACHE_RECORD_VERSION
        meta = record["meta"]
        assert meta["status"] == 200
        assert meta["total"] == TOTAL
        assert meta["letters"] == PAGE_SIZE
        # Every page is tied to the crawl's page-1 identity, so an incomplete
        # harvest record can refuse the whole crawl later.
        assert meta["query"] == query
        assert meta["harvest"] is None
        # A page of a larger crawl is neither complete-in-itself nor short of
        # what was expected -- only False is ever refused, and the harvest
        # record carries the crawl's completeness (as on the sync path).
        assert meta["complete"] is None
        # Page 1 was fetched before `Total` was known; page 2 knows it should
        # hold exactly PAGE_SIZE items (Total minus what page 1 held).
        assert meta["expected"] == (None if page == 1 else PAGE_SIZE)


def test_async_complete_crawl_records_harvest_provenance(tmp_path):
    responder = page_responder([PAGE_SIZE] * 10, total=TOTAL)
    client, session = make_client(tmp_path, responder)
    asyncio.run(client.fetch_all_pages(dict(QUERY_PARAMS)))
    assert session.calls == list(range(1, 11))
    query = client._build_url(dict(QUERY_PARAMS), page=1)
    provenance = client.cache.harvest_provenance(query)
    assert provenance["complete"] is True
    assert provenance["arrived"] == TOTAL
    assert provenance["pages_fetched"] == 10


# ============== 2. inconsistent entries are refused, then refetched ==============
def test_async_inconsistent_entry_is_refused_and_refetched(tmp_path):
    responder = page_responder([PAGE_SIZE] * 10, total=TOTAL)
    client, session = make_client(tmp_path, responder)
    asyncio.run(client.fetch_all_pages(dict(QUERY_PARAMS)))

    # The payload on disk no longer holds what was recorded: exactly the
    # "truncated Letters array frozen for an hour" failure mode.
    cache = client.cache
    url1 = client._build_url(dict(QUERY_PARAMS), page=1)
    path = cache._get_cache_path(cache._get_cache_key(url1))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["data"]["Letters"] = record["data"]["Letters"][:1]
    path.write_text(json.dumps(record), encoding="utf-8")

    # A later run must refetch the tampered page, not serve it.
    client2, session2 = make_client(tmp_path, responder)
    letters = asyncio.run(client2.fetch_all_pages(dict(QUERY_PARAMS)))
    assert 1 in session2.calls
    assert len(letters) == TOTAL
    assert client2.cache.get_stats()["refusals"].get("letters_mismatch") == 1


def test_async_short_page_is_cached_incomplete_and_never_served(tmp_path):
    # Page 2 arrives with 2 of the 10 items it should have held.
    sizes = [10, 2] + [10] * 8
    responder = page_responder(sizes, total=TOTAL)
    client, session = make_client(tmp_path, responder)
    with pytest.raises(IncompleteResultsError):
        asyncio.run(client.fetch_all_pages(dict(QUERY_PARAMS)))

    cache = client.cache
    query = client._build_url(dict(QUERY_PARAMS), page=1)
    url2 = client._build_url(dict(QUERY_PARAMS), page=2)
    record = record_for(cache, url2)
    # The fact is cached, never the fiction: written with what it should have
    # held and marked incomplete.
    assert record["meta"]["letters"] == 2
    assert record["meta"]["expected"] == PAGE_SIZE
    assert record["meta"]["complete"] is False
    assert cache.get(url2) is None

    # The shortfall was recorded before the error propagated ...
    provenance = cache.harvest_provenance(query)
    assert provenance["complete"] is False
    assert provenance["expected_total"] == TOTAL
    assert provenance["arrived"] == TOTAL - 8
    # ... so page 1 -- consistent in itself -- is now refused too: it belongs
    # to a harvest recorded incomplete.
    url1 = client._build_url(dict(QUERY_PARAMS), page=1)
    assert cache.get(url1) is None
    refusals = cache.get_stats()["refusals"]
    assert refusals.get("incomplete") == 1
    assert refusals.get("incomplete_harvest") == 1


# ============== 3. use_cache / refresh are honoured ==============
def test_async_cache_hit_avoids_refetch_and_refresh_bypasses(tmp_path):
    responder = _CountingResponder(per_page=5, total=5)
    client1, session1 = make_client(tmp_path, responder)
    letters1 = asyncio.run(client1.fetch_all_pages(dict(QUERY_PARAMS)))
    assert session1.calls == [1]
    assert letters1[0]["TracingNo"] == "1010"

    # Second run: served from the cache, the API is not touched.
    client2, session2 = make_client(tmp_path, responder)
    letters2 = asyncio.run(client2.fetch_all_pages(dict(QUERY_PARAMS)))
    assert session2.calls == []
    assert len(letters2) == 5
    assert letters2[0]["TracingNo"] == "1010"

    # refresh=True ignores the fresh entry, refetches, and overwrites it.
    client3, session3 = make_client(tmp_path, responder)
    letters3 = asyncio.run(
        client3.fetch_all_pages(dict(QUERY_PARAMS), refresh=True)
    )
    assert session3.calls == [1]
    assert letters3[0]["TracingNo"] == "2010"
    # The entry on disk now holds the refreshed payload, still fully
    # self-consistent: refresh must not downgrade the record's metadata.
    cache = client3.cache
    url1 = client3._build_url(dict(QUERY_PARAMS), page=1)
    record = record_for(cache, url1)
    assert record["data"]["Letters"][0]["TracingNo"] == "2010"
    assert record["meta"]["status"] == 200
    assert record["meta"]["complete"] is True
    assert client3.cache.get(url1) == record["data"]


def test_async_use_cache_false_never_reads_or_writes(tmp_path):
    responder = page_responder([5], total=5)
    client1, session1 = make_client(tmp_path, responder)
    letters = asyncio.run(
        client1.fetch_all_pages(dict(QUERY_PARAMS), use_cache=False)
    )
    assert session1.calls == [1]
    assert len(letters) == 5
    # No page entries were written ...
    kinds = [
        json.loads(p.read_text(encoding="utf-8"))["kind"]
        for p in client1.cache.cache_dir.glob("*.json")
    ]
    assert "page" not in kinds
    # ... so a later run also goes to the API.
    client2, session2 = make_client(tmp_path, responder)
    asyncio.run(client2.fetch_all_pages(dict(QUERY_PARAMS)))
    assert session2.calls == [1]


# ============== 4. concurrency: async workers sharing one cache dir ==============
def test_async_concurrent_writers_never_corrupt_an_entry(tmp_path):
    """The asyncio equivalent of test_concurrent_writers_never_corrupt_an_entry:
    writers hammer one key while a task on the same loop reads the raw file."""
    cache = FileCache(
        CacheConfig(cache_dir=str(tmp_path / "cache"), cleanup_on_init=False)
    )
    url = "https://example.com/async-concurrent"
    key = cache._get_cache_key(url)
    path = cache._get_cache_path(key)
    payloads = [payload(3, total=3) for _ in range(4)]
    bad = []

    async def writer(worker):
        while True:
            cache.set(url, payloads[worker], status=200, expected=3)
            await asyncio.sleep(0)

    async def reader():
        while True:
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                await asyncio.sleep(0)
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as e:
                bad.append(f"truncated file observed: {e}")
                return
            if record.get("data") not in payloads:
                bad.append("partial payload observed")
                return
            await asyncio.sleep(0)

    async def main():
        tasks = [asyncio.create_task(writer(i)) for i in range(4)]
        tasks.append(asyncio.create_task(reader()))
        await asyncio.sleep(0.4)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(main())
    assert bad == []
    assert cache.get(url) in payloads
    # No temp files left behind by the atomic write.
    assert list(cache.cache_dir.glob("*.tmp")) == []


def test_concurrent_async_clients_sharing_one_cache_dir_stay_consistent(tmp_path):
    """Two max_concurrent crawls racing over the same query in one cache
    directory: every surviving record is a complete, consistent entry -- never
    a half-written file from the other worker."""
    responder = page_responder([PAGE_SIZE] * 3, total=30)
    client1, _ = make_client(tmp_path, responder, name="shared", max_concurrent=5)
    client2, _ = make_client(tmp_path, responder, name="shared", max_concurrent=5)

    async def main():
        return await asyncio.gather(
            client1.fetch_all_pages(dict(QUERY_PARAMS)),
            client2.fetch_all_pages(dict(QUERY_PARAMS)),
        )

    results = asyncio.run(main())
    assert [len(result) for result in results] == [30, 30]

    cache = client1.cache
    for page in (1, 2, 3):
        url = client1._build_url(dict(QUERY_PARAMS), page=page)
        record = record_for(cache, url)
        assert record["owner"] == CACHE_OWNER
        assert record["data"]["Total"] == 30
        assert record["meta"]["status"] == 200
        assert record["meta"]["letters"] == PAGE_SIZE
        # The winner of the race is servable: get() validates it end to end.
        assert cache.get(url) is not None
    assert list(cache.cache_dir.glob("*.tmp")) == []
