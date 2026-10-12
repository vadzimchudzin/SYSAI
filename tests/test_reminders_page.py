"""The "Напоминания" page of the panel: list, add, edit, switch, delete; superusers only."""
import datetime as dt
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from fastapi.testclient import TestClient

from app import accounts, meeting_alerts, reminders
from app.db import Employee, MeetingAlert, SessionLocal, User
from test_ai_mode import make_meeting


@pytest.fixture(autouse=True)
def staff():
    make_meeting()
    with SessionLocal() as s:
        s.add(Employee(name="Пётр Без Телеграма", position="Менеджер"))
        s.commit()


def ids():
    with SessionLocal() as s:
        return {e.name: e.id for e in s.query(Employee)}


def form(**over):
    people = ids()
    data = {"id": "0", "title": "Планёрка", "meet_time": "10:00", "lead_minutes": "15", "weekdays": ["0", "1", "2", "3", "4"],
            "recipients": [str(people["Иван Петров"]), str(people["Анна Орлова"])], "details": "Переговорная 2", "once_date": ""}
    data.update(over)
    return data


def message(response):
    return unquote(parse_qs(urlparse(response.headers["location"]).query).get("msg", [""])[0])


def alert_row(alert_id=1):
    with SessionLocal() as s:
        return s.get(MeetingAlert, alert_id)


def test_the_page_opens_and_the_menu_has_the_item(client):
    page = client.get("/reminders")
    assert page.status_code == 200 and "Напоминания" in page.text and "Пока нет ни одного напоминания" in page.text
    assert 'href="/reminders"' in client.get("/app").text


def test_an_alert_is_added_and_shown_as_a_card(client):
    answer = client.post("/reminders", data=form(), follow_redirects=False)
    assert answer.status_code == 303 and message(answer) == "Сохранено"
    page = client.get("/reminders").text
    assert "Планёрка" in page and "10:00" in page and "за 15 мин" in page and "Переговорная 2" in page
    assert "следующее напоминание" in page and "2 чел." in page
    row = alert_row()
    assert row.enabled and row.weekdays == [0, 1, 2, 3, 4] and row.lead_minutes == 15 and len(row.recipients) == 2


def test_the_switch_turns_an_alert_off_and_on(client):
    client.post("/reminders", data=form())
    answer = client.post("/reminders/1/toggle", follow_redirects=False)
    assert message(answer) == "Выключено" and alert_row().enabled is False
    assert "выключено" in client.get("/reminders").text
    answer = client.post("/reminders/1/toggle", follow_redirects=False)
    assert message(answer) == "Включено" and alert_row().enabled is True


def test_an_alert_is_edited_in_place(client):
    client.post("/reminders", data=form())
    page = client.get("/reminders?edit=1").text
    assert 'value="Планёрка"' in page and 'name="meet_time" required value="10:00"' in page
    client.post("/reminders", data=form(id="1", title="Летучка", meet_time="09:30", lead_minutes="30", weekdays=["0"]))
    row = alert_row()
    assert (row.title, row.meet_time, row.lead_minutes, row.weekdays) == ("Летучка", "09:30", 30, [0])
    with SessionLocal() as s:
        assert s.query(MeetingAlert).count() == 1


def test_an_alert_is_deleted(client):
    client.post("/reminders", data=form())
    answer = client.post("/reminders/1/delete", follow_redirects=False)
    assert message(answer) == "Удалено"
    with SessionLocal() as s:
        assert s.query(MeetingAlert).count() == 0


@pytest.mark.parametrize("over, fragment", [
    ({"recipients": []}, "кому напоминать"),
    ({"meet_time": "сейчас"}, "время совещания"),
    ({"weekdays": []}, "дни недели"),
    ({"lead_minutes": "-5"}, "минут"),
])
def test_a_bad_form_goes_back_with_the_reason(client, over, fragment):
    answer = client.post("/reminders", data=form(**over), follow_redirects=False)
    assert answer.status_code == 303 and fragment in message(answer)
    with SessionLocal() as s:
        assert s.query(MeetingAlert).count() == 0


def test_a_bad_edit_goes_back_to_the_edit_form(client):
    client.post("/reminders", data=form())
    answer = client.post("/reminders", data=form(id="1", recipients=[]), follow_redirects=False)
    assert answer.headers["location"].startswith("/reminders?edit=1&msg=")


def test_people_without_telegram_cannot_be_ticked_and_are_marked(client):
    page = client.get("/reminders").text
    assert "Пётр Без Телеграма" in page and "нет Telegram" in page
    assert page.count("disabled") >= 1


def test_a_one_off_date_is_shown_instead_of_the_days(client):
    day = (dt.datetime.now(reminders.zone()).date() + dt.timedelta(days=3)).isoformat()
    client.post("/reminders", data=form(once_date=day, weekdays=[]))
    page = client.get("/reminders").text
    assert "day on once" in page


def test_what_the_director_already_did_is_shown_on_the_card(client):
    client.post("/reminders", data=form())
    people = ids()
    with SessionLocal() as s:
        row = s.get(MeetingAlert, 1)
        today = dt.datetime.now(reminders.zone()).date().isoformat()
        row.skips = [{"date": today, "employee_id": people["Анна Орлова"], "by": 5}]
        s.commit()
    assert "Директор уже напомнил: Анна Орлова" in client.get("/reminders").text


def test_the_unknown_alert_gives_a_message_not_an_error(client):
    assert message(client.post("/reminders/99/toggle", follow_redirects=False)) == "Напоминание не найдено"
    assert client.post("/reminders/99/delete", follow_redirects=False).status_code == 303


def test_only_superusers_may_use_the_page():
    accounts.register("ivan", "correct-horse")
    with SessionLocal() as s:
        s.query(User).filter_by(username="ivan").one().status = "approved"
        s.commit()
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "ivan", "password": "correct-horse"})
        assert c.get("/reminders").status_code == 403
        assert c.post("/reminders", data=form()).status_code == 403
        assert c.post("/reminders/1/toggle").status_code == 403
        assert 'href="/reminders"' not in c.get("/app").text
    with TestClient(app) as anonymous:
        assert anonymous.get("/reminders", follow_redirects=False).status_code == 303


def test_the_alert_stylesheet_is_served():
    from app.main import app
    with TestClient(app) as c:
        html = c.get("/login").text
        href = [part for part in html.split('"') if part.startswith("/static/style.css")][0]
        assert ".rm-card" in c.get(href).text
