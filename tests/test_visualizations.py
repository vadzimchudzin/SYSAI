"""Validation tests for numeric visualizations generated from transcript evidence."""
import datetime as dt

from app import analyze


TRANSCRIPT = "Продажи: январь — 10 тысяч рублей, февраль — 12 тысяч рублей."
CHART = {
    "title": "Продажи по месяцам",
    "type": "bar",
    "labels": ["Январь", "Февраль"],
    "series": [{"name": "Продажи", "values": [10, 12]}],
    "unit": "тыс. рублей",
    "source_quote": TRANSCRIPT,
}


def report(chart):
    return {
        "title": "Отчёт",
        "summary": "Обсудили продажи.",
        "next_meeting": None,
        "participants": [],
        "topics": [],
        "decisions": [],
        "tasks": [],
        "visualizations": [chart],
        "open_questions": [],
        "risks": [],
        "notes": [],
    }


def test_visualization_is_kept_when_its_source_quote_exists():
    out = analyze._validate_report(report(CHART), [], TRANSCRIPT, 60, dt.date(2026, 10, 18))
    assert out["visualizations"] == [CHART]


def test_visualization_without_source_evidence_is_removed_and_noted():
    chart = {**CHART, "source_quote": "Конверсия составила 84 процента."}
    out = analyze._validate_report(report(chart), [], TRANSCRIPT, 60, dt.date(2026, 10, 18))
    assert out["visualizations"] == []
    assert any("исключён" in note for note in out["notes"])


def test_visualization_rejects_mismatched_series_lengths():
    chart = {**CHART, "series": [{"name": "Продажи", "values": [10]}]}
    try:
        analyze._validate_report(report(chart), [], TRANSCRIPT, 60, dt.date(2026, 10, 18))
    except Exception as exc:
        assert "подписи" in str(exc) or "серии" in str(exc)
    else:
        raise AssertionError("Mismatched chart series should be rejected")
