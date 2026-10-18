from starlette.datastructures import FormData

from app import main, settings_store


def test_explicit_balanced_style_overrides_non_default_global_style(monkeypatch):
    monkeypatch.setattr(
        settings_store,
        "get",
        lambda key, default=None: "executive" if key == "report_style" else default,
    )
    options = main._options(FormData({
        "deadline_mode": "ask",
        "report_style": "balanced",
    }))
    assert options["report_style"] == "balanced"


def test_default_style_is_not_redundantly_stored(monkeypatch):
    monkeypatch.setattr(
        settings_store,
        "get",
        lambda key, default=None: "balanced" if key == "report_style" else default,
    )
    options = main._options(FormData({
        "deadline_mode": "ask",
        "report_style": "balanced",
    }))
    assert "report_style" not in options
