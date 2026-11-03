"""№26 «Календарь»: the deadlines of the tasks and the regular meetings go to the calendars of the people by secret ICS
addresses; the company calendar (its secret ICS address) gives the recordings their participants — the people invited
to the meeting held at that time, matched by the work e-mail or the name; else the reminders are asked as before."""
import datetime as dt
import re

import pytest

from app import calendars, config, participants, reminders, settings_store, userbot
from app.db import CalendarEvent, Employee, Meeting, MeetingAlert, SessionLocal, Task

MONDAY = dt.date(2026, 3, 2)

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
BEGIN:VTIMEZONE
TZID:Custom Zone
BEGIN:STANDARD
DTSTART:16010101T000000
TZOFFSETFROM:+0300
TZOFFSETTO:+0300
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:plan@google.com
SUMMARY:Планёрка\\, отдел продаж
DTSTART;TZID=Europe/Minsk:20260302T100000
DTEND;TZID=Europe/Minsk:20260302T110000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=6
EXDATE;TZID=Europe/Minsk:20260304T100000
ORGANIZER;CN="Петров, Иван":mailto:boss@firm.by
ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;CN=Anna Smirnova;X-NUM-GUESTS=0:mailto:an
 na@firm.by
ATTENDEE;CN=Борис Котов;PARTSTAT=DECLINED:mailto:boris@firm.by
ATTENDEE;CUTYPE=ROOM;CN=Переговорная:mailto:room@resource.calendar.google.com
ATTENDEE;CN=Иван Подрядчиков;PARTSTAT=NEEDS-ACTION:mailto:ivan@contractor.by
BEGIN:VALARM
TRIGGER:-PT10M
ACTION:DISPLAY
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:plan@google.com
RECURRENCE-ID;TZID=Europe/Minsk:20260309T100000
SUMMARY:Планёрка (перенос)
DTSTART;TZID=Europe/Minsk:20260309T120000
DTEND;TZID=Europe/Minsk:20260309T130000
END:VEVENT
BEGIN:VEVENT
UID:outlook-1
SUMMARY:Разбор с подрядчиком
DTSTART;TZID=Russian Standard Time:20260302T150000
DURATION:PT30M
ATTENDEE;CN=v.ivanova@firm.by:mailto:v.ivanova@firm.by
END:VEVENT
BEGIN:VEVENT
UID:custom-1
SUMMARY:Своя зона
DTSTART;TZID=Custom Zone:20260303T150000
END:VEVENT
BEGIN:VEVENT
UID:monthly-1
SUMMARY:Совет директоров
DTSTART:20260105T070000Z
RRULE:FREQ=MONTHLY;BYDAY=1MO;UNTIL=20260501T000000Z
END:VEVENT
BEGIN:VEVENT
UID:cancelled-1
SUMMARY:Отменено
STATUS:CANCELLED
DTSTART:20260302T080000Z
END:VEVENT
BEGIN:VEVENT
UID:allday-1
SUMMARY:Отпуск
DTSTART;VALUE=DATE:20260310
DTEND;VALUE=DATE:20260312
END:VEVENT
END:VCALENDAR
"""


def staff(*people):
    with SessionLocal() as s:
        rows = [Employee(name=name, email=email, telegram_chat_id=chat, position="инженер") for name, email, chat in people]
        s.add_all(rows)
        s.commit()
        return [row.id for row in rows]


def window():
    return dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)


def test_an_ics_file_is_read_with_its_recurrences_zones_and_people():
    found = calendars.occurrences(calendars.parse(ICS), *window())
    plan = [(o["start"].strftime("%d.%m %H:%M"), o["title"]) for o in found if o["uid"] == "plan@google.com"]
    # COUNT=6 from 02.03: the 04.03 one is excluded, the 09.03 one moved to 12:00 and keeps the people of the series
    assert plan == [("02.03 10:00", "Планёрка, отдел продаж"), ("09.03 12:00", "Планёрка (перенос)"),
                    ("11.03 10:00", "Планёрка, отдел продаж"), ("16.03 10:00", "Планёрка, отдел продаж"),
                    ("18.03 10:00", "Планёрка, отдел продаж")]
    first = next(o for o in found if o["uid"] == "plan@google.com")
    assert first["organizer"] == {"name": "Петров, Иван", "email": "boss@firm.by", "status": "", "kind": "INDIVIDUAL"}
    assert [(a["name"], a["email"], a["status"]) for a in first["attendees"]][:2] == [
        ("Anna Smirnova", "anna@firm.by", "ACCEPTED"), ("Борис Котов", "boris@firm.by", "DECLINED")]
    moved = next(o for o in found if o["title"] == "Планёрка (перенос)")
    assert moved["attendees"] == first["attendees"]
    outlook = next(o for o in found if o["uid"] == "outlook-1")
    assert outlook["start"].utcoffset() == dt.timedelta(hours=3) and outlook["end"] - outlook["start"] == dt.timedelta(minutes=30)
    assert outlook["attendees"][0]["name"] == ""  # the address written as the name is not a name
    custom = next(o for o in found if o["uid"] == "custom-1")
    assert custom["start"].utcoffset() == dt.timedelta(hours=3) and custom["end"] - custom["start"] == dt.timedelta(hours=1)
    board = [o["start"].strftime("%d.%m") for o in found if o["uid"] == "monthly-1"]
    assert board == ["05.01", "02.02", "02.03", "06.04"]  # the first Monday, until May
    assert not any(o["uid"] == "cancelled-1" for o in found)
    holiday = next(o for o in found if o["uid"] == "allday-1")
    assert holiday["all_day"] and holiday["start"] == dt.date(2026, 3, 10)


def test_recurrence_rules_daily_interval_until_and_yearly():
    text = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:d
DTSTART:20260302T090000Z
RRULE:FREQ=DAILY;INTERVAL=2;UNTIL=20260310T090000Z
END:VEVENT
BEGIN:VEVENT
UID:w
DTSTART;TZID=Europe/Berlin:20260320T090000
RRULE:FREQ=WEEKLY;INTERVAL=2
END:VEVENT
BEGIN:VEVENT
UID:y
DTSTART;VALUE=DATE:20250315
RRULE:FREQ=YEARLY
END:VEVENT
BEGIN:VEVENT
UID:m
DTSTART:20260131T100000Z
RRULE:FREQ=MONTHLY;BYMONTHDAY=-1;COUNT=3
END:VEVENT
END:VCALENDAR"""
    found = calendars.occurrences(calendars.parse(text), *window())
    days = lambda uid: [o["start"].strftime("%d.%m") for o in found if o["uid"] == uid]  # noqa: E731
    assert days("d") == ["02.03", "04.03", "06.03", "08.03", "10.03"]
    assert days("w")[:3] == ["20.03", "03.04", "17.04"]
    berlin = [o["start"] for o in found if o["uid"] == "w"]
    assert berlin[0].utcoffset() == dt.timedelta(hours=1) and berlin[1].utcoffset() == dt.timedelta(hours=2)  # summer time
    assert days("y") == ["15.03"]
    assert days("m") == ["31.01", "28.02", "31.03"]


