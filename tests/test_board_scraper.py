"""Tests for ``codal_scraper.board_scraper`` — the module that produces the
thesis dataset.

Synthetic fixtures only: no network, no real Codal pages, no Playwright browser
and **no ``PlaywrightCrawler`` instance**.  The stubs below mimic exactly the
part of the Playwright API the scraper calls — a row object whose
``inner_text()`` returns tab-separated cells, a page whose ``query_selector`` /
``query_selector_all`` hand back those objects, and a crawling context carrying
``request.url``.

Convention for defects
----------------------
Tests that pin down behaviour the code review classified as a *defect* assert
the **correct** behaviour under ``pytest.mark.xfail(strict=True, reason="<finding
ID>: ...")``.  They fail today (reported as ``xfail``) and, because
``strict=True``, turn into a hard failure the day the defect is fixed — which
forces the mark to be removed instead of leaving a test that blesses the bug.
The finding IDs are from ``/home/team/shared/codal_scraper_review.md``.
"""

import asyncio
import logging

import networkx as nx
import pandas as pd
import pytest

from codal_scraper.board_scraper import BoardMemberScraper
from codal_scraper.constants import BOARD_MEMBER_SELECTORS
from codal_scraper.utils import is_independent_duty, year_month_from_date

SEL = BOARD_MEMBER_SELECTORS
STUB_URL = "https://codal.ir/stub/n-45"

# ZWNJ, as Codal renders "غیر موظف" in some responses (see P0-8).
ZWNJ = "\u200c"


def run(coro):
    """Drive one coroutine without pytest-asyncio (not a dependency here)."""
    return asyncio.run(coro)


class _Missing:
    """Sentinel: "do not put this selector in the stub page's metadata"."""

    def __str__(self):  # pragma: no cover - debugging aid only
        return "<missing>"


MISSING = _Missing()


# --------------------------------------------------------------------------
# Playwright stubs
# --------------------------------------------------------------------------

class StubElement:
    """Whatever ``page.query_selector`` hands back: something with inner_text."""

    def __init__(self, text):
        self._text = text

    async def inner_text(self):
        return self._text


class StubRow:
    """A grid row: ``inner_text()`` returns the cells tab-separated.

    This is the contract ``_parse_member_row`` is written against
    (``board_scraper.py:432-451``).
    """

    def __init__(self, cells):
        self._cells = list(cells)

    async def inner_text(self):
        return "\t".join(c if c is not None else "" for c in self._cells)


class StubPage:
    """Mimics the two page calls the scraper makes."""

    def __init__(self, rows=(), meta=None):
        self.rows = [StubRow(r) for r in rows]
        self.meta = dict(meta or {})

    async def query_selector_all(self, selector):
        if selector == SEL["table"]:
            return list(self.rows)
        # The broader fallback selector finds nothing in these fixtures.
        return []

    async def query_selector(self, selector):
        if selector in self.meta:
            return StubElement(self.meta[selector])
        return None


class StubRequest:
    def __init__(self, url=STUB_URL):
        self.url = url


class StubContext:
    def __init__(self, page, url=STUB_URL):
        self.page = page
        self.request = StubRequest(url)


def make_meta(date="1402/05/15", company="شرکت آزمون", assembly_date="1402/05/01"):
    """Selector -> inner_text() map for the page's metadata elements."""
    meta = {
        SEL["company"]: company,
        SEL["ceo_name"]: "مدیرعامل آزمون",
        SEL["ceo_national_id"]: "0012345678",
        SEL["ceo_degree"]: "دکتری",
        SEL["ceo_major"]: "اقتصاد",
        SEL["date"]: date,
        SEL["assembly_date"]: assembly_date,
    }
    return {k: v for k, v in meta.items() if v is not MISSING}


# --------------------------------------------------------------------------
# Row fixtures
# --------------------------------------------------------------------------

