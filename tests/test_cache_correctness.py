"""
P1-1 cache correctness: a stale or partial scrape must never be served as
fresh, a refresh must be possible through the public API, and invalidate()
must not be able to delete files the cache never wrote.
"""
import json
import threading
import time

import pytest

from codal_scraper import CodalClient
from codal_scraper.cache import (
    CACHE_OWNER, CACHE_RECORD_VERSION, CacheConfig, FileCache,
)


def make_cache(tmp_path, **kw):
    config = CacheConfig(cache_dir=str(tmp_path / "cache"), cleanup_on_init=False, **kw)
    return FileCache(config)


def payload(n, total=None):
    total = n if total is None else total
    return {"Total": total, "Letters": [{"Title": f"l{i}"} for i in range(n)]}


# ---------- 1. inconsistent entries are refused, not served ----------

def test_mismatched_entry_is_refused(tmp_path):
    cache = make_cache(tmp_path)
    url = "https://example.com/api?x=1"
    data = payload(3)
    cache.set(url, data, status=200, expected=3)
    assert cache.get(url) == data

    # The payload on disk no longer holds what was recorded: exactly the
    # "truncated Letters array frozen for an hour" failure mode.
    path = cache._get_cache_path(cache._get_cache_key(url))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["data"]["Letters"] = record["data"]["Letters"][:1]
    path.write_text(json.dumps(record), encoding="utf-8")

    assert cache.get(url) is None
    assert cache.is_cached(url) is False
    assert cache.get_stats()["refusals"].get("letters_mismatch") == 1


def test_truncated_page_is_refused(tmp_path):
    cache = make_cache(tmp_path)
    url = "https://example.com/api?page=2"
    # 20 items expected on page 2, only 2 arrived: written marked incomplete
    # and therefore never served.
    assert cache.set(url, payload(2, total=100), status=200, expected=20) is True
    assert cache.get(url) is None


def test_non_200_entry_is_refused(tmp_path):
    cache = make_cache(tmp_path)
    url = "https://example.com/api?flaky=1"
    cache.set(url, payload(3), status=503)
    assert cache.get(url) is None


def test_client_refetches_after_a_refused_entry(tmp_path, mock_session, mock_response, sample_api_response):
    client = CodalClient(cache_config=CacheConfig(cache_dir=str(tmp_path / "c"), cleanup_on_init=False))
    client._session = mock_session
    mock_session.get.return_value = mock_response(json_data=sample_api_response)
    assert client.fetch_page(1) is not None

    # Corrupt the cached payload's Letters array.
    cache = client.cache
    path = cache._get_cache_path(cache._get_cache_key(client.get_query_url(use_api=True)))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["data"]["Letters"] = record["data"]["Letters"][:1]
    path.write_text(json.dumps(record), encoding="utf-8")

    fresh = client.fetch_page(1)
    assert len(fresh) == len(sample_api_response["Letters"])  # refetched, not served


# ---------- 2. a valid entry is still served ----------

def test_valid_entry_is_still_served(tmp_path):
    cache = make_cache(tmp_path)
    url = "https://example.com/api?ok=1"
    data = payload(3, total=5)
    cache.set(url, data, status=200, total=5, letters=3, expected=3)
    assert cache.get(url) == data
    assert cache.is_cached(url) is True
    assert cache.get_stats()["hits"] == 1


def test_plain_payload_still_roundtrips(tmp_path):
    """No metadata: nothing to disagree with, so it is served as before."""
    cache = make_cache(tmp_path)
    cache.set("https://example.com/plain", {"value": 1})
    assert cache.get("https://example.com/plain")["value"] == 1


# ---------- 3. an incomplete harvest is never served as complete ----------

def test_incomplete_harvest_record_refuses_its_pages(tmp_path):
    cache = make_cache(tmp_path)
    query = "https://search.codal.ir/api/x?q=1"
    page_url = query + "&PageNumber=2"
    cache.set(page_url, payload(20, total=100), status=200,
              expected=20, query=query)
    assert cache.get(page_url) is not None

    # The harvest that produced this page is now recorded as incomplete.
    cache.record_harvest(query, {"expected_total": 100, "arrived": 40,
                                 "complete": False, "failed_pages": 1})
    assert cache.get(page_url) is None
    assert cache.is_cached(page_url) is False
    assert cache.get_stats()["refusals"].get("incomplete_harvest") == 1

    # A complete harvest re-admits the entry.
    cache.record_harvest(query, {"expected_total": 100, "arrived": 100,
                                 "complete": True, "failed_pages": 0})
    assert cache.get(page_url) is not None


def test_client_records_incomplete_harvest_provenance(tmp_path, mock_session, mock_response):
    """A harvest that falls short is recorded, so its pages are not reused."""
    from codal_scraper.exceptions import IncompleteResultsError

    def respond(url, **kwargs):
        return mock_response(json_data={"Total": 100, "Letters": [{"Title": "a"}]})

    client = CodalClient(cache_config=CacheConfig(cache_dir=str(tmp_path / "c"), cleanup_on_init=False))
    client._session = mock_session
    mock_session.get.side_effect = respond
    client.set_letter_code("ن-45")
    client.set_date_range("1402/01/01", "1402/12/29")

    with pytest.raises(IncompleteResultsError):
        client.fetch_all_pages(max_pages=1, show_progress=False, allow_partial=False)

    provenance = client.cache.harvest_provenance(client.get_query_url())
    assert provenance is not None and provenance["complete"] is False


# ---------- 4. ttl=0 means "expire now" ----------