def test_sync_keeps_the_timed_meetings_and_the_state(monkeypatch):
    monkeypatch.setattr(calendars, "now", lambda: dt.datetime(2026, 3, 5, 12, 0, tzinfo=dt.timezone.utc))
    seen = []
    monkeypatch.setattr(calendars, "fetch", lambda url: seen.append(url) or ICS)
    calendars.save_address("webcal://calendar.google.com/calendar/ical/x/private-1/basic.ics", by="admin")
    assert settings_store.get("calendar_ics_url") != "" and "google" not in settings_store.get("calendar_ics_url")
    state = calendars.sync()
    assert seen == ["https://calendar.google.com/calendar/ical/x/private-1/basic.ics"]
    with SessionLocal() as s:
        rows = s.query(CalendarEvent).order_by(CalendarEvent.starts_at).all()
        titles = [r.title for r in rows]
        assert "Отпуск" not in titles and "Отменено" not in titles and titles.count("Планёрка, отдел продаж") == 4
        room = [p for r in rows for p in r.attendees if p["kind"] == "ROOM"]
        assert not room
    assert state["ok"] and state["events"] == len(rows) and state["with_people"] >= 5
    with pytest.raises(calendars.CalendarError):
        calendars.save_address("calendar.google.com/basic.ics")


def test_an_error_is_kept_and_retried_later(monkeypatch):
    calendars.save_address("https://example.com/cal.ics")

    def broken(url):
        raise calendars.CalendarError("Календарь не открылся (404)")
    monkeypatch.setattr(calendars, "fetch", broken)
    with pytest.raises(calendars.CalendarError):
        calendars.sync()
    assert calendars.status()["ok"] is False and "404" in calendars.status()["error"]
    calls = []
    monkeypatch.setattr(calendars, "sync", lambda: calls.append(1))
    calendars.run_due()
    assert calls == []  # tried again in an hour, not every loop