# Column indices of the 16-column (newer) layout, board_scraper.py:304-310.
LAYOUT16 = {
    "prev_member": 0,
    "new_member": 1,
    "member_id": 2,
    "prev_representative": 3,
    "new_representative": 4,
    "national_id": 5,
    "position": 6,
    "duty": 7,
    "degree": 8,
    "major": 9,
    "experience": 10,
    "multi_exec": 11,
    "multi_nonexec": 12,
    "declaration": 13,
    "acceptance": 14,
    "verification": 15,
}

# One unique token per column, so a mis-mapped index is visible in the record.
TOKENS16 = [
    "prev-mem", "new-mem", "12345", "prev-rep", "new-rep", "45678",
    "pos-token", "duty-token", "deg-token", "maj-token", "exp-token",
    "yes", "no", "decl-token", "acc-token", "verif-token",
]


def row16(**overrides):
    """A well-formed 16-column row; ``overrides`` keyed by field name."""
    cells = list(TOKENS16)
    for field, value in overrides.items():
        cells[LAYOUT16[field]] = value
    return cells


def row_of_width(n, last="last-cell"):
    """A row with exactly ``n`` columns (the trailing cell non-empty)."""
    cells = [f"c{i}" for i in range(n)]
    cells[-1] = last
    return cells


def make_scraper():
    """A scraper instance without a browser.

    ``BoardMemberScraper.__init__`` only builds a ``networkx`` graph; the
    crawler is created lazily in ``initialize_crawler`` (l.85-87, l.109-114),
    which is never called here.
    """
    return BoardMemberScraper()


def scrape_rows(scraper, rows, **meta_kw):
    """Run ``_scrape_board_members`` against stubbed rows."""
    context = StubContext(StubPage(rows, make_meta(**meta_kw)))
    run(scraper._scrape_board_members(context))
    return scraper.members_data


def member_record(**overrides):
    """A minimal record as ``_update_network`` receives it."""
    record = {
        "year": "1402",
        "company": "شرکت الف",
        "new_member": "عضو آزمون",
        "position": "عضو هیئت مدیره",
        "is_independent": False,
        "degree": "کارشناسی ارشد",
        "major": "مالی",
    }
    record.update(overrides)
    return record


# ==========================================================================
# 1. Row parsing — _parse_member_row, and the >=16 vs >=12 branch
# ==========================================================================

class TestParseMemberRow:
    def test_well_formed_sixteen_column_row_is_kept_intact(self):
        scraper = make_scraper()
        parsed = scraper._parse_member_row("\t".join(row16()))
        assert parsed == TOKENS16
        assert len(parsed) == 16

    def test_twelve_column_row_is_kept_intact(self):
        scraper = make_scraper()
        parsed = scraper._parse_member_row("\t".join(row_of_width(12)))
        assert len(parsed) == 12
        assert parsed[-1] == "last-cell"

    def test_cells_are_stripped_of_surrounding_whitespace(self):
        scraper = make_scraper()
        assert scraper._parse_member_row("  a  \t  b  ") == ["a", "b"]
        # Interior empty cells are kept, so column indices stay stable.
        assert scraper._parse_member_row("a\t\tb") == ["a", "", "b"]

    def test_trailing_layout_empty_cells_are_dropped(self):
        """A 16-column row whose last cells are blank parses as fewer columns.

        Consequence worth knowing (P1-5): such a row then takes the *older*
        layout branch, so the 4 trailing yes/no columns are never read.  This
        test documents the length only; the mis-mapping it causes is asserted
        in ``TestRowLayoutBranches``.
        """
        scraper = make_scraper()
        assert len(scraper._parse_member_row("a\tb\t\t\t")) == 2
        assert len(scraper._parse_member_row("\t".join(row16(verification="")))) == 15

    def test_row_between_the_branches_has_fourteen_columns(self):
        """14 columns: neither layout, but >= 12, so the older map is used."""
        scraper = make_scraper()
        parsed = scraper._parse_member_row("\t".join(TOKENS16[:14]))
        assert len(parsed) == 14

    def test_blank_row_returns_none(self):
        scraper = make_scraper()
        assert scraper._parse_member_row("") is None
        assert scraper._parse_member_row("\t\t") is None


