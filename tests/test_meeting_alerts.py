"""Meeting reminders set in the panel: time, switch, people. And how they give way when the director reminds by hand."""
import datetime as dt

import pytest

from app import agent, meeting_alerts, reminders, settings_store, userbot
from app.db import Employee, MeetingAlert, Reminder, SessionLocal
from test_ai_mode import incoming, make_meeting, tgacc  # noqa: F401  (fixture)
from test_voice_reminders import director, everyone, model, reminder_rows, remind  # noqa: F401  (fixture)

ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]


CLOCK_TIMES = ["00:05", "09:20", "16:59", "17:10", "23:45"]       # the service clock, frozen at these local times of day


class Clock:
    """The service clock: frozen at a local time of today and moved by hand. The alerts, the reminders and the agent all read
    it, so every scenario runs the same at any time of day, including just before midnight (a meeting after midnight)."""

    def __init__(self, monkeypatch, at):
        zone = reminders.zone()
        hour, minute = (int(x) for x in at.split(":"))
        today = dt.datetime.now(zone).date()
        self.base = dt.datetime.combine(today, dt.time(hour, minute), tzinfo=zone).astimezone(dt.timezone.utc)
        self.shift = dt.timedelta(0)
        for module in (meeting_alerts, reminders, agent):
            monkeypatch.setattr(module, "now", self)

    def __call__(self):
        return self.base + self.shift

    def go(self, minutes):
        self.shift = dt.timedelta(minutes=minutes)

    def local(self, minutes=0):
        return (self() + dt.timedelta(minutes=minutes)).astimezone(reminders.zone())

    def at(self, hours):
        """A time for the model to put into a reminder ("ГГГГ-ММ-ДД ЧЧ:ММ" in the service time zone)."""
        return self.local(hours * 60).strftime("%Y-%m-%d %H:%M")


@pytest.fixture(params=CLOCK_TIMES, ids=lambda at: f"at{at.replace(':', '')}")
def clock(monkeypatch, request):
    return Clock(monkeypatch, request.param)


def people():
    with SessionLocal() as s:
        return {e.name: e.id for e in s.query(Employee)}


def make_alert(clock, *, meeting_in=40, lead=15, who=("Иван Петров", "Анна Орлова"), days=ALL_DAYS, title="Планёрка",
               details="Переговорная 2", once=False, **extra):
    ids = people()
    meeting = clock.local(meeting_in)
    values = {"title": title, "details": details, "meet_time": meeting.strftime("%H:%M"), "lead_minutes": lead,
              "weekdays": [str(d) for d in days], "recipients": [str(ids[n]) for n in who],
              "once_date": meeting.date().isoformat() if once else ""}
    values.update(extra)
    return meeting_alerts.save(values)


def alert(alert_id=1):
    with SessionLocal() as s:
        row = s.get(MeetingAlert, alert_id)
        return {"enabled": row.enabled, "fired": row.last_fired_on, "result": row.last_result, "skips": list(row.skips or [])}


def go_and_run(clock, minutes):
    clock.go(minutes)
    meeting_alerts.run_due()
    reminders.run_due()


def texts_to(tgacc, ref):
    return [text for r, text in tgacc.sent if r == ref]


def remind_meeting(to, **kwargs):
    decision = remind(to, **kwargs)
    decision["actions"][0]["meeting"] = True
    return decision


# ----------------------------------------------------------------------------- the form
def test_the_form_becomes_clean_values():
    data = meeting_alerts.clean({"title": "  Планёрка ", "meet_time": "9:05", "lead_minutes": "30", "weekdays": ["4", "0", "0"],
                                 "recipients": ["3", "x", "3", "5"], "details": " ждём "})
    assert data == {"title": "Планёрка", "details": "ждём", "meet_time": "09:05", "lead_minutes": 30, "weekdays": [0, 4],
                    "once_date": None, "recipients": [3, 5]}