def _load(monkeypatch, text=ICS):
    monkeypatch.setattr(calendars, "fetch", lambda url: text)
    monkeypatch.setattr(calendars, "now", lambda: dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.timezone.utc))
    calendars.save_address("https://calendar.example/basic.ics")
    calendars.sync()


def recording(title="Запись TicNote", day=MONDAY, at=None):
    options = {"recorded_at": at.astimezone(dt.timezone.utc).isoformat()} if at else {}
    with SessionLocal() as s:
        row = Meeting(title=title, filename="rec.wav", source="ticnote", status="awaiting_approval", meeting_date=day,
                      options=options, duration_sec=120)
        s.add(row)
        s.commit()
        return row.id


def local(day, hhmm):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=reminders.zone())


def ensure(mid):
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        done = participants.ensure(s, m)
        s.commit()
        return done, participants.view(s, m)


def test_a_recording_takes_the_invited_of_the_calendar_before_the_reminders(monkeypatch):
    boss, anna, boris, vera = staff(("Иван Петров", "boss@firm.by", ""), ("Анна Смирнова", "", ""),
                                    ("Борис Котов", "boris@firm.by", ""), ("Вера Иванова", "", ""))
    with SessionLocal() as s:
        s.add(MeetingAlert(title="Планёрка", meet_time="10:00", weekdays=[0], recipients=[vera]))
        s.commit()
    _load(monkeypatch)
    done, view = ensure(recording(at=local(MONDAY, "10:05")))
    # the organizer by the e-mail, Anna by the name in Latin letters; Boris declined; the room is not a person
    assert done and view["ids"] == [boss, anna] and view["guests"] == ["Иван Подрядчиков"]
    assert view["origin"] == "Из календаря: «Планёрка, отдел продаж» 02.03 10:00."
    # 15:00 the same day: the meeting with the contractor; v.ivanova@ is Vera by the e-mail written as a name
    done, view = ensure(recording(at=local(MONDAY, "15:10")))
    assert done and view["ids"] == [vera] and view["origin"].startswith("Из календаря: «Разбор с подрядчиком»")


def test_without_an_employee_among_the_invited_the_reminder_is_used(monkeypatch):
    (vera,) = staff(("Вера Иванова", "", ""))
    with SessionLocal() as s:
        s.add(MeetingAlert(title="Совет", meet_time="10:00", weekdays=[0], recipients=[vera]))
        s.commit()
    _load(monkeypatch)
    done, view = ensure(recording(at=local(MONDAY, "10:00")))
    assert done and view["ids"] == [vera] and view["origin"] == "Из напоминания «Совет»."


def test_a_recording_far_from_any_meeting_gets_nothing_from_the_calendar(monkeypatch):
    staff(("Иван Петров", "boss@firm.by", ""))
    _load(monkeypatch)
    done, view = ensure(recording(at=local(MONDAY, "19:30")))
    assert not done and not view["set"]
    # with no time known and two meetings that day, only the title can tell
    done, view = ensure(recording(title="Планёрка, отдел продаж — запись", at=None))
    assert done and view["origin"].startswith("Из календаря")


def test_the_meeting_page_offers_the_calendar_and_marks_where_the_list_came_from(client, monkeypatch):
    boss, = staff(("Иван Петров", "boss@firm.by", ""))
    _load(monkeypatch)
    mid = recording(at=local(MONDAY, "10:01"))
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        participants.set_people(s, m, [], ["Кто-то"], by="admin")
        s.commit()
    assert "Взять из календаря «Планёрка, отдел продаж»" in client.get(f"/meetings/{mid}").text
    answer = client.post(f"/meetings/{mid}/participants", data={"action": "alert"}, follow_redirects=False)
    assert answer.headers["location"].endswith("people=calendar#meeting-participants")
    page = client.get(f"/meetings/{mid}?people=calendar").text
    assert "Участники взяты из календаря" in page and "Из календаря: «Планёрка, отдел продаж»" in page
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        participants.set_people(s, m, [boss], [], by="admin")
        s.commit()
        assert participants.view(s, m)["origin"] == "Исправлено вручную · admin. Сначала было из календаря: «Планёрка, отдел продаж»."