class TestRowLayoutBranches:
    """The ``num_cols >= 16`` / ``>= 12`` index selection (l.287-324)."""

    def test_sixteen_column_row_uses_the_newer_index_map(self):
        scraper = make_scraper()
        rows = scrape_rows(scraper, [row16(duty="غیر موظف")])

        assert len(rows) == 1
        record = rows[0]
        assert record["prev_member"] == "prev-mem"
        assert record["new_member"] == "new-mem"
        assert record["member_id"] == "12345"
        assert record["prev_representative"] == "prev-rep"
        assert record["new_representative"] == "new-rep"
        assert record["national_id"] == "45678"
        assert record["position"] == "pos-token"
        assert record["degree"] == "deg-token"
        assert record["major"] == "maj-token"
        # Newer-layout-only columns:
        assert record["experience"] == "exp-token"
        assert record["verification_status"] == "verif-token"
        assert record["has_multiple_executive"] is True
        assert record["has_multiple_non_executive"] is False
        assert record["has_corporate_declaration"] is False
        assert record["has_position_acceptance"] is False

    def test_twelve_column_row_uses_the_older_index_map(self):
        # Older layout: no experience / declaration / acceptance / verification
        # column, so the yes/no flags sit at 10 and 11.
        cells = [f"c{i}" for i in range(10)] + ["yes", "no"]
        scraper = make_scraper()
        rows = scrape_rows(scraper, [cells])

        assert len(rows) == 1
        record = rows[0]
        assert record["experience"] == ""
        assert record["verification_status"] == ""
        assert record["has_multiple_executive"] is True
        assert record["has_multiple_non_executive"] is False
        assert record["has_corporate_declaration"] is False
        assert record["has_position_acceptance"] is False

    def test_fourteen_column_row_is_read_with_the_twelve_column_map(self):
        """DOCUMENTS A DEFECT (P1-5) — see the xfail below.

        A 14-column row is not the older 12-column layout, but it is >= 12, so
        ``idx_multi_exec = 10`` / ``idx_multi_nonexec = 11`` are applied to it.
        Column 10 of the newer layout is *experience*, so a free-text cell is
        read as a yes/no answer ("exp-token" -> silently ``False``) and the
        real ``yes`` in column 11 lands in ``has_multiple_non_executive``.
        ``experience`` is dropped entirely.
        """
        scraper = make_scraper()
        rows = scrape_rows(scraper, [TOKENS16[:14]])

        assert len(rows) == 1
        record = rows[0]
        assert record["experience"] == ""             # column 10 discarded
        assert record["has_multiple_executive"] is False   # 'exp-token'
        assert record["has_multiple_non_executive"] is True  # the shifted 'yes'
        assert record["verification_status"] == ""

    @pytest.mark.xfail(
        strict=True,
        reason="P1-5: a 14-column row is neither the 12- nor the 16-column "
               "layout, yet it is silently parsed with the 12-column index map",
    )
    def test_fourteen_column_row_should_not_be_silently_accepted(self):
        scraper = make_scraper()
        scrape_rows(scraper, [TOKENS16[:14]])
        # Correct behaviour: fail loudly (record the row as an error and keep
        # it out of the dataset) instead of shifting its values.
        assert not scraper.members_data
        assert scraper.errors


# ==========================================================================
# 2. Rows below the 12-field threshold (l.279-286)
# ==========================================================================

class TestRowsBelowFieldThreshold:
    def test_eleven_field_row_is_dropped_at_debug_level(self, caplog):
        """DOCUMENTS A DEFECT (P1-5) — the drop is silent.

        The row never reaches ``members_data`` and nothing above DEBUG is
        logged, so a truncated grid looks exactly like a clean run.  The
        desired behaviour is asserted in the xfail test below.
        """
        scraper = make_scraper()
        with caplog.at_level(logging.DEBUG):
            rows = scrape_rows(scraper, [row_of_width(11)])

        assert rows == []
        assert "Skipping incomplete row with 11 fields" in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.xfail(
        strict=True,
        reason="P1-5: rows under 12 fields are dropped at DEBUG and never "
               "recorded in self.errors, so data loss is invisible",
    )
    def test_short_row_should_be_recorded_as_an_error(self):
        scraper = make_scraper()
        scrape_rows(scraper, [row_of_width(11)])
        assert len(scraper.errors) == 1

    def test_blank_row_is_skipped_before_parsing(self):
        """A wholly empty grid row (layout artefact) yields no record."""
        scraper = make_scraper()
        assert scrape_rows(scraper, [[""]]) == []
        assert scrape_rows(scraper, [["   "]]) == []

    def test_good_rows_around_a_bad_row_are_still_kept(self):
        scraper = make_scraper()
        rows = scrape_rows(
            scraper, [row_of_width(5), row16(), row_of_width(11)]
        )
        assert len(rows) == 1
        assert rows[0]["new_member"] == "new-mem"