@pytest.mark.parametrize("values, fragment", [
    ({"meet_time": "", "weekdays": ["0"]}, "время совещания"),
    ({"meet_time": "25:00", "weekdays": ["0"]}, "время совещания"),
    ({"meet_time": "10:00", "weekdays": []}, "дни недели или одну дату"),
    ({"meet_time": "10:00", "weekdays": ["7"]}, "день недели"),
    ({"meet_time": "10:00", "weekdays": ["0"], "lead_minutes": "много"}, "целое число"),
    ({"meet_time": "10:00", "weekdays": ["0"], "lead_minutes": "9999"}, "минут"),
    ({"meet_time": "10:00", "once_date": "завтра"}, "ГГГГ-ММ-ДД"),
])
def test_a_form_that_cannot_be_used_is_explained(values, fragment):
    with pytest.raises(meeting_alerts.AlertError, match=fragment):
        meeting_alerts.clean(values)


def test_nobody_to_remind_is_an_error(clock):
    make_meeting()
    with pytest.raises(meeting_alerts.AlertError, match="кому напоминать"):
        meeting_alerts.save({"meet_time": "10:00", "weekdays": ["0"], "recipients": ["9999"]})


def test_a_date_that_has_passed_is_refused(clock):
    make_meeting()
    yesterday = (clock.local().date() - dt.timedelta(days=1)).isoformat()
    with pytest.raises(meeting_alerts.AlertError, match="уже прошло"):
        meeting_alerts.save({"meet_time": "10:00", "once_date": yesterday, "recipients": [str(i) for i in people().values()]})


def test_a_date_replaces_the_weekdays(clock):
    make_meeting()
    day = (clock.local().date() + dt.timedelta(days=2)).isoformat()
    meeting_alerts.save({"meet_time": "10:00", "weekdays": ["1", "2"], "once_date": day, "recipients": [str(i) for i in people().values()]})
    with SessionLocal() as s:
        row = s.query(MeetingAlert).one()
        assert row.weekdays == [] and row.once_date.isoformat() == day


# ----------------------------------------------------------------------------- the minute it fires
def test_it_reminds_the_chosen_people_at_the_right_minute(tgacc, model, clock):
    make_meeting()
    make_alert(clock, meeting_in=40, lead=15)
    go_and_run(clock, 20)
    assert not any(ref in ("@ivan", "@anna") for ref, _ in tgacc.sent)                  # too early
    go_and_run(clock, 27)
    ivan = texts_to(tgacc, "@ivan")
    assert len(ivan) == 1 and ivan[0].startswith("[reminder:Иван Петров] Планёрка | начало в ")
    assert "Переговорная 2" in ivan[0] and "за 15 мин" in ivan[0]
    assert len(texts_to(tgacc, "@anna")) == 1
    assert alert()["fired"] == clock().astimezone(reminders.zone()).date() and "отправлено: Анна Орлова, Иван Петров" in alert()["result"]
    assert reminder_rows()[0][1] == "sent"


def test_it_does_not_repeat_itself(tgacc, model, clock):
    make_meeting()
    make_alert(clock)
    for minutes in (27, 28, 29, 35):
        go_and_run(clock, minutes)
    assert len(texts_to(tgacc, "@ivan")) == 1 and len(reminder_rows()) == 1


def test_only_the_chosen_people_are_reminded(tgacc, model, clock):
    make_meeting()
    make_alert(clock, who=("Анна Орлова",))
    go_and_run(clock, 27)
    assert texts_to(tgacc, "@ivan") == [] and len(texts_to(tgacc, "@anna")) == 1


def test_a_switched_off_alert_stays_silent(tgacc, model, clock):
    make_meeting()
    make_alert(clock)
    assert meeting_alerts.toggle(1) is False
    go_and_run(clock, 27)
    assert not any(ref in ("@ivan", "@anna") for ref, _ in tgacc.sent) and reminder_rows() == []


def test_switched_on_again_it_starts_with_the_next_occurrence_not_with_a_missed_one(tgacc, model, clock):
    make_meeting()
    make_alert(clock)
    meeting_alerts.toggle(1)
    clock.go(90)                                      # the meeting is over
    assert meeting_alerts.toggle(1) is True
    meeting_alerts.run_due()
    assert alert()["fired"] is not None and "пропущено" not in alert()["result"]
    assert tgacc.sent == [] and reminder_rows() == []


def test_a_new_alert_whose_time_has_gone_today_is_not_called_missed(tgacc, model, clock):
    make_meeting()
    settings_store.set_many({"director_chat_ids": ["@boss"]})
    make_alert(clock, meeting_in=-30, lead=15)
    meeting_alerts.run_due()
    reminders.run_due()
    assert tgacc.sent == [] and reminder_rows() == []