def test_ttl_zero_expires_immediately(tmp_path):
    cache = make_cache(tmp_path)
    url = "https://example.com/ttl0"
    assert cache.set(url, payload(1), ttl=0) is True
    assert cache.get(url) is None  # never served
    assert cache.is_cached(url) is False


def test_negative_ttl_writes_nothing(tmp_path):
    cache = make_cache(tmp_path)
    url = "https://example.com/nocache"
    cache.set(url, payload(1), ttl=-1)
    assert cache.get(url) is None
    assert list(cache.cache_dir.glob("*.json")) == []


# ---------- 5. refresh through the public API ----------

def test_refresh_bypasses_a_fresh_valid_entry(tmp_path, mock_session, mock_response):
    client = CodalClient(cache_config=CacheConfig(cache_dir=str(tmp_path / "c"), cleanup_on_init=False))
    client._session = mock_session
    first = {"Total": 1, "Letters": [{"Title": "old"}]}
    second = {"Total": 1, "Letters": [{"Title": "new"}]}

    mock_session.get.return_value = mock_response(json_data=first)
    assert client.fetch_page(1)[0]["Title"] == "old"

    mock_session.get.return_value = mock_response(json_data=second)
    # The entry is fresh and valid, so a normal call still serves it...
    assert client.fetch_page(1)[0]["Title"] == "old"
    # ...but refresh=True refetches through the public API.
    assert client.fetch_page(1, refresh=True)[0]["Title"] == "new"
    # ...and the refreshed response has replaced the entry.
    assert client.fetch_page(1)[0]["Title"] == "new"


def test_search_methods_accept_use_cache_and_refresh(tmp_path, mock_session, mock_response):
    client = CodalClient(cache_config=CacheConfig(cache_dir=str(tmp_path / "c"), cleanup_on_init=False))
    client._session = mock_session
    mock_session.get.return_value = mock_response(
        json_data={"Total": 1, "Letters": [{"Title": "x"}]}
    )
    letters = client.search_by_symbol("فولاد", from_date="1402/01/01",
                                      to_date="1402/01/10", refresh=True)
    assert len(letters) == 1
    client.search_board_changes("1402/01/01", "1402/01/10", use_cache=False)


# ---------- 6. invalidate() cannot delete what it did not write ----------

def test_decoy_json_survives_every_invalidate(tmp_path):
    cache = make_cache(tmp_path)
    decoy = cache.cache_dir / "exported_results.json"
    decoy.write_text('{"thesis": "my precious export"}', encoding="utf-8")

    url = "https://example.com/api"
    cache.set(url, payload(2), status=200, expected=2)
    assert cache.get(url) is not None

    with pytest.raises(ValueError):
        cache.invalidate()  # no-argument bulk delete is no longer the default
    assert decoy.exists()

    assert cache.invalidate(url) == 1
    assert decoy.exists()

    cache.set(url, payload(2), status=200, expected=2)
    cache.cleanup()
    assert decoy.exists()

    # Even the opt-in bulk form only removes entries this cache wrote.
    assert cache.invalidate(bulk=True) == 1
    assert decoy.exists()
    assert decoy.read_text(encoding="utf-8") == '{"thesis": "my precious export"}'


def test_decoy_in_a_working_directory_is_safe(tmp_path):
    """CacheConfig(cache_dir=".") + invalidate() used to delete every *.json."""
    decoy = tmp_path / "results.json"
    decoy.write_text('{"export": 1}', encoding="utf-8")
    cache = FileCache(CacheConfig(cache_dir=str(tmp_path), cleanup_on_init=False))
    cache.set("https://example.com/api", payload(1), status=200)
    cache.invalidate(bulk=True)
    assert decoy.exists()


def test_legacy_entry_is_ignored_not_served(tmp_path):
    """Entries written before the format change carry no marker."""
    cache = make_cache(tmp_path)
    url = "https://example.com/legacy"
    path = cache._get_cache_path(cache._get_cache_key(url))
    path.write_text(json.dumps({"url": url, "data": {"Total": 5, "Letters": [{}]},
                                "created_at": time.time(),
                                "expires_at": time.time() + 3600}), encoding="utf-8")
    assert cache.get(url) is None      # not served: cannot be vouched for
    assert path.exists()               # ...and not deleted either


def test_record_is_marked_and_versioned(tmp_path):
    cache = make_cache(tmp_path)
    url = "https://example.com/marker"
    cache.set(url, payload(1), status=200)
    record = json.loads(cache._get_cache_path(cache._get_cache_key(url)).read_text())
    assert record["owner"] == CACHE_OWNER
    assert record["version"] == CACHE_RECORD_VERSION
    assert record["meta"]["status"] == 200


# ---------- 7. concurrent writes do not corrupt an entry ----------

def test_concurrent_writers_never_corrupt_an_entry(tmp_path):
    cache = make_cache(tmp_path)
    url = "https://example.com/concurrent"
    key = cache._get_cache_key(url)
    path = cache._get_cache_path(key)
    payloads = [payload(3, total=3) for _ in range(4)]
    stop = threading.Event()
    bad = []

    def writer(worker):
        while not stop.is_set():
            cache.set(url, payloads[worker], status=200, expected=3)

    def reader():
        while not stop.is_set():
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as e:
                bad.append(f"truncated file observed: {e}")
                stop.set()
                return
            if record.get("data") not in payloads:
                bad.append("partial payload observed")
                stop.set()
                return

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    threads.append(threading.Thread(target=reader))
    for t in threads:
        t.start()
    time.sleep(0.4)
    stop.set()
    for t in threads:
        t.join()

    assert bad == []
    assert cache.get(url) in payloads
    # No temp files left behind by the atomic write.
    assert list(cache.cache_dir.glob("*.tmp")) == []