def test_name_matching_by_mail_name_and_latin_letters():
    rows = [Employee(id=1, name="Алексей Кузнецов", aliases="Лёша", email=""),
            Employee(id=2, name="Юрий Новиков", aliases="", email="yn@firm.by"),
            Employee(id=3, name="Анна Смирнова", aliases="", email=""), Employee(id=4, name="Анна Петрова", email="")]
    hit = lambda **p: (calendars.match_person({"name": "", "email": "", **p}, rows) or Employee(id=0)).id  # noqa: E731
    assert hit(email="YN@firm.by") == 2
    assert hit(name="Aleksei Kuznetsov") == 1 and hit(name="Alexey Kuznetsov") == 1
    assert hit(email="a.kuznetsov@firm.by") == 1
    assert hit(name="Yuri Novikov") == 2 and hit(name="Юрий") == 2
    assert hit(name="Анна") == 0  # two Annas: never guessed
    assert hit(name="Anna Smirnova") == 3 and hit(name="Петр Сидоров") == 0


def _alert(title="Планёрка", weekdays=(0, 2), recipients=(), at="10:00", once=None):
    with SessionLocal() as s:
        row = MeetingAlert(title=title, meet_time=at, weekdays=list(weekdays), once_date=once, recipients=list(recipients),
                           details="Переговорная 2", created_at=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc))
        s.add(row)
        s.commit()
        return row.id


def _task(title, who, deadline, status="sent", **extra):
    with SessionLocal() as s:
        row = Task(title=title, assignee_id=who, deadline=deadline, status=status, **{"assignee_name": "", **extra})
        s.add(row)
        s.commit()
        return row.id


def test_the_personal_address_has_the_deadlines_and_the_regular_meetings(client):
    anna, boris = staff(("Анна Смирнова", "", ""), ("Борис Котов", "", ""))
    soon = dt.date.today() + dt.timedelta(days=3)
    mine = _task("Сдать отчёт; по объекту, «Криница»", anna, soon, description="Длинное описание " * 12)
    _task("Чужая задача", boris, soon)
    _task("Черновик", anna, soon, status="draft")
    done = _task("Сделанное", anna, dt.date.today() - dt.timedelta(days=2), status="done")
    _task("Давнее сделанное", anna, dt.date.today() - dt.timedelta(days=60), status="done")
    _alert(recipients=[anna])
    _alert(title="Без Анны", recipients=[boris])
    url = calendars.link(anna, "http://testserver")
    assert re.fullmatch(r"http://testserver/calendar/feed/[0-9a-f]{32}\.ics", url)
    answer = client.get(url.replace("http://testserver", ""))
    assert answer.status_code == 200 and answer.headers["content-type"].startswith("text/calendar")
    body = answer.text
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
    flat = body.replace("\r\n ", "")
    assert f"UID:task-{mine}@sysai" in flat and f"DTSTART;VALUE=DATE:{soon:%Y%m%d}" in flat
    assert "SUMMARY:Срок: Сдать отчёт\\; по объекту\\, «Криница»" in flat
    assert f"UID:task-{done}@sysai" in flat and "SUMMARY:✓ Срок: Сделанное" in flat
    assert "Чужая задача" not in flat and "Черновик" not in flat and "Давнее сделанное" not in flat
    assert "SUMMARY:Планёрка" in flat and "RRULE:FREQ=WEEKLY;BYDAY=MO,WE" in flat and "Без Анны" not in flat
    assert "DTSTART;TZID=Europe/Minsk:20260202T100000" in flat and "BEGIN:VTIMEZONE" in flat and "TZID:Europe/Minsk" in flat
    assert "Участники: Анна Смирнова" in flat and "Переговорная 2" in flat
    # the parser of the app reads it back: the alert repeats on Mondays and Wednesdays at 10:00 Minsk time
    back = calendars.occurrences(calendars.parse(body), dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc),
                                 dt.datetime(2026, 2, 10, tzinfo=dt.timezone.utc))
    assert [o["start"].strftime("%d.%m %H:%M") for o in back if o["title"] == "Планёрка"] == ["02.02 10:00", "04.02 10:00", "09.02 10:00"]