def test_only_the_chosen_weekdays_count(tgacc, model, clock):
    make_meeting()
    not_that_day = (clock.local(40).weekday() + 1) % 7                    # the meeting itself is on another weekday
    make_alert(clock, days=[not_that_day])
    go_and_run(clock, 27)
    assert tgacc.sent == [] and reminder_rows() == []
    assert meeting_alerts.page_data()["alerts"][0]["next"].startswith("следующее напоминание: ")


def test_a_one_off_alert_fires_and_switches_itself_off(tgacc, model, clock):
    make_meeting()
    make_alert(clock, once=True, days=[])
    go_and_run(clock, 27)
    assert len(texts_to(tgacc, "@ivan")) == 1
    assert alert()["enabled"] is False
    go_and_run(clock, 60)
    assert len(texts_to(tgacc, "@ivan")) == 1


def test_nothing_goes_out_while_the_account_is_away_and_it_goes_when_it_is_back(tgacc, model, clock, monkeypatch):
    make_meeting()
    make_alert(clock)
    connected = userbot.ready
    monkeypatch.setattr(userbot, "ready", lambda: False)
    go_and_run(clock, 27)
    assert reminder_rows() == [] and tgacc.sent == []
    monkeypatch.setattr(userbot, "ready", connected)
    go_and_run(clock, 28)
    assert len(texts_to(tgacc, "@ivan")) == 1


def test_people_without_telegram_are_named_and_skipped(tgacc, model, clock):
    make_meeting()
    with SessionLocal() as s:
        s.add(Employee(name="Пётр Без Телеграма", position="Менеджер"))
        s.commit()
    make_alert(clock, who=("Иван Петров", "Пётр Без Телеграма"))
    view = meeting_alerts.page_data()
    assert [p["name"] for p in view["employees"] if not p["reachable"]] == ["Пётр Без Телеграма"]
    assert view["alerts"][0]["silent"] == ["Пётр Без Телеграма"]
    go_and_run(clock, 27)
    assert len(texts_to(tgacc, "@ivan")) == 1


def test_an_alert_for_people_nobody_can_write_to_says_so(tgacc, model, clock):
    make_meeting()
    with SessionLocal() as s:
        s.add(Employee(name="Пётр Без Телеграма"))
        s.commit()
    make_alert(clock, who=("Пётр Без Телеграма",))
    go_and_run(clock, 27)
    assert "некому писать" in alert()["result"] and reminder_rows() == []


# ----------------------------------------------------------------------------- the service was not running
def test_a_missed_alert_is_reported_to_the_director_and_never_to_the_people(tgacc, model, clock):
    make_meeting()
    settings_store.set_many({"director_chat_ids": ["@boss"]})
    make_alert(clock, meeting_in=90, lead=60)            # due in 30 minutes, the meeting in 90
    go_and_run(clock, 70)                                # the service woke up 40 minutes after the reminder was due
    assert texts_to(tgacc, "@ivan") == [] and texts_to(tgacc, "@anna") == [] and reminder_rows() == []
    told = texts_to(tgacc, "@boss")
    assert len(told) == 1 and told[0].startswith("[missed] Планёрка | late=")
    assert "Иван Петров" in told[0] and "Анна Орлова" in told[0]
    assert "пропущено" in alert()["result"]
    go_and_run(clock, 75)
    assert len(texts_to(tgacc, "@boss")) == 1


def test_an_alert_noticed_after_the_meeting_began_is_missed_too(tgacc, model, clock):
    make_meeting()
    settings_store.set_many({"director_chat_ids": ["@boss"]})
    make_alert(clock, meeting_in=40, lead=0)
    go_and_run(clock, 43)
    assert texts_to(tgacc, "@ivan") == [] and len(texts_to(tgacc, "@boss")) == 1


def test_a_little_lateness_is_forgiven(tgacc, model, clock):
    make_meeting()
    make_alert(clock, meeting_in=90, lead=60)
    go_and_run(clock, 30 + 10)                           # ten minutes after the reminder was due, meeting far away
    assert len(texts_to(tgacc, "@ivan")) == 1