# ==========================================================================
# 3. Date extraction (l.252-265)
# ==========================================================================

class TestDateExtraction:
    @pytest.mark.parametrize(
        "date,expected_date_num,expected_year,expected_month",
        [
            ("1402/05/15", "14020515", "1402", "05"),   # zero-padded
            ("1402/5/15", "14020515", "1402", "05"),    # unpadded (P0-6)
            ("1402/12/29", "14021229", "1402", "12"),
        ],
    )
    def test_padded_and_unpadded_dates_agree(
        self, date, expected_date_num, expected_year, expected_month
    ):
        scraper = make_scraper()
        rows = scrape_rows(scraper, [row16()], date=date)

        assert rows[0]["date"] == expected_date_num
        assert rows[0]["year"] == expected_year
        assert rows[0]["month"] == expected_month

    def test_malformed_date_leaves_row_date_empty_and_warns(self, caplog):
        scraper = make_scraper()
        with caplog.at_level(logging.WARNING):
            rows = scrape_rows(scraper, [row16()], date="not a date")

        assert rows[0]["date"] == ""
        assert rows[0]["year"] == ""
        assert rows[0]["month"] == ""
        assert "Unparseable session date" in caplog.text

    def test_missing_date_element_leaves_row_date_empty(self):
        scraper = make_scraper()
        rows = scrape_rows(scraper, [row16()], date=MISSING)
        assert rows[0]["date"] == ""
        assert rows[0]["year"] == ""

    @pytest.mark.parametrize("date", ["1402/51/15", "1402/13/01", "1402/00/01"])
    def test_impossible_month_cannot_reach_a_row(self, date):
        """Regression for P0-6: ``1402/5/15`` used to yield ``month == 51``.

        A month outside 1..12 yields empty year/month and empty ``date``; the
        row is still emitted (that is a separate §6 finding) but carries no
        invented month.
        """
        scraper = make_scraper()
        rows = scrape_rows(scraper, [row16()], date=date)
        assert rows[0]["month"] == ""
        assert rows[0]["year"] == ""
        assert rows[0]["date"] == ""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1402/05/15", ("1402", "05")),
            ("1402/5/15", ("1402", "05")),      # unpadded (P0-6)
            ("1402/12/29", ("1402", "12")),
            ("1402/51/15", ("", "")),           # impossible month
            ("1402/13/01", ("", "")),
            ("garbage", ("", "")),
            ("", ("", "")),
            (None, ("", "")),
        ],
    )
    def test_year_month_from_date_contract(self, value, expected):
        assert year_month_from_date(value) == expected


# ==========================================================================
# 4. Independence detection (l.376-377)
# ==========================================================================

class TestIndependenceDetection:
    @pytest.mark.parametrize(
        "duty,expected",
        [
            ("غیر موظف", True),                   # plain space
            ("غیر" + ZWNJ + "موظف", True),        # ZWNJ (P0-8 regression)
            ("   غیر" + ZWNJ + "موظف   ", True),  # padded ZWNJ spelling
            ("موظف", False),                      # plain executive
            ("عضو موظف", False),
            ("", False),                          # empty duty cell
            (None, False),                        # missing duty cell
        ],
    )
    def test_duty_cell_sets_is_independent(self, duty, expected):
        scraper = make_scraper()
        rows = scrape_rows(scraper, [row16(duty=duty)])
        assert rows[0]["is_independent"] is expected

    def test_is_independent_duty_accepts_both_spellings(self):
        """Direct regression for P0-8."""
        assert is_independent_duty("غیر موظف") is True
        assert is_independent_duty("غیر" + ZWNJ + "موظف") is True
        assert is_independent_duty("موظف") is False
        assert is_independent_duty("") is False