def test_the_director_address_has_everything_and_links_can_be_changed(client):
    anna, boris = staff(("Анна Смирнова", "", ""), ("Борис Котов", "", ""))
    soon = dt.date.today() + dt.timedelta(days=1)
    _task("Задача Анны", anna, soon, assignee_name="Анна")
    _task("Задача Бориса", boris, soon)
    _alert(title="Без Анны", recipients=[boris])
    path = calendars.link("all").split("testserver")[-1]
    path = path[path.index("/calendar/"):]
    flat = client.get(path).text.replace("\r\n ", "")
    assert "Срок: Задача Анны — Анна Смирнова" in flat and "Срок: Задача Бориса — Борис Котов" in flat and "Без Анны" in flat
    old = calendars.link(anna)
    old = old[old.index("/calendar/"):]
    calendars.reset_links()
    assert client.get(old).status_code == 404 and client.get(path).status_code == 404
    new = calendars.link(anna)
    assert client.get(new[new.index("/calendar/"):]).status_code == 200
    settings_store.set_many({"calendar_feed_on": False})
    assert client.get(new[new.index("/calendar/"):]).status_code == 404
    assert client.get("/calendar/feed/" + "0" * 32 + ".ics").status_code == 404


def test_an_inactive_employee_has_no_address(client):
    (gone,) = staff(("Ушедший Сотрудник", "", ""))
    path = calendars.link(gone)
    path = path[path.index("/calendar/"):]
    assert client.get(path).status_code == 200
    with SessionLocal() as s:
        s.get(Employee, gone).active = False
        s.commit()
    assert client.get(path).status_code == 404


def test_the_page_shows_the_links_and_keeps_the_address_secret(client, monkeypatch):
    staff(("Анна Смирнова", "", "@anna"), ("Борис Котов", "boris@firm.by", ""))
    page = client.get("/calendar").text
    assert "Сроки и совещания в календаре сотрудников" in page and "/calendar/feed/" in page and "нет Telegram" in page
    assert "не подключён" in page and "сотрудников без рабочей почты" in page
    monkeypatch.setattr(calendars, "fetch", lambda url: ICS)
    answer = client.post("/calendar/address", data={"url": "https://calendar.google.com/x/private-SECRET/basic.ics"},
                         follow_redirects=False)
    assert answer.headers["location"] == "/calendar?msg=saved#company"
    page = client.get("/calendar").text
    assert "private-SECRET" not in page and "подключён" in page and "прочитан" in page
    assert client.post("/calendar/address", data={"url": "ftp://x"}, follow_redirects=False).headers["location"].startswith("/calendar?error=")
    assert "private-SECRET" in calendars.address()  # an empty or wrong field keeps the saved one
    client.post("/calendar/forget")
    assert not calendars.configured()
    with SessionLocal() as s:
        assert s.query(CalendarEvent).count() == 0


def test_links_are_sent_to_those_with_telegram(client, monkeypatch):
    staff(("Анна Смирнова", "", "@anna"), ("Борис Котов", "", ""))
    sent = []
    monkeypatch.setattr(userbot, "ai_mode", lambda: True)
    monkeypatch.setattr(userbot, "send_message", lambda ref, text, **kw: sent.append((ref, text)) or {"message_id": 1})
    answer = client.post("/calendar/send-links", follow_redirects=False)
    assert "Ссылки отправлены: 1." in __import__("urllib.parse").parse.unquote(answer.headers["location"])
    assert sent[0][0] == "@anna" and "/calendar/feed/" in sent[0][1] and "Добавить по URL" in sent[0][1]


def test_the_employee_card_keeps_the_work_mail(client):
    (anna,) = staff(("Анна Смирнова", "", ""))
    form = {"id": anna, "name": "Анна Смирнова", "email": " Anna@Firm.BY ", "active": "on"}
    assert client.post("/employees", data=form, follow_redirects=False).status_code in (200, 303)
    with SessionLocal() as s:
        assert s.get(Employee, anna).email == "anna@firm.by"
    bad = client.post("/employees", data={**form, "email": "не почта"}, headers={"x-requested-with": "fetch"})
    assert bad.status_code == 400 and "Рабочая почта" in bad.json()["error"]
    # the quick form of the org chart has no such field: the mail stays
    client.post("/employees", data={"id": anna, "name": "Анна Смирнова", "active": "on"})
    with SessionLocal() as s:
        assert s.get(Employee, anna).email == "anna@firm.by"


def test_a_task_knows_when_it_was_done():
    (anna,) = staff(("Анна Смирнова", "", ""))
    tid = _task("Отчёт", anna, dt.date.today())
    with SessionLocal() as s:
        t = s.get(Task, tid)
        assert t.created_at is not None and t.done_at is None
        t.status = "done"
        s.commit()
        assert s.get(Task, tid).done_at is not None
        t.status = "in_progress"
        s.commit()
        assert s.get(Task, tid).done_at is None