def test_the_reviewers_are_told_when_there_is_no_director(tgacc, model, clock):
    make_meeting()                                       # the account fixture lists @boss as the reviewer
    make_alert(clock, meeting_in=90, lead=60)
    go_and_run(clock, 70)
    assert len(texts_to(tgacc, "@boss")) == 1


# ----------------------------------------------------------------------------- the director reminds by hand
def skips(alert_id=1):
    return [(x["employee_id"], x["by"]) for x in alert(alert_id)["skips"]]


def test_the_directors_reminder_about_a_meeting_takes_the_people_off_the_alert(tgacc, model, clock):
    make_meeting()
    settings_store.set_many({"director_chat_ids": ["@boss"]})
    agent.invalidate_roster()
    make_alert(clock)
    director(model, lambda facts: remind_meeting(everyone(facts)), "напомни всем о совещании")
    ids = people()
    assert sorted(i for i, _ in skips()) == sorted(ids.values())
    reminders.run_due()
    assert len(texts_to(tgacc, "@ivan")) == 1 and len(texts_to(tgacc, "@anna")) == 1      # the director's own reminder went out
    go_and_run(clock, 27)                                                                   # the alert has nothing left to say
    assert len(texts_to(tgacc, "@ivan")) == 1 and len(texts_to(tgacc, "@anna")) == 1
    assert len(reminder_rows()) == 1
    assert "директор уже напомнил всем" in alert()["result"]


def test_only_the_people_the_director_reminded_are_left_out(tgacc, model, clock):
    make_meeting()
    settings_store.set_many({"director_chat_ids": ["@boss"]})
    agent.invalidate_roster()
    make_alert(clock)
    anna = people()["Анна Орлова"]
    director(model, lambda facts: remind_meeting([anna]), "напомни Анне о совещании")
    reminders.run_due()
    go_and_run(clock, 27)
    assert len(texts_to(tgacc, "@anna")) == 1 and len(texts_to(tgacc, "@ivan")) == 1
    assert texts_to(tgacc, "@ivan")[0].startswith("[reminder:Иван Петров]")
    assert "уже напомнил директор: Анна Орлова" in alert()["result"]


def test_a_reminder_that_is_not_about_a_meeting_leaves_the_alert_alone(tgacc, model, clock):
    make_meeting()
    settings_store.set_many({"director_chat_ids": ["@boss"]})
    agent.invalidate_roster()
    make_alert(clock)
    director(model, lambda facts: remind(everyone(facts), topic="Сдать отчёт", details="до конца дня"), "напомни всем про отчёт")
    assert skips() == []
    reminders.run_due()
    go_and_run(clock, 27)
    assert len(texts_to(tgacc, "@ivan")) == 2


def test_cancelling_the_directors_reminder_gives_the_people_back_to_the_alert(tgacc, model, clock):
    make_meeting()
    settings_store.set_many({"director_chat_ids": ["@boss"]})
    agent.invalidate_roster()
    make_alert(clock)
    make_alert(clock, meeting_in=300, lead=15)                           # the meeting is in five hours; the alert in 285 minutes
    director(model, lambda facts: remind_meeting(everyone(facts), at=clock.at(3)), "напомни всем о совещании в три часа")
    assert len(skips(2)) == 2 and skips(1) == []
    director(model, lambda facts: {"reply": "Отменил", "actions": [{"type": "cancel_reminder",
                                                                   "reminder_id": facts["reminders_pending"][0]["id"]}]}, "отмени")
    assert skips(2) == []
    go_and_run(clock, 287)
    assert len(texts_to(tgacc, "@ivan")) == 1 and len(texts_to(tgacc, "@anna")) == 1


def test_the_alert_closest_to_the_directors_time_is_the_one_that_gives_way(tgacc, model, clock):
    make_meeting()
    settings_store.set_many({"director_chat_ids": ["@boss"]})
    agent.invalidate_roster()
    make_alert(clock, meeting_in=40, lead=15, title="Утренняя")
    make_alert(clock, meeting_in=300, lead=15, title="Вечерняя")
    director(model, lambda facts: remind_meeting(everyone(facts), at=clock.at(4.5)), "напомни всем о вечернем совещании")
    assert skips(1) == [] and len(skips(2)) == 2


