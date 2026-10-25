"""Sprint 11.6–11.7: the case summary for the reviewers (daily / weekly) and the case feed in the panel."""
import datetime as dt
from zoneinfo import ZoneInfo

from app import cases, digest, progress, settings_store
from app.db import Case, CaseEvent, SessionLocal, now
from test_ai_mode import FakeBackend, tgacc  # noqa: F401
from test_cases import CaseModel, cmodel, doc, incoming, make_task  # noqa: F401
from test_delivery_cases import JPG, order, send_photos, team  # noqa: F401

MINSK = ZoneInfo("Europe/Minsk")


def at(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=MINSK).astimezone(dt.timezone.utc)


def test_daily_due_once_a_day_after_the_hour():
    settings_store.set_many({"digest_frequency": "daily", "digest_hour": 9, "digest_last_sent": ""})
    assert not digest.due(at(2026, 10, 26, 8, 59))
    assert digest.due(at(2026, 10, 26, 9, 0))
    settings_store.set_many({"digest_last_sent": "2026-10-26"})
    assert not digest.due(at(2026, 10, 26, 15))
    assert digest.due(at(2026, 10, 27, 9, 30))


def test_weekly_due_on_the_chosen_day_only():
    settings_store.set_many({"digest_frequency": "weekly", "digest_hour": 10, "digest_weekday": 0, "digest_last_sent": ""})
    assert not digest.due(at(2026, 10, 25, 12))      # Sunday
    assert digest.due(at(2026, 10, 26, 10))          # Monday 10:00
    settings_store.set_many({"digest_last_sent": "2026-W44"})
    assert not digest.due(at(2026, 10, 26, 18))      # already sent this week
    settings_store.set_many({"digest_frequency": "off"})
    assert not digest.due(at(2026, 11, 2, 10))


def test_summary_lists_closed_waiting_and_decisions(team, tgacc, cmodel):
    send_photos()
    order(cmodel, team)                              # a delivery: done
    task_id = make_task({"vadim": team["vadim"]}, kind="ask", counterparts=(("recipient", "vadim"),), target="vadim",
                        title="Спросить у Вадима сроки")
    from app import assistant
    assistant.run_task(task_id)                      # waiting for Вадим's answer
    with SessionLocal() as s:
        s.add(Case(title="Акт от подрядчика", kind="request", status="attention", note="нужно разрешение на пересылку"))
        s.commit()
    text = digest.build("daily")
    assert text.startswith("Сводка по делам за сутки")
    assert "✅ Завершено: 1" in text and "Скриншоты для Вадима" in text
    assert "⚠️ Нужно ваше решение: 1" in text and "нужно разрешение на пересылку" in text
    assert "Спросить у Вадима сроки — ждём ответ от Вадим" in text


def test_stale_cases_are_marked(team, tgacc, cmodel):
    from app import assistant
    task_id = make_task({"vadim": team["vadim"]}, kind="ask", counterparts=(("recipient", "vadim"),), target="vadim",
                        title="Спросить у Вадима сроки")
    assistant.run_task(task_id)
    text = digest.build("daily", moment=now() + dt.timedelta(hours=30))
    assert "(без движения 30 ч)" in text


def test_send_goes_to_reviewers_only_and_marks_the_period(team, tgacc, cmodel):
    settings_store.set_many({"approver_chat_ids": ["@omg"], "observer_chat_ids": ["@obs"], "director_chat_ids": ["@boss"],
                             "digest_frequency": "daily", "digest_last_sent": ""})
    tgacc.users.update({"@obs": 70, "@boss": 7})
    assert digest.send() == 1
    targets = [r for r, t in tgacc.sent if t.startswith("Сводка по делам")]
    assert targets == ["@omg"]
    assert settings_store.get("digest_last_sent") == now().astimezone(MINSK).date().isoformat()


def test_run_due_sends_once(team, tgacc, cmodel, monkeypatch):
    settings_store.set_many({"digest_frequency": "daily", "digest_hour": 0, "digest_last_sent": ""})
    digest.run_due()
    digest.run_due()
    assert sum(t.startswith("Сводка по делам") for _, t in tgacc.sent) == 1


def test_settings_page_and_send_now(client, team, tgacc, cmodel):
    html = client.get("/settings").text
    assert 'name="digest_frequency"' in html and "Раз в неделю" in html and 'formaction="/settings/test-digest"' in html
    client.post("/settings", data={"digest_frequency": "weekly", "digest_hour": "8", "digest_weekday": "4"})
    s = settings_store.all_settings()
    assert (s["digest_frequency"], s["digest_hour"], s["digest_weekday"]) == ("weekly", 8, 4)
    assert client.post("/settings", data={"digest_frequency": "hourly"}).status_code == 400
    assert client.post("/settings", data={"digest_hour": "25"}).status_code == 400
    page = client.post("/settings/test-digest").text
    assert "Сводка отправлена проверяющим: 1 из 1" in page
    assert settings_store.get("digest_last_sent") == ""  # «прислать сейчас» does not move the schedule


# ----------------------------------------------------------------------------- the feed
def test_feed_shows_chat_orders_with_steps_and_the_sent_archive(client, team, tgacc, cmodel):
    send_photos()
    order(cmodel, team)
    with SessionLocal() as s:
        data = progress.build(s, "chat")
    card = data["groups"][0][1][0]
    assert card["from_chat"] and card["requested_by"] and card["delivered"][0]["archive"] == "Скриншоты для Вадима.zip"
    assert card["delivered"][0]["artifact_id"] and len(card["delivered"][0]["files"]) == 4
    assert "упаковать 4 файл(ов) в ZIP" in card["plan"]
    assert card["events"][-1]["at"].tzinfo is not None and str(card["events"][-1]["at"].tzinfo) == "Europe/Minsk"
    html = client.get("/progress?scope=chat").text
    assert "Поручения из чата" in html and "Скриншоты для Вадима.zip" in html and "/artifacts/" in html
    zip_link = html.split('href="/artifacts/')[1].split('"')[0]
    assert client.get(f"/artifacts/{zip_link}").content[:2] == b"PK"