# ==========================================================================
# 5. _parse_yes_no_value (l.465-478)
# ==========================================================================

class TestParseYesNoValue:
    @pytest.mark.parametrize(
        "value", ["بله", " بله ", "yes", "YES", "Yes", "true", "True", "TRUE", "1", " 1 "]
    )
    def test_accepted_literals_are_true(self, value):
        assert make_scraper()._parse_yes_no_value(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "خیر",      # the documented "no" literal
            "",
            None,
            "no",
            "false",
            "0",
            "2",
            "y",
            "بله غیر موظف",   # a real cell with extra wording -> False silently
        ],
    )
    def test_everything_else_is_silently_false(self, value):
        """A cell that is neither بله nor a known English literal is ``False``.

        Nothing raises and nothing is logged, so an unrecognised spelling of
        "yes" (Arabic ``نعم``, a cell with trailing wording, a changed column)
        becomes a genuine-looking ``False`` in the exported dataset.
        """
        assert make_scraper()._parse_yes_no_value(value) is False


# ==========================================================================
# 6. Graph construction (_update_network, l.480-512)
# ==========================================================================

class TestNetworkConstruction:
    def test_company_and_person_nodes_carry_their_attributes(self):
        scraper = make_scraper()
        scraper._update_network(member_record())

        graph = scraper.network
        assert graph.has_node("شرکت الف")
        assert graph.has_node("عضو آزمون")
        assert graph.nodes["شرکت الف"]["node_type"] == "company"
        assert graph.nodes["عضو آزمون"]["node_type"] == "person"
        assert graph.nodes["عضو آزمون"]["degree"] == "کارشناسی ارشد"
        assert graph.nodes["عضو آزمون"]["major"] == "مالی"

        edge = graph["شرکت الف"]["عضو آزمون"]
        assert edge["position"] == "عضو هیئت مدیره"
        assert edge["year"] == "1402"
        assert edge["is_independent"] is False
        assert graph.number_of_edges() == 1

    def test_person_nodes_are_keyed_by_the_member_name(self):
        scraper = make_scraper()
        scraper._update_network(member_record(new_member="حسین آزمون"))
        assert "حسین آزمون" in scraper.network

    def test_only_new_member_is_added_to_the_graph(self):
        scraper = make_scraper()
        scraper._update_network(
            member_record(prev_member="عضو رفته", new_member="عضو آمده")
        )
        assert "عضو آمده" in scraper.network
        assert "عضو رفته" not in scraper.network

    def test_record_without_a_new_member_adds_nothing(self):
        scraper = make_scraper()
        scraper._update_network(member_record(new_member=""))
        assert scraper.network.number_of_nodes() == 0

    def test_record_without_a_company_adds_nothing(self):
        scraper = make_scraper()
        scraper._update_network(member_record(company=""))
        assert scraper.network.number_of_nodes() == 0

    @pytest.mark.xfail(
        strict=True,
        reason="P0-7 (§6): nx.Graph.add_edge overwrites the edge attribute "
               "dict, so multi-year membership collapses to the last year",
    )
    def test_both_years_survive_for_a_multi_year_membership(self):
        scraper = make_scraper()
        scraper._update_network(member_record(year="1402"))
        scraper._update_network(member_record(year="1403"))

        edge = scraper.network["شرکت الف"]["عضو آزمون"]
        # Correct behaviour: the year dimension is recoverable (e.g. a key per
        # (person, firm, jalali_year)).  Today only "1403" survives.
        assert edge["year"] == ["1402", "1403"]

    @pytest.mark.xfail(
        strict=True,
        reason="§6: person nodes are keyed on the normalised name string, not "
               "member_id/national_id, so one director can become several nodes",
    )
    def test_one_director_with_two_name_spellings_is_one_node(self):
        scraper = make_scraper()
        # Same national id, same person, two routine Persian spellings.
        scraper._update_network(
            member_record(new_member="عبدالحسین آزمون", national_id="1234567890")
        )
        scraper._update_network(
            member_record(new_member="عبد الحسین آزمون", national_id="1234567890")
        )

        # One company + one person.
        assert scraper.network.number_of_nodes() == 2


