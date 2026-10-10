"""
Shared pagination logic for the sync and async Codal clients (P0-3).

The Codal search API reports the size of a result set in its ``Total`` field.
Its ``Page`` field is ambiguous: it may be the *current page index* or the
*total page count*. Deriving the number of pages to fetch from ``Page`` is
unsafe under the first reading -- page 1 would report a page count of 1, so
every query would silently return only its first page.

This module never uses ``Page`` to decide how much to fetch:

* the number of pages needed is estimated from ``Total`` and the page size the
  API *actually returned* on page 1 (``len(Letters)``), not from a constant;
* the harvest runs until it has ``Total`` items or a page comes back empty;
* a hard page cap makes a runaway ``Total`` raise instead of hammering the API;
* the harvest is asserted against ``Total``, so a shortfall raises unless the
  caller opts in to ``allow_partial=True`` -- and a partial harvest still
  carries its provenance (expected vs arrived).

``Page`` is only ever *reported* (:func:`detect_page_semantics`), never
trusted. The first live run therefore tells us which semantics hold.
"""

from math import ceil
from typing import Any, Dict, List, Optional

from .exceptions import IncompleteResultsError, PaginationCapExceededError

import logging

logger = logging.getLogger(__name__)

#: Extra pages allowed beyond ``ceil(Total / page_size)`` so that a page whose
#: size differs from page 1 (or a single empty page mid-stream) does not turn
#: into a spurious failure -- while still bounding the request count.
PAGE_CAP_SLACK = 2

#: Absolute ceiling on pages fetched for a single query. Guards against a
#: runaway ``Total``/page-size combination turning into an unbounded crawl.
#: Callers that genuinely need more may pass ``max_pages`` explicitly.
MAX_PAGES_ABSOLUTE = 5000

# Values returned by :func:`detect_page_semantics`.
PAGE_SEMANTICS_INDEX = "current-page-index"
PAGE_SEMANTICS_COUNT = "total-page-count"
PAGE_SEMANTICS_AMBIGUOUS = "ambiguous"
PAGE_SEMANTICS_ABSENT = "absent"
PAGE_SEMANTICS_INCONSISTENT = "inconsistent"


def response_total(response: Dict[str, Any]) -> Optional[int]:
    """Return a usable ``Total`` from an API response, or None if unusable."""
    if not isinstance(response, dict):
        return None
    value = response.get("Total")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = int(value)
    return value if value > 0 else None


def estimate_page_count(total: Optional[int], page_size: int) -> int:
    """
    Pages needed to hold ``total`` items of ``page_size`` each.

    With no usable ``Total`` there is nothing to estimate, and 0 is returned.
    """
    if not total or total <= 0:
        return 0
    size = page_size if page_size and page_size > 0 else 1
    return int(ceil(total / size))


def page_cap(pages_needed: int, max_pages: Optional[int] = None) -> int:
    """
    Hard ceiling on the number of pages requested for one query.

    ``max_pages`` (the caller's own bound, as before) both limits the cap and
    may *raise* it above :data:`MAX_PAGES_ABSOLUTE` when a large query is
    genuinely intended.
    """
    ceiling = MAX_PAGES_ABSOLUTE if not max_pages else int(max_pages)
    return max(1, min(pages_needed + PAGE_CAP_SLACK, ceiling))


def detect_page_semantics(
    response: Dict[str, Any],
    requested_page: int,
    expected_pages: int
) -> str:
    """
    Report how the response's ``Page`` field is behaving, without trusting it.

    Comparing ``Page`` against both the page we just requested and the page
    count implied by ``Total``/page size tells us which reading the API uses
    (and flags a third possibility: neither, i.e. the field means something
    else entirely).
    """
    if not isinstance(response, dict):
        return PAGE_SEMANTICS_ABSENT
    page_field = response.get("Page")
    if page_field is None:
        return PAGE_SEMANTICS_ABSENT
    if isinstance(page_field, bool) or not isinstance(page_field, (int, float)):
        return PAGE_SEMANTICS_INCONSISTENT
    value = int(page_field)
    matches_index = value == requested_page
    matches_count = expected_pages > 0 and value == expected_pages
    if matches_index and matches_count:
        return PAGE_SEMANTICS_AMBIGUOUS
    if matches_index:
        return PAGE_SEMANTICS_INDEX
    if matches_count:
        return PAGE_SEMANTICS_COUNT
    return PAGE_SEMANTICS_INCONSISTENT


