# Codal Scraper

High-level Python toolkit for fetching and processing disclosure data from [Codal.ir](https://codal.ir), the official information system for Iranian listed companies.

The library provides:

- **Synchronous client** (`CodalClient`) for Codal's search API
- **Asynchronous client** (`AsyncCodalClient`) for high-throughput scraping
- **Data processing pipeline** (`DataProcessor`) for cleaning, filtering, aggregating and exporting results
- **Board member scraper** (`BoardMemberScraper`) for detailed هیئت‌مدیره information using Playwright/Crawlee
- **Utilities** for Persian dates, text normalization, caching, and rate limiting

The codebase is structured as a reusable package (`src/codal_scraper`) with example scripts in `examples/` and tests in `tests/`.

---

## Installation

The package is **not on PyPI** — install from source:

```bash
git clone https://github.com/mohapak/codal_scraper.git
cd codal_scraper
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
```

This installs the package in editable mode with all core dependencies (requests, pandas, beautifulsoup4, jdatetime, networkx, openpyxl, pyarrow, aiohttp, tqdm). `requirements.txt` lists the same dependencies for non-editable installs.

### Optional extras

- **Board scraping** (`BoardMemberScraper`): `pip install -e ".[async]"` (installs `crawlee[playwright]` and `playwright`; you also need the Playwright browser binaries — run `playwright install` once)
- **Network visualization** (`visualize_network`): `pip install -e ".[viz]"` (installs `pyvis`)
- **Development / tests**: `pip install -e ".[dev]"` (installs `pytest`)

---

## Quick start

### Synchronous: search Codal and export to Excel

```python
from pathlib import Path
from codal_scraper import CodalClient, DataProcessor

output_dir = Path("output")
output_dir.mkdir(exist_ok=True)

# Search for هیئت‌مدیره changes (letter code ن-45) in a date range
with CodalClient() as client:
    letters = client.search_board_changes(
        from_date="1402/01/01",
        to_date="1402/12/29",
        max_pages=5,
    )

processor = DataProcessor(letters)
processor.add_letter_descriptions()
processor.to_excel(output_dir / "board_changes.xlsx")
```

The search methods return a `PaginationResult` — a plain `list` of letter dicts that also carries harvest provenance (see [Pagination and incomplete harvests](#pagination-and-incomplete-harvests)).

`examples/basic_usage.py` contains more end-to-end demos: searching board changes, searching by symbol, fetching financial statements, and chaining processing steps before export.

### Asynchronous: high-throughput fetching

```python
import asyncio
from pathlib import Path
from codal_scraper import AsyncCodalClient, DataProcessor

async def main():
    async with AsyncCodalClient(max_concurrent=5) as client:
        letters = await client.fetch_board_changes(
            from_date="1402/01/01",
            to_date="1402/12/29",
            max_pages=20,
        )

    processor = DataProcessor(letters)
    processor.add_letter_descriptions()
    processor.to_excel(Path("output") / "async_board_changes.xlsx")

asyncio.run(main())
```

`examples/async_usage.py` shows basic async fetching, fetching multiple symbols concurrently, and progress reporting.

### Scraping detailed board member data (هیئت‌مدیره)

> **Requires the `async` extra** (`pip install -e ".[async]"` plus `playwright install`). This scraper opens real Codal pages in a headless browser, so it cannot be exercised offline — the test suite covers its parsing helpers with synthetic stubs, not live pages.

```python
import asyncio
from pathlib import Path
from codal_scraper import CodalClient, BoardMemberScraper

async def main():
    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)

    # 1) Get board-change announcements and URLs
    with CodalClient() as client:
        letters = client.search_board_changes(
            from_date="1402/06/01",
            to_date="1402/12/29",
            max_pages=5,
        )
        urls = client.extract_letter_urls(letters)

    urls = urls[:10]  # small demo subset

    # 2) Scrape board member details from each Codal page
    async with BoardMemberScraper(max_concurrent=3, headless=True) as scraper:
        df = await scraper.scrape_urls(urls)
        scraper.export_to_excel(df, output_dir / "board_members.xlsx")

asyncio.run(main())
```

`examples/board_members_example.py` shows the full workflow: fetch ن-45 announcements, save Codal URLs to CSV, scrape detailed board-member records, export data/errors/network statistics, and build a board-network visualization (needs the `viz` extra for `visualize_network`).

---

## Main components

### `CodalClient` (sync)

Located in `src/codal_scraper/client.py`, exposed as `codal_scraper.CodalClient`.

- **Fluent, chainable API** based on Codal's query parameters (`set_symbol`, `set_date_range`, `set_letter_code`, `set_company_type`, …, `reset_params`)
- **Retry logic**, **rate limiting** and **file-based caching** (see [Caching](#caching))
- Convenience methods — all accept `use_cache: bool = True` and `refresh: bool = False`:
  - `search_board_changes(from_date, to_date, company_type=None, max_pages=None, use_cache=True, refresh=False)`
  - `search_financial_statements(from_date, to_date, period_length=12, audited_only=True, max_pages=None, use_cache=True, refresh=False)`
  - `search_monthly_reports(from_date, to_date, symbol=None, max_pages=None, use_cache=True, refresh=False)`
  - `search_by_symbol(symbol, from_date=None, to_date=None, max_pages=None, use_cache=True, refresh=False)`
- `fetch_page(page_number, use_cache=True, refresh=False)` and `fetch_all_pages(max_pages=None, use_cache=True, allow_partial=False, refresh=False)` for lower-level control
- Helpers:
  - `extract_letter_urls(letters)`, `save_urls_to_csv(letters, "letter_urls.csv")`
  - `download_file(url, file_path)`, `download_financial_excel_files(...)`
  - `get_summary_stats()`, `get_pagination_info()`, `clear_cache()`, `reset_stats()`

### `AsyncCodalClient`

Located in `src/codal_scraper/async_client.py`, exposed as `codal_scraper.AsyncCodalClient`.

- Uses `aiohttp` with an async rate limiter (`requests_per_second`, default 2.0)
- Fetches multiple pages concurrently via `fetch_all_pages(max_pages=None, use_cache=True, allow_partial=False, refresh=False)`
- High-level methods mirror the sync client — all five accept `use_cache: bool = True` and `refresh: bool = False`:
  - `fetch_board_changes(...)`
  - `fetch_financial_statements(...)`
  - `fetch_monthly_reports(...)`
  - `fetch_by_symbol(...)`
  - `fetch_multiple_symbols(symbols, from_date, to_date, ...)`
- `get_stats()` returns a `QueryStats` dataclass with performance metrics
- The async client's cache writes the same on-disk format as the sync client, but under a **separate key namespace** — a page fetched async will not be served to a sync client and vice versa

### `DataProcessor`

Located in `src/codal_scraper/processor.py`, exposed as `codal_scraper.DataProcessor`.

- Wraps a pandas `DataFrame` created from Codal API `Letters`
- Common operations (most are chainable):
  - Transformations: `add_letter_descriptions`, `add_column`, `apply_to_column`, `rename_columns`, `select_columns`, `sort_by`, `remove_duplicates`, `fill_na`, `drop_columns`, `reset_index`
  - Filtering: `filter_by_letter_code`, `filter_by_date_range`, `filter_by_symbols`, `filter_by_condition`
  - Aggregation: `aggregate_by_symbol_year`, `group_by`
  - Inspection: `get_summary_stats`, `get_column_info`, `describe`, `head`, `tail`
- Export:
  - `to_excel(path, include_summary=True)`
  - `to_csv(path)`
  - `to_json(path)`
  - `to_parquet(path)`
  - `to_dataframe()`, `to_dict()`
- Loading helpers (classmethods): `from_json_file`, `from_excel_file`, `from_csv_file`, `from_parquet_file`

### `BoardMemberScraper`

Located in `src/codal_scraper/board_scraper.py`, exposed as `codal_scraper.BoardMemberScraper`. Requires the `async` extra.

- Uses `crawlee` + Playwright to open each Codal ن-45 page and parse the هیئت‌مدیره grid
- Extracts a `BoardMemberData` record per board row (member identity, position, independence, education, multiple-role flags, CEO info, …)
- Maintains `members_data`, a `networkx` graph of company ↔ person relations, and a structured error list (`ScrapingErrorInfo`)
- High-level methods:
  - `scrape_urls(urls_or_df, url_column="url")`
  - `scrape_from_csv(csv_path, url_column="url")`
  - `export_to_excel(...)`, `export_to_csv(...)`, `export_errors(...)`
  - `visualize_network("board_network.html")` (needs the `viz` extra)
  - `get_stats()`, `get_error_summary()`, `reset()`

---

## Caching

Both clients cache API responses on disk by default, in `.codal_cache/` next to the working directory (configurable via `CacheConfig`). Caching exists to protect the dataset: a partial or inconsistent response is never served as if it were complete.

```python
from codal_scraper import CodalClient
from codal_scraper.cache import CacheConfig

config = CacheConfig(
    cache_dir=".codal_cache",  # any directory you like
    default_ttl=3600,          # seconds an entry stays valid
    max_size_mb=100,           # evict oldest entries beyond this budget
    enabled=True,              # False disables caching entirely
)
client = CodalClient(cache_config=config)
```

### What an entry records

Every entry written by this library is a JSON file with:

- `version` — the on-disk record schema version
- `owner` — a marker identifying the cache that wrote it (`codal_scraper.FileCache`)
- `kind` — `page` or `harvest` (provenance)
- `data` — the API payload
- `meta` — what made the payload trustworthy at write time: HTTP `status`, the API's `total`, the item count `letters`, the `expected` count for that page, `complete`, the parent `query`, and `harvest` provenance

Writes are atomic (`tempfile` + `fsync` + `os.replace`) under a per-key lock, so a reader never observes a half-written entry and two workers never interleave writes to the same key.

### What gets served — and what gets refused

Before serving an entry, the cache re-checks the payload against the recorded metadata and **refuses** it (returns a miss, so the caller refetches) when:

- it was not written by this cache (`untrusted`) — including entries from a **previous version of this library**, which use an older record format, and any foreign JSON file that merely lives in the directory
- the record shape is invalid or its format version is older (`stale_format`)
- it is expired
- the payload holds fewer items than the page should have delivered (`truncated`)
- the recorded HTTP status was not 200, or the `Letters`/`Total` counts no longer agree with what was recorded
- the harvest it belongs to was recorded **incomplete** — a partial crawl can never be handed back as complete on a later run

Every refusal is counted per reason: `client.cache.get_stats()['refusals']` shows why entries were not served. Refusals never raise — a refused entry simply becomes a refetch.

**Upgrading from an older `.codal_cache`: no manual clearing is required.** Old-format entries are ignored as untrusted, never served, and are simply overwritten by key on the next fetch. Foreign JSON files in the directory are never served, and never deleted by the cache.

### TTL semantics

For both `FileCache` and `MemoryCache`, the `ttl` argument of `set()`:

- `None` (or omitted) — use the configured `default_ttl`
- `0` — the entry is written already expired: it is stored but never served
- negative — nothing is stored at all ("do not cache")

### Selective refresh and invalidation

All sync public methods (`search_board_changes`, `search_financial_statements`, `search_monthly_reports`, `search_by_symbol`, `fetch_page`, `fetch_all_pages`) and all five async public methods accept:

- `use_cache=False` — skip the read for this call (the fetch still writes a fresh entry)
- `refresh=True` — skip the read **and** overwrite the cached entry

`invalidate()` deletes entries, but only ones this cache wrote (a matching `owner` marker) and only inside the cache directory:

```python
client.cache.invalidate(url, params)   # one entry
client.cache.invalidate(bulk=True)    # every owned entry — opt-in
client.cache.invalidate()             # ValueError: refuse to mean "everything" by accident
```

### Size budget

`max_size_mb` bounds the cache directory. When exceeded, the oldest **owned** entries are evicted first; harvest-provenance records are exempt (they are tiny, and deleting one would re-admit the partial pages it refuses). A budget of `0` disables enforcement. Unowned files are never removed to make room.

---

## Pagination and incomplete harvests

Page counts are derived from the API's `Total` and the actual page size of the first page, and the client detects which semantics the `Page` field uses (page index vs. page count). Two guards bound a crawl:

- `max_pages` — the caller's own bound per query
- an absolute ceiling of 5000 pages per query, regardless of `max_pages`

A harvest compares what arrived with what the API claimed (`Total`). `search_*`/`fetch_*` methods return a `PaginationResult` — a `list` of letters with extra provenance:

```python
with CodalClient() as client:
    letters = client.search_board_changes("1402/01/01", "1402/12/29")
print(len(letters), letters.expected_total, letters.complete, letters.shortfall)
print(letters.provenance())  # machine-readable: pages_fetched, failed_pages, page_cap, ...
```

If fewer items arrive than `Total` claimed — a failed page, a truncation, or the page cap being hit — the result is **not** returned by default: the call raises `IncompleteResultsError` (carrying `expected`, `collected`, `pages_fetched`) so a partial dataset can never pass silently as complete. This also covers the page cap: hitting `max_pages` (or the absolute 5000-page ceiling) before collecting everything raises `PaginationCapExceededError` unless you opt in.

```python
from codal_scraper.exceptions import IncompleteResultsError

with CodalClient() as client:
    try:
        letters = client.fetch_all_pages(max_pages=5)
    except IncompleteResultsError as e:
        print(f"Short harvest: expected {e.expected}, got {e.collected}")
        # Either raise max_pages and retry...
        letters = client.fetch_all_pages(max_pages=None)
```

Where a partial dataset is genuinely acceptable, `fetch_all_pages(..., allow_partial=True)` returns what arrived (the `PaginationResult` still records the shortfall in its provenance, and the incomplete harvest is recorded in the cache so its pages are refused on later runs).

---

## Refresh workflow (after a code fix)

When the scraping or parsing code changes, cached responses reflect the **old** code's behaviour. Do not delete the cache directory — refetch with `refresh=True`:

```python
# Re-run yesterday's query after a code fix, replacing stale entries:
letters = client.search_board_changes("1402/01/01", "1402/12/29", refresh=True)
```

Each refetched page overwrites its entry by key (same URL + params → same key). Entries for other queries stay valid. Deleting `.codal_cache/` also works, but it discards every entry, not just the affected query.

---

## Working with dates and the Persian calendar

Codal uses the **Jalali (Shamsi) calendar** with dates formatted as `YYYY/MM/DD` (e.g. `1402/01/01`).

This library:

- Validates Persian dates via `InputValidator.is_date`
- Offers helpers:
  - `get_current_persian_date()`, `get_current_persian_year()` (in `constants`)
  - `gregorian_to_shamsi`, `shamsi_to_gregorian`, `datetime_to_num`, `num_to_datetime` (in `utils`)
- Provides `DateRange` dataclass for strongly-typed date ranges

Always pass Persian dates like `"1402/01/01"` to query methods unless you explicitly convert them yourself.

---

## Error handling

Most public methods raise custom exceptions instead of raw ones:

- **Input problems** → `ValidationError`
- **HTTP / API issues** → `APIError`, `NetworkError`, `RateLimitError`
- **Pagination issues** → `PaginationError` (with `IncompleteResultsError` and `PaginationCapExceededError` below it)
- **Parsing issues** → `ParseError`
- **Scraping problems** → `ScrapingError` and detailed `ScrapingErrorInfo` records

Example pattern:

```python
from codal_scraper import CodalClient
from codal_scraper.exceptions import ValidationError, APIError, NetworkError

try:
    with CodalClient() as client:
        letters = client.search_board_changes("1402/01/01", "1402/12/29")
except ValidationError as e:
    print("Bad input:", e)
except APIError as e:
    print("Codal API error:", e)
except NetworkError as e:
    print("Network issue:", e)
```

---

## Utilities and supporting modules

- `codal_scraper.constants` – API URLs, header templates, Codal letter-code mappings, default rate-limit/cache settings, board-member CSS selectors, Persian year utilities.
- `codal_scraper.types` – `TypedDict` and `dataclass` types such as `LetterData`, `BoardMemberData`, `DateRange`, `QueryStats`, `PaginationInfo`, `ScrapingErrorInfo`.
- `codal_scraper.utils` – helpers for:
  - Persian/Arabic digit conversion and text normalization
  - Date conversion between Gregorian and Jalali (Shamsi)
  - Safe dict operations, URL building, list/collection utilities
  - Converting API responses to pandas `DataFrame` (`parse_codal_response`)
- `codal_scraper.cache` – file-based (`FileCache`, `CacheConfig`) and in-memory (`MemoryCache`) caching with TTL and integrity checks.
- `codal_scraper.rate_limiter` – sync (`RateLimiter`, `AdaptiveRateLimiter`) and async (`AsyncRateLimiter`) token-bucket limiters.
- `codal_scraper.validators` – `InputValidator` and `validate_input` for strict checking of Persian dates, symbols, ISIC codes, URLs, etc.
- `codal_scraper.exceptions` – exception hierarchy (`CodalScraperError`, `ValidationError`, `APIError`, `RateLimitError`, `NetworkError`, `ParseError`, `ScrapingError`, `CacheError`, `ConfigurationError`, `PaginationError` and its subclasses).

---

## Testing

```bash
pip install -e ".[dev]"
python -m pytest -q
```

The suite runs offline: network transport is simulated with fake sessions and canned payloads, so no request is sent to Codal.

Current status, reported as-is: `5 failed, 271 passed, 7 xfailed`. The 5 failures are **documented test defects** (tests written for an API that does not exist or behaves differently — e.g. a `CodalClient.get_stats` method that was never present; the real method is `get_summary_stats`), deliberately left failing rather than skipped, several awaiting design decisions. The 7 xfails are `strict=True` markers asserting behaviours that are **not yet correct** (documented defects in the board-scraper parsing/date logic, awaiting design decisions). Neither set indicates a runtime bug found in the library's main paths.

---

## Project layout

- `src/codal_scraper/` – main package
- `examples/` – runnable scripts demonstrating sync, async and board-scraper workflows
- `tests/` – pytest tests for client, async client, cache, pagination, processor, validators, utils, board scraper
- `output/` – example output files (Excel, CSV, JSON) created by sample scripts (ignored in git)
- `storage/` – Crawlee key-value stores and request queues created during board scraping (ignored in git; can be safely removed)

---

## Additional documentation

For a deeper, developer-focused explanation of the architecture and advanced usage patterns, see:

- `docs/USAGE.md` – detailed usage of `CodalClient`, `AsyncCodalClient`, `DataProcessor`, and `BoardMemberScraper`, plus examples of filtering, aggregation, board-network analysis, and testing.

Note: `docs/USAGE.md` predates the cache-correctness and pagination-harvest changes described above; this README is the reference for current behaviour.

---

## How to run the examples

From the project root:

```bash
source .venv/bin/activate  # Windows: .venv\Scripts\activate

python examples/basic_usage.py
python examples/async_usage.py
python examples/board_members_example.py   # needs the async extra
```

Most examples write results under the `output/` directory and hit the live Codal API — run them deliberately, and prefer the cached `use_cache=True` default on repeat runs.

---

## Packaging

This repository already includes:

- `src/` layout with the `codal_scraper` package
- `pyproject.toml` (setuptools build configuration, extras, metadata) and `setup.py`
- `requirements.txt` for development

To build a distributable:

```bash
pip install build
python -m build
```

---

## License

This project is licensed under the **MIT License**. See `LICENSE` for details.

---

Made with ❤️ for the Iranian financial community.