def test_an_alert_that_has_already_gone_off_is_not_touched(tgacc, model, clock):
    make_meeting()
    settings_store.set_many({"director_chat_ids": ["@boss"]})
    agent.invalidate_roster()
    make_alert(clock)
    go_and_run(clock, 27)
    clock.go(28)
    director(model, lambda facts: remind_meeting(everyone(facts)), "напомни всем о совещании")
    assert skips() == []


def test_a_meeting_that_is_not_within_a_day_of_the_directors_reminder_is_not_claimed(tgacc, model, clock):
    make_meeting()
    settings_store.set_many({"director_chat_ids": ["@boss"]})
    agent.invalidate_roster()
    make_alert(clock, days=[(clock.local(40).weekday() + 3) % 7])         # the next meeting is three days away
    director(model, lambda facts: remind_meeting(everyone(facts)), "напомни всем о совещании")
    assert skips() == []


def test_a_reminder_about_a_meeting_after_midnight_takes_the_people_off_that_nights_alert(tgacc, model, clock):
    make_meeting()
    settings_store.set_many({"director_chat_ids": ["@boss"]})
    agent.invalidate_roster()
    make_alert(clock, meeting_in=40)
    director(model, lambda facts: remind_meeting(everyone(facts)), "напомни всем о совещании")
    assert len(skips()) == 2                                               # at 23:45 the meeting is tomorrow at 00:25


def test_a_claim_is_for_one_date_only(tgacc, model, clock):
    make_meeting()
    settings_store.set_many({"director_chat_ids": ["@boss"]})
    agent.invalidate_roster()
    make_alert(clock)
    director(model, lambda facts: remind_meeting(everyone(facts)), "напомни всем")
    with SessionLocal() as s:
        row = s.get(MeetingAlert, 1)
        assert {x["date"] for x in row.skips} == {clock.local(40).date().isoformat()}     # the date of the meeting, not of the reminder


def test_an_ordinary_employee_reminding_himself_does_not_cancel_the_alert(tgacc, model, clock):
    make_meeting()
    make_alert(clock)
    model.decisions.append(remind_meeting("me", topic="Совещание"))
    agent.handle_incoming(incoming(user_id=42, username="ivan", text="напомни мне о совещании", message_id=11))
    assert reminder_rows()[0][3] == ["42"] and skips() == []


def test_a_meeting_flag_must_be_true_or_false(tgacc, model, clock):
    make_meeting()
    bad = remind(everyone, topic="x")
    bad["actions"][0]["meeting"] = "да"
    ctx = {"team": [{"id": 1, "name": "Иван", "reachable": True}], "reminders_pending": []}
    assert "meeting" in agent._check(bad["actions"][0], ctx, None, True, True)


# ----------------------------------------------------------------------------- the page data
def test_the_page_shows_what_is_next_and_what_the_director_already_did(tgacc, model, clock):
    make_meeting()
    settings_store.set_many({"director_chat_ids": ["@boss"]})
    agent.invalidate_roster()
    make_alert(clock)
    anna = people()["Анна Орлова"]
    director(model, lambda facts: remind_meeting([anna]), "напомни Анне о совещании")
    view = meeting_alerts.page_data(1)
    card = view["alerts"][0]
    day = "сегодня" if clock.local(25).date() == clock.local().date() else "завтра"
    assert card["next"].startswith(f"следующее напоминание: {day} в ") and card["by_director"] == ["Анна Орлова"]
    assert view["edit"]["id"] == 1 and view["edit"]["lead_minutes"] == 15 and len(view["edit"]["recipient_ids"]) == 2
    assert [d["on"] for d in card["days"]] == [True] * 7


def test_the_lead_labels():
    assert [meeting_alerts.lead_label(m) for m in (0, 5, 15, 60, 120, 90)] == [
        "в момент начала", "за 5 мин", "за 15 мин", "за 1 час", "за 2 ч", "за 90 мин"]


def test_reminders_made_by_alerts_have_no_requester_and_no_report(tgacc, model, clock):
    make_meeting()
    make_alert(clock)
    go_and_run(clock, 27)
    with SessionLocal() as s:
        row = s.query(Reminder).one()
        assert row.chat_id == "" and row.requested_by == ""
    assert not any(text.startswith("[report]") for _, text in tgacc.sent)