class PaginationResult(list):
    """
    The letters of a query's harvest, plus how that harvest went.

    It is a plain ``list`` of ``LetterData`` (so existing callers, slicing and
    ``len()`` keep working) that also records the provenance needed to tell a
    complete harvest from a truncated one: ``expected_total`` (what the API
    claimed) against ``len(self)`` (what arrived).
    """

    __slots__ = (
        "expected_total", "page_size", "pages_requested", "pages_fetched",
        "failed_pages", "page_cap", "page_field_mode",
    )

    def __init__(
        self,
        items=(),
        *,
        expected_total: Optional[int] = None,
        page_size: Optional[int] = None,
        pages_requested: int = 0,
        pages_fetched: int = 0,
        failed_pages: int = 0,
        page_cap: Optional[int] = None,
        page_field_mode: Optional[str] = None
    ):
        super().__init__(items)
        self.expected_total = expected_total
        self.page_size = page_size
        self.pages_requested = pages_requested
        self.pages_fetched = pages_fetched
        self.failed_pages = failed_pages
        self.page_cap = page_cap
        self.page_field_mode = page_field_mode

    @property
    def arrived(self) -> int:
        """Number of items actually collected."""
        return len(self)

    @property
    def complete(self) -> bool:
        """True when the harvest reached the API's reported total."""
        return self.expected_total is None or len(self) >= self.expected_total

    @property
    def shortfall(self) -> int:
        """Items the API claimed but that never arrived."""
        if self.expected_total is None:
            return 0
        return max(0, self.expected_total - len(self))

    def provenance(self) -> Dict[str, Any]:
        """Machine-readable description of where this data came from."""
        return {
            "expected_total": self.expected_total,
            "arrived": len(self),
            "complete": self.complete,
            "pages_fetched": self.pages_fetched,
            "pages_requested": self.pages_requested,
            "failed_pages": self.failed_pages,
            "page_size": self.page_size,
            "page_cap": self.page_cap,
            "page_field_mode": self.page_field_mode,
        }

    def __repr__(self) -> str:  # keep list repr, add the provenance headline
        return (
            f"PaginationResult({list.__repr__(self)}, "
            f"expected_total={self.expected_total!r}, arrived={len(self)}, "
            f"pages_fetched={self.pages_fetched!r})"
        )


def assert_within_cap(
    pages_needed: int,
    cap: int,
    *,
    total: Optional[int],
    page_size: int
) -> None:
    """
    Refuse to start paginating when the request implies more pages than the cap.

    Raised *before* paging, so a nonsensical ``Total`` costs one request rather
    than thousands.
    """
    if pages_needed <= cap:
        return
    raise PaginationCapExceededError(
        f"Refusing to paginate: response reports Total={total} with a page size "
        f"of {page_size}, implying {pages_needed} pages, above the page cap of "
        f"{cap}. Either the response metadata is wrong (e.g. a runaway 'Total') "
        f"or the query genuinely needs more pages -- pass max_pages explicitly "
        f"if the latter is intended.",
        expected=pages_needed,
        collected=0,
        details={"total": total, "page_size": page_size, "page_cap": cap}
    )


def finalize_harvest(
    result: PaginationResult,
    *,
    allow_partial: bool = False,
    query: str = ""
) -> PaginationResult:
    """
    Assert that a harvest collected everything the API said existed.

    A silent shortfall is the failure that would invalidate a published
    dataset, so it raises :class:`IncompleteResultsError` by default. With
    ``allow_partial=True`` the partial list is returned, but it still carries
    ``expected_total``, ``arrived``, ``shortfall`` and ``provenance()``.
    """
    if result.complete:
        return result
    message = (
        f"Paginated harvest incomplete: the API reported Total="
        f"{result.expected_total} but {result.arrived} items arrived "
        f"({result.shortfall} missing) from {result.pages_fetched} page(s) "
        f"fetched, page size {result.page_size}, page cap {result.page_cap}, "
        f"failed pages {result.failed_pages}; response 'Page' field read as "
        f"{result.page_field_mode}."
    )
    if query:
        message = f"{message} Query: {query}"
    if allow_partial:
        logger.warning(
            "allow_partial=True: returning partial results -- %s", message
        )
        return result
    raise IncompleteResultsError(
        message,
        expected=result.expected_total,
        collected=result.arrived,
        pages_fetched=result.pages_fetched,
        details={
            "page_size": result.page_size,
            "page_cap": result.page_cap,
            "failed_pages": result.failed_pages,
            "page_field_mode": result.page_field_mode,
            "allow_partial": allow_partial,
        }
    )


def new_result(
    first_response: Dict[str, Any],
    first_letters: List[Any],
    *,
    page_cap_value: int,
    page_field_mode: str
) -> PaginationResult:
    """Build the accumulator for a harvest from its first page."""
    return PaginationResult(
        first_letters,
        expected_total=response_total(first_response),
        page_size=len(first_letters),
        pages_requested=1,
        pages_fetched=1 if first_letters else 0,
        page_cap=page_cap_value,
        page_field_mode=page_field_mode,
    )