# ==========================================================================
# 7. members_data accumulation across scrape_urls calls (P1-5)
# ==========================================================================

class StubCrawler:
    """Stands in for the Playwright crawler: it appends records instead."""

    def __init__(self, scraper):
        self.scraper = scraper

    async def run(self, urls):
        for url in urls:
            self.scraper.members_data.append(member_record(url=url))


class TestMembersDataAccumulation:
    @pytest.mark.xfail(
        strict=True,
        reason="P1-5: scrape_urls never resets members_data, so a second call "
               "returns the first call's records as well as its own",
    )
    def test_second_call_does_not_return_the_first_calls_records(self, monkeypatch):
        scraper = make_scraper()

        async def fake_initialize_crawler():
            # No PlaywrightCrawler is constructed anywhere in this file.
            scraper._crawler = StubCrawler(scraper)
            scraper._is_initialized = True

        monkeypatch.setattr(scraper, "initialize_crawler", fake_initialize_crawler)

        first = run(scraper.scrape_urls(["https://codal.ir/a"]))
        second = run(scraper.scrape_urls(["https://codal.ir/b"]))

        assert list(first["url"]) == ["https://codal.ir/a"]
        # Correct behaviour: each result frame holds only what that call
        # scraped.  Today the second frame still carries /a, and re-scraping
        # the same URL would duplicate every record.
        assert list(second["url"]) == ["https://codal.ir/b"]

    def test_reset_clears_members_data_and_network(self):
        scraper = make_scraper()
        scraper.members_data.append(member_record())
        scraper._update_network(member_record())

        scraper.reset()

        assert scraper.members_data == []
        assert scraper.network.number_of_nodes() == 0

    def test_scrape_urls_with_no_valid_urls_returns_empty_frame(self):
        scraper = make_scraper()
        result = run(scraper.scrape_urls([]))
        assert isinstance(result, pd.DataFrame)
        assert result.empty


# ==========================================================================
# 8. Export helpers (P1-6)
# ==========================================================================

class TestExportHelpers:
    def test_export_to_csv_writes_the_file(self, tmp_path):
        scraper = make_scraper()
        out = tmp_path / "board.csv"
        scraper.export_to_csv(pd.DataFrame([member_record()]), str(out))

        assert out.exists()
        written = pd.read_csv(out)
        assert list(written.columns) == list(member_record().keys())
        assert written.loc[0, "new_member"] == "عضو آزمون"

    def test_export_to_csv_with_no_data_writes_no_file(self, tmp_path):
        scraper = make_scraper()
        out = tmp_path / "empty.csv"
        assert scraper.export_to_csv(pd.DataFrame(), str(out)) is None
        assert not out.exists()

    @pytest.mark.xfail(
        strict=True,
        reason="P1-6: export_to_csv swallows the failure and returns None, "
               "indistinguishable from a successful export",
    )
    def test_export_to_csv_signals_a_failed_write(self, tmp_path):
        scraper = make_scraper()
        missing_dir = tmp_path / "no-such-dir" / "board.csv"
        result = scraper.export_to_csv(pd.DataFrame([member_record()]), str(missing_dir))
        assert result is False

    @pytest.mark.xfail(
        strict=True,
        reason="P1-6: export_to_excel swallows the failure and returns None, "
               "so a partial export looks complete",
    )
    def test_export_to_excel_signals_a_failed_write(self, tmp_path):
        scraper = make_scraper()
        missing_dir = tmp_path / "no-such-dir" / "board.xlsx"
        result = scraper.export_to_excel(
            pd.DataFrame([member_record()]), str(missing_dir)
        )
        assert result is False
