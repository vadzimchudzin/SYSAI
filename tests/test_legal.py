"""Belarus site rules: owner details, privacy policy, consent to personal data processing, no third-party requests."""
import re

import pytest

from app import accounts, settings_store
from app.db import SessionLocal, User


@pytest.fixture
def visitor():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def form(**extra):
    data = {"username": "ivan", "display_name": "Иван", "password": "correct-horse", "password2": "correct-horse", "consent": "1"}
    data.update(extra)
    return {k: v for k, v in data.items() if v is not None}


def test_owner_details_come_from_sysat():
    s = settings_store.all_settings()
    assert "СисЭйТи" in s["legal_name"] and "Борисов" in s["legal_address"] and s["legal_email"] == "company@sysat.by"


@pytest.mark.parametrize("path", ["/", "/login", "/register", "/privacy", "/legal"])
def test_public_pages_link_the_policy_and_show_no_third_party_hosts(visitor, path):
    html = visitor.get(path).text
    assert 'href="/privacy"' in html
    assert not re.search(r"(googleapis|gstatic|google-analytics|googletagmanager|yandex|metrika)", html)


def test_footer_shows_the_owner(visitor):
    html = visitor.get("/").text
    assert "СисЭйТи" in html and "company@sysat.by" in html and "Борисов" in html and 'href="/legal"' in html


def test_unp_appears_once_it_is_known(visitor):
    assert "УНП" not in visitor.get("/legal").text
    settings_store.set_many({"legal_unp": "123456789"})
    assert "УНП 123456789" in visitor.get("/").text
    assert "123456789" in visitor.get("/legal").text and "УНП 123456789" in visitor.get("/privacy").text


def test_policy_names_operator_purposes_recipients_and_rights(visitor):
    html = visitor.get("/privacy").text
    for part in ("99-З", "Оператор", "СисЭйТи", "Render", "Neon", "OpenRouter", "США", "Германия", "ст. 9", "отозвать согласие",
                 "cookie", "Национальный центр защиты персональных данных"):
        assert part in html, part


def test_the_sign_up_form_has_an_unticked_consent_box(visitor):
    html = visitor.get("/register").text
    box = re.search(r'<input type="checkbox" name="consent"[^>]*>', html).group(0)
    assert "required" in box and "checked" not in box
    assert 'href="/privacy"' in html and "США" in html


def test_a_request_without_consent_is_refused(visitor):
    r = visitor.post("/register", data=form(consent=None))
    assert r.status_code == 400 and "согласия" in r.text
    with SessionLocal() as s:
        assert s.query(User).filter_by(username="ivan").first() is None


def test_consent_is_stored_with_the_text_version(visitor):
    assert visitor.post("/register", data=form()).status_code == 200
    with SessionLocal() as s:
        user = s.query(User).filter_by(username="ivan").one()
        assert user.consent_at is not None and user.consent_version == accounts.CONSENT_VERSION


def test_superuser_edits_the_owner_details_in_settings(client):
    r = client.post("/settings", data={"legal_unp": "190000001", "pd_responsible": "Корнюшко С. М., директор",
                                       "legal_email": "privacy@sysat.by"})
    assert r.status_code in (200, 303)
    page = client.get("/legal").text
    assert "190000001" in page and "Корнюшко" in page and "privacy@sysat.by" in page
    assert "Владелец сайта" in client.get("/settings").text


def test_the_upload_page_warns_about_the_recording(client):
    html = client.get("/app/new").text
    assert "Предупредите участников" in html and "OpenRouter" in html
