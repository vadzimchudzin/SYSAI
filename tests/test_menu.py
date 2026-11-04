"""The left menu: groups that fold (the open ones in a cookie, the group of the open page always open) and the rail."""
from fastapi.testclient import TestClient

from app import accounts
from app.db import SessionLocal, User


def _opened(html: str) -> set:
    return {part.split('"')[0] for part in html.split('class="nav-group open" data-nav-group="')[1:]}


def _groups(html: str) -> set:
    return {part.split('"')[0] for part in html.split('data-nav-group="')[1:]}


def test_groups_fold_and_the_choice_is_remembered(client):
    html = client.get("/app").text
    assert _groups(html) == {"work", "company", "ai", "admin"} and _opened(html) == {"work"}  # the network is off
    assert 'href="/employees"' in html and 'href="/settings"' in html  # folded groups still have their links
    assert _opened(client.get("/employees").text) == {"work", "company"}  # the group of the open page
    client.cookies.set("sysai_nav", "ai.admin")
    assert _opened(client.get("/app").text) == {"ai", "admin"}
    assert '<html lang="ru" class="">' in client.get("/app").text
    client.cookies.set("sysai_side", "mini")
    assert '<html lang="ru" class="side-mini">' in client.get("/app").text  # the narrow rail of icons
    assert "/static/sidebar.js" in client.get("/app").text


def test_the_network_group_and_what_an_employee_sees(client, net_on):
    html = client.get("/files").text
    assert "net" in _opened(html) and 'href="/hub"' in html and 'href="/agents/access"' in html
    accounts.register("ivan", "correct-horse")
    with SessionLocal() as s:
        s.query(User).filter_by(username="ivan").one().status = "approved"
        s.commit()
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "ivan", "password": "correct-horse"})
        html = c.get("/app").text
        assert _groups(html) == {"work", "net"}
        assert "Мой агент" in html and 'href="/files"' in html and 'href="/agents/access"' not in html
