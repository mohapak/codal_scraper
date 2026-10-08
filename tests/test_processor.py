from pathlib import Path

import pandas as pd
import pytest

from codal_scraper.processor import DataProcessor


LETTERS = [
    {
        "Url": "/Reports/Decision.aspx?LetterSerial=123456",
        "TracingNo": "123456",
        "Symbol": "فولاد",
        "CompanyName": "فولاد مبارکه اصفهان",
        "Title": "معرفی/تغییر در ترکیب اعضای هیئت مدیره",
        "LetterCode": "ن-45",
        "PublishDateTime": "1402/03/05 10:30:00",
        "HasExcel": True,
    },
    {
        "Url": "/Reports/Decision.aspx?LetterSerial=123457",
        "TracingNo": "123457",
        "Symbol": "فولاد",
        "CompanyName": "فولاد مبارکه اصفهان",
        "Title": "صورت های مالی میان دوره ای",
        "LetterCode": "ن-10",
        "PublishDateTime": "1402/02/20 09:00:00",
        "HasExcel": False,
    },
    {
        "Url": "/Reports/Decision.aspx?LetterSerial=123458",
        "TracingNo": "123458",
        "Symbol": "خودرو",
        "CompanyName": "ایران خودرو",
        "Title": "گزارش فعالیت ماهانه",
        "LetterCode": "ن-10",
        "PublishDateTime": "1402/01/10 08:00:00",
        "HasExcel": False,
    },
]


@pytest.fixture
def processor():
    """A DataProcessor over three Codal letters (two symbols, one ن-45)."""
    return DataProcessor(LETTERS)


def test_to_dataframe_normalizes_columns(processor):
    df = processor.to_dataframe()
    assert "publish_date_time" in df.columns
    assert df.loc[0, "letter_code"] == "ن-45"


def test_filter_by_letter_code(processor):
    filtered = processor.filter_by_letter_code("ن-45")
    df = filtered.to_dataframe()
    assert len(df) == 1
    assert df.iloc[0]["symbol"] == "فولاد"


def test_filter_by_date_range(processor):
    filtered = processor.filter_by_date_range("1402/02/01", "1402/12/29")
    assert len(filtered.to_dataframe()) == 2


def test_select_and_sort(processor):
    df = (
        processor.select_columns(["Symbol", "PublishDateTime"])
        .sort_values("publish_date_time", ascending=False)
        .to_dataframe()
    )
    assert list(df.columns) == ["symbol", "publish_date_time"]
    assert df.iloc[0]["publish_date_time"].startswith("1402/03/05")


def test_summary(processor):
    summary = processor.summary()
    assert summary["rows"] == 3
    assert summary["unique_symbols"] == 2
    assert summary["letter_code_breakdown"]["ن-45"] == 1


def test_groupby(processor):
    grouped = processor.groupby("symbol", {"tracing_no": "count"})
    assert isinstance(grouped, pd.DataFrame)
    assert grouped[grouped["symbol"] == "فولاد"]["tracing_no"].iloc[0] == 2


def test_export_to_csv(tmp_path, processor):
    out = processor.to_csv(tmp_path / "letters.csv")
    assert Path(out).exists()
    content = Path(out).read_text(encoding="utf-8")
    assert "Board change" in content
