"""Voice messages in Telegram and reminders asked of the AI employee ("напомни всем, что сегодня совещание")."""
import datetime as dt
import json
import subprocess

import pytest

from app import agent, llm, reminders, settings_store, transcribe, userbot
from app.db import ConversationMessage, Employee, Reminder, SessionLocal, now
from test_ai_mode import FakeModel, _facts, incoming, make_meeting, tgacc  # noqa: F401  (fixtures)


class RModel(FakeModel):
    """FakeModel plus the two new kinds of text: a reminder to a person and the report to the one who asked."""

    def __call__(self, model_name, messages, **kwargs):
        system = messages[0]["content"]
        if ("Ты напоминаешь человеку" in system or "Ты отчитываешься перед тем, кто просил напомнить" in system
                or "Ты сообщаешь тому, кто просил напомнить" in system):
            facts = _facts(messages)
            self.calls.append({"system": system, "facts": facts, "user": messages[1]["content"]})
            if self.down:
                raise llm.LLMError("модель недоступна")
            if "Ты напоминаешь человеку" in system:
                text = f"[reminder:{facts['recipient']['name']}] {facts['topic']} | {facts['details']}"
            elif "Ты сообщаешь тому, кто просил напомнить" in system:
                text = f"[missed] {facts['topic']} | late={facts['minutes_late']} | for={facts['for']}"
            else:
                text = f"[report] ok={facts['delivered_to']} fail={[f['who'] for f in facts['not_delivered_to']]}"
            return json.dumps({"text": text}, ensure_ascii=False)
        return super().__call__(model_name, messages, **kwargs)


@pytest.fixture
def model(monkeypatch):
    fake = RModel()
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(reminders, "SEND_DELAY", 0)
    agent.invalidate_roster()
    yield fake
    agent.invalidate_roster()


def voice_of(seconds=3, mime="audio/ogg", data=b"OGG"):
    state = {"fetched": 0}

    def fetch():
        state["fetched"] += 1
        return data
    return {"fetch": fetch, "seconds": seconds, "mime": mime}, state


def hear(monkeypatch, text="Иван, ошибки с оплатой закрыть до пятницы"):
    heard = []

    def fake(data, mime="", seconds=0):
        heard.append((data, mime, seconds))
        if isinstance(text, Exception):
            raise text
        return text
    monkeypatch.setattr(transcribe, "voice_text", fake)
    return heard


def say_voice(user_id=42, username="ivan", name="Иван", message_id=7, **kw):
    msg = incoming(user_id=user_id, username=username, name=name, text="", message_id=message_id)
    voice, state = voice_of(**kw)
    msg["voice"] = voice
    return msg, state


def rows(chat):
    with SessionLocal() as s:
        return [(m.role, m.status, m.text, m.result) for m in
                s.query(ConversationMessage).filter_by(chat_id=str(chat)).order_by(ConversationMessage.id)]


def reminder_rows():
    with SessionLocal() as s:
        return [(r.id, r.status, r.topic, [p["ref"] for p in r.recipients], list(r.delivered or []), r.chat_id)
                for r in s.query(Reminder).order_by(Reminder.id)]


def in_hours(hours):
    return (dt.datetime.now(reminders.zone()) + dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")


def director(model, decision, text="напомни"):
    model.decisions.append(decision)
    agent.handle_incoming(incoming(user_id=7, username="boss", name="Директор", text=text,
                                   message_id=int(now().timestamp() * 1000) % 10**8 + len(model.calls)))


def remind(to, at=None, topic="Совещание сегодня", details="в 15:00, переговорная 2", reply="Напомню"):
    return {"reply": reply, "actions": [{"type": "remind", "to": to, "at": at, "topic": topic, "details": details}]}


def everyone(facts):
    return [p["id"] for p in facts["team"]]


# ----------------------------------------------------------------------------- voice
def test_a_voice_message_is_understood_like_text(tgacc, model, monkeypatch):
    make_meeting()
    heard = hear(monkeypatch)
    model.decisions.append({"reply": "Принято, жду до пятницы", "actions": []})
    msg, state = say_voice()
    agent.handle_incoming(msg)
    assert state["fetched"] == 1 and heard == [(b"OGG", "audio/ogg", 3)]
    chat = rows(42)
    assert chat[0][2] == "Иван, ошибки с оплатой закрыть до пятницы" and chat[0][3]["voice"] is True
    assert ("42", "Принято, жду до пятницы") in tgacc.sent
    facts = model.calls[-1]["facts"]
    assert facts["voice_message"] is True and facts["voice_problem"] is None
    assert "расшифровка голосового" in model.calls[-1]["system"]


def test_a_stranger_voice_is_never_downloaded(tgacc, model, monkeypatch):
    make_meeting()
    heard = hear(monkeypatch)
    msg, state = say_voice(user_id=999, username="stranger", name="Чужой")
    agent.handle_incoming(msg)
    assert state["fetched"] == 0 and heard == [] and rows(999) == [] and tgacc.sent == []


def test_the_same_voice_is_recognised_once(tgacc, model, monkeypatch):
    make_meeting()
    heard = hear(monkeypatch)
    model.decisions.append({"reply": "", "actions": []})
    msg, state = say_voice(message_id=55)
    agent.handle_incoming(msg)
    again, state2 = say_voice(message_id=55)
    agent.handle_incoming(again)   # the unread scan hands it over again
    assert len(heard) == 1 and state2["fetched"] == 0 and len(rows(42)) == 1


def test_a_long_voice_is_not_recognised_and_the_model_says_so(tgacc, model, monkeypatch):
    make_meeting()
    heard = hear(monkeypatch)
    model.decisions.append({"reply": "Голосовое слишком длинное, напишите коротко", "actions": []})
    msg = incoming(text="", message_id=9)
    msg["voice"] = {"too_long": True, "seconds": 1800, "mime": "audio/mpeg"}
    agent.handle_incoming(msg)
    assert heard == []
    assert model.calls[-1]["facts"]["voice_problem"] == "too_long"
    assert rows(42)[0][2].startswith("(голосовое сообщение длиннее")
    assert ("42", "Голосовое слишком длинное, напишите коротко") in tgacc.sent


@pytest.mark.parametrize("outcome, problem", [(RuntimeError("модель недоступна"), "failed"), ("", "empty")])
def test_a_voice_that_cannot_be_understood_is_flagged_for_the_model(tgacc, model, monkeypatch, outcome, problem):
    make_meeting()
    hear(monkeypatch, text=outcome)
    model.decisions.append({"reply": "Не расслышал, повторите", "actions": []})
    msg, _ = say_voice(message_id=11)
    agent.handle_incoming(msg)
    assert model.calls[-1]["facts"]["voice_problem"] == problem
    assert ("42", "Не расслышал, повторите") in tgacc.sent


def test_voice_text_sends_the_audio_to_the_model_as_mp3(monkeypatch, tmp_path):
    from app import config, settings_store
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "key")
    settings_store.set_many({"transcribe_model": transcribe.LEGACY_CHAT_MODEL})
    wav = tmp_path / "v.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=300:d=2", str(wav)], check=True)
    seen = {}

    def chat(model_name, messages, **kwargs):
        seen["parts"] = messages[0]["content"]
        return json.dumps({"text": " Совещание в три "}, ensure_ascii=False)
    monkeypatch.setattr(llm, "chat", chat)
    with SessionLocal() as s:
        s.add(Employee(name="Иван Петров", telegram_chat_id="@ivan", position="Менеджер"))
        s.commit()
    assert transcribe.voice_text(wav.read_bytes(), "audio/wav", 2) == "Совещание в три"
    kinds = [p["type"] for p in seen["parts"]]
    assert kinds == ["text", "input_audio"] and seen["parts"][1]["input_audio"]["format"] == "mp3"
    assert "Иван Петров" in seen["parts"][0]["text"]   # the directory helps to spell the names


def test_voice_text_needs_the_key(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    with pytest.raises(llm.LLMError):
        transcribe.voice_text(b"x", "audio/ogg", 1)


def test_the_backend_hands_over_voice_notes_lazily():
    import asyncio

    class File:
        duration, size, mime_type = 4, 9000, "audio/ogg"

    class Voice:
        raw_text, id, file, voice, audio, video_note = "", 5, File(), object(), None, None

        async def download_media(self, file=None):
            return b"DATA"

    class Person:
        id, username, phone, bot, first_name, last_name = 42, "ivan", "", False, "Иван", ""

    handed = []
    backend = userbot.TelethonBackend()
    backend._on_message = handed.append

    async def go():
        await backend._dispatch(Person(), Voice())
        msg = handed[0]
        return msg, await asyncio.get_running_loop().run_in_executor(None, msg["voice"]["fetch"])
    msg, data = asyncio.run(go())
    assert msg["text"] == "" and msg["voice"]["seconds"] == 4 and data == b"DATA"


def test_the_backend_ignores_photos_and_stickers():
    import asyncio

    class Photo:
        raw_text, id, file, voice, audio, video_note = "", 5, None, None, None, None

    class Person:
        id, username, phone, bot, first_name, last_name = 42, "ivan", "", False, "Иван", ""

    handed = []
    backend = userbot.TelethonBackend()
    backend._on_message = handed.append
    asyncio.run(backend._dispatch(Person(), Photo()))
    assert handed == []


# ----------------------------------------------------------------------------- reminders: the director asks
def test_the_director_asks_to_remind_everyone_and_gets_a_report(tgacc, model):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts), reply="Напомню Ивану и Анне про совещание"),
             "напомни всем, что сегодня совещание в 15:00")
    assert ("7", "Напомню Ивану и Анне про совещание") in tgacc.sent       # the answer goes first
    assert [r[1] for r in reminder_rows()] == ["pending"]
    reminders.run_due()
    assert ("@ivan", "[reminder:Иван Петров] Совещание сегодня | в 15:00, переговорная 2") in tgacc.sent
    assert ("@anna", "[reminder:Анна Орлова] Совещание сегодня | в 15:00, переговорная 2") in tgacc.sent
    assert tgacc.sent[-1][0] == "7" and tgacc.sent[-1][1].startswith("[report]")
    assert reminder_rows()[0][1] == "sent" and sorted(reminder_rows()[0][4]) == ["@anna", "@ivan"]


def test_reminders_to_everyone_skip_the_one_who_asks(tgacc, model):
    ids = make_meeting(people=(("Иван Петров", "@ivan", ("Подготовить смету",)),))
    with SessionLocal() as s:
        boss = Employee(name="Директор Борис", telegram_chat_id="@boss", telegram_user_id="7", position="Директор")
        s.add(boss)
        s.commit()
    agent.invalidate_roster()
    director(model, remind("all"), "напомни всем")
    assert reminder_rows()[0][3] == ["@ivan"]


def test_the_reminder_to_reviewers(tgacc, model):
    make_meeting()
    settings_store.set_many({"approver_chat_ids": ["@boss", "@deputy"]})
    agent.invalidate_roster()
    director(model, remind("reviewers"), "напомни руководителям")
    assert reminder_rows()[0][3] == ["@deputy"]          # @boss is the one who asks
    tgacc.users["@deputy"] = 8
    reminders.run_due()
    assert any(ref == "@deputy" and text.startswith("[reminder:") for ref, text in tgacc.sent)


def test_a_reminder_for_a_named_person(tgacc, model):
    make_meeting()
    director(model, lambda facts: remind([p["id"] for p in facts["team"] if p["name"].startswith("Анна")]),
             "напомни Анне про совещание")
    assert reminder_rows()[0][3] == ["@anna"]
    reminders.run_due()
    assert [ref for ref, _ in tgacc.sent if ref in ("@ivan", "@anna")] == ["@anna"]


def test_a_reminder_waits_for_its_time(tgacc, model):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts), at=in_hours(3)), "напомни за три часа")
    reminders.run_due()
    assert [ref for ref, _ in tgacc.sent] == ["7"] and reminder_rows()[0][1] == "pending"
    with SessionLocal() as s:
        s.query(Reminder).update({"due_at": now() - dt.timedelta(minutes=1)})
        s.commit()
    reminders.run_due()
    assert reminder_rows()[0][1] == "sent" and any(ref == "@ivan" for ref, _ in tgacc.sent)


def test_the_model_is_given_the_local_time_to_work_out_the_hour(tgacc, model):
    make_meeting()
    director(model, {"reply": "ок", "actions": []}, "привет")
    facts = model.calls[-1]["facts"]
    assert set(facts["now"]) == {"local", "weekday", "timezone"} and facts["reminders_pending"] == []


@pytest.mark.parametrize("at, fragment", [
    ((dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M"), "уже прошло"),
    ((dt.datetime.now() + dt.timedelta(days=200)).strftime("%Y-%m-%d %H:%M"), "90 дней"),
    ("завтра утром", "ГГГГ-ММ-ДД"),
])
def test_a_bad_time_is_explained_to_the_model_which_corrects_itself(tgacc, model, at, fragment):
    make_meeting()
    model.decisions.append(lambda facts: remind(everyone(facts), at=at))
    model.decisions.append(lambda facts: remind(everyone(facts), at=in_hours(2)))
    agent.handle_incoming(incoming(user_id=7, username="boss", name="Директор", text="напомни", message_id=21))
    assert fragment in model.calls[1]["user"]
    assert len(reminder_rows()) == 1


def test_an_unusable_request_twice_in_a_row_creates_nothing(tgacc, model):
    make_meeting()
    for _ in range(2):
        model.decisions.append(remind("everybody"))
    agent.handle_incoming(incoming(user_id=7, username="boss", name="Директор", text="напомни", message_id=22))
    assert reminder_rows() == []


def test_somebody_without_a_telegram_contact_cannot_be_reminded(tgacc, model):
    make_meeting()
    with SessionLocal() as s:
        s.add(Employee(name="Борис Тихонов", telegram_chat_id="", position="Бухгалтер"))
        s.commit()
        silent = s.query(Employee).filter_by(name="Борис Тихонов").one().id
    model.decisions.append(remind([silent]))
    model.decisions.append({"reply": "У Бориса нет Telegram в справочнике, напомнить не могу", "actions": []})
    agent.handle_incoming(incoming(user_id=7, username="boss", name="Директор", text="напомни Борису", message_id=23))
    assert "нет Telegram-контакта" in model.calls[1]["user"] and reminder_rows() == []
    assert ("7", "У Бориса нет Telegram в справочнике, напомнить не могу") in tgacc.sent


# ----------------------------------------------------------------------------- reminders: employees
def test_an_employee_may_ask_to_be_reminded_himself(tgacc, model):
    make_meeting()
    model.decisions.append(remind("me", topic="Отчёт по оплате", details="до 17:00", reply="Напомню"))
    agent.handle_incoming(incoming(text="напомни мне про отчёт", message_id=31))
    assert reminder_rows()[0][3] == ["42"]
    reminders.run_due()
    assert ("42", "[reminder:Иван Петров] Отчёт по оплате | до 17:00") in tgacc.sent
    assert not any(text.startswith("[report]") for _, text in tgacc.sent)      # no report about a note to oneself


def test_an_employee_may_not_remind_other_people(tgacc, model):
    make_meeting()
    model.decisions.append(remind("all"))
    model.decisions.append({"reply": "Другим напомнить не могу", "actions": []})
    agent.handle_incoming(incoming(text="напомни всем", message_id=32))
    assert "только себе" in model.calls[1]["user"] and reminder_rows() == []


def test_an_employee_cannot_cancel_other_peoples_reminders(tgacc, model):
    make_meeting()
    director(model, remind("all", at=in_hours(5)), "напомни всем")
    rid = reminder_rows()[0][0]
    model.decisions.append({"reply": "x", "actions": [{"type": "cancel_reminder", "reminder_id": rid}]})
    model.decisions.append({"reply": "Это не моё напоминание", "actions": []})
    agent.handle_incoming(incoming(text="отмени", message_id=33))
    assert "не найдено" in model.calls[-1]["user"] and reminder_rows()[0][1] == "pending"


def test_the_director_can_see_and_cancel_a_reminder(tgacc, model):
    make_meeting()
    director(model, remind("all", at=in_hours(5)), "напомни всем через пять часов")
    rid = reminder_rows()[0][0]
    director(model, {"reply": "Отменил", "actions": [{"type": "cancel_reminder", "reminder_id": rid}]}, "отмени напоминание")
    seen = model.calls[-1]["facts"]["reminders_pending"]
    assert seen and seen[0]["id"] == rid and seen[0]["topic"] == "Совещание сегодня"
    assert reminder_rows()[0][1] == "cancelled"
    with SessionLocal() as s:
        s.query(Reminder).update({"due_at": now() - dt.timedelta(minutes=1)})
        s.commit()
    reminders.run_due()
    assert not any(ref in ("@ivan", "@anna") for ref, _ in tgacc.sent)


# ----------------------------------------------------------------------------- delivery guarantees
def test_nothing_is_sent_while_the_account_is_not_connected(tgacc, model, monkeypatch):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем")
    monkeypatch.setattr(userbot, "ready", lambda: False)
    reminders.run_due()
    assert reminder_rows()[0][1] == "pending" and not any(ref == "@ivan" for ref, _ in tgacc.sent)


def test_quiet_hours_do_not_hold_back_a_reminder_somebody_asked_for(tgacc, model, monkeypatch):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем")
    monkeypatch.setattr(userbot, "quiet_until", lambda now_utc=None: now() + dt.timedelta(hours=8))
    reminders.run_due()
    assert reminder_rows()[0][1] == "sent"


def test_one_failing_person_does_not_block_the_others_and_is_retried(tgacc, model, monkeypatch):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем")
    real = tgacc.send
    broken = {"on": True}

    def flaky(ref, text, reply_to=None, typing=0.0):
        if ref == "@anna" and broken["on"]:
            raise RuntimeError("Telegram не отвечает")
        return real(ref, text, reply_to=reply_to, typing=typing)
    monkeypatch.setattr(tgacc, "send", flaky)
    reminders.run_due()
    row = reminder_rows()[0]
    assert row[1] == "pending" and row[4] == ["@ivan"]
    with SessionLocal() as s:
        assert s.query(Reminder).one().attempts == 1
        s.query(Reminder).update({"due_at": now() - dt.timedelta(seconds=1)})
        s.commit()
    broken["on"] = False
    reminders.run_due()
    row = reminder_rows()[0]
    assert row[1] == "sent" and sorted(row[4]) == ["@anna", "@ivan"]
    assert sum(1 for ref, _ in tgacc.sent if ref == "@ivan" and text_is_reminder(_)) == 1   # Ivan was written to once


def text_is_reminder(text):
    return text.startswith("[reminder:")


def test_a_person_who_cannot_be_reached_is_reported_after_the_last_attempt(tgacc, model, monkeypatch):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем")
    real = tgacc.send

    def deaf(ref, text, reply_to=None, typing=0.0):
        if ref == "@anna":
            raise RuntimeError("Получатель не найден")
        return real(ref, text, reply_to=reply_to, typing=typing)
    monkeypatch.setattr(tgacc, "send", deaf)
    for _ in range(reminders.MAX_ATTEMPTS):
        with SessionLocal() as s:
            s.query(Reminder).filter(Reminder.status == "pending").update({"due_at": now() - dt.timedelta(seconds=1)})
            s.commit()
        reminders.run_due()
    assert reminder_rows()[0][1] == "failed"
    report = [t for ref, t in tgacc.sent if ref == "7" and t.startswith("[report]")]
    assert report and "Анна Орлова" in report[-1] and "Иван Петров" in report[-1]


def test_a_model_outage_delays_the_reminder_instead_of_losing_it(tgacc, model):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем")
    model.down = True
    reminders.run_due()
    assert reminder_rows()[0][1] == "pending" and reminder_rows()[0][4] == []
    model.down = False
    with SessionLocal() as s:
        s.query(Reminder).update({"due_at": now() - dt.timedelta(seconds=1)})
        s.commit()
    reminders.run_due()
    assert reminder_rows()[0][1] == "sent"


def test_an_interrupted_delivery_is_taken_again_without_repeating_messages(tgacc, model):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем")
    with SessionLocal() as s:     # a crash after Ivan got his message
        s.query(Reminder).update({"status": "sending", "claimed_at": now() - dt.timedelta(minutes=10),
                                  "delivered": ["@ivan"]})
        s.commit()
    reminders.run_due()
    sent = [ref for ref, text in tgacc.sent if text_is_reminder(text)]
    assert sent == ["@anna"] and reminder_rows()[0][1] == "sent"


def test_a_due_reminder_is_delivered_once_even_when_the_loop_runs_twice(tgacc, model):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем")
    reminders.run_due()
    reminders.run_due()
    assert sum(1 for ref, t in tgacc.sent if ref == "@ivan" and text_is_reminder(t)) == 1


def test_the_reminder_lands_in_the_conversation_history(tgacc, model):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем")
    reminders.run_due()
    assert any(role == "assistant" and text.startswith("[reminder:Иван") for role, _, text, _ in rows(42))


# ----------------------------------------------------------------------------- the service was down when it was due
def go_back(hours=0, minutes=0):
    """The reminder was due this long ago (the service was down or asleep until now)."""
    with SessionLocal() as s:
        s.query(Reminder).update({"due_at": now() - dt.timedelta(hours=hours, minutes=minutes)})
        s.commit()


def test_a_reminder_noticed_hours_late_is_not_sent_and_the_asker_is_told(tgacc, model):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем про совещание")
    go_back(hours=3)
    reminders.run_due()
    assert reminder_rows()[0][1] == "missed" and reminder_rows()[0][4] == []
    assert not any(ref in ("@ivan", "@anna") for ref, _ in tgacc.sent)         # nobody got a message about a meeting that is over
    ref, text = tgacc.sent[-1]
    assert ref == "7" and text.startswith("[missed] Совещание сегодня") and "late=180" in text and "Иван Петров" in text
    with SessionLocal() as s:
        assert "опоздание 180 мин" in s.query(Reminder).one().error


def test_a_reminder_a_few_minutes_late_still_goes_out(tgacc, model):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем")
    go_back(minutes=reminders.MAX_LATE // 60 - 5)
    reminders.run_due()
    assert reminder_rows()[0][1] == "sent" and any(ref == "@ivan" for ref, _ in tgacc.sent)


def test_the_limit_is_twenty_minutes(tgacc, model):
    assert reminders.MAX_LATE == 20 * 60
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем")
    go_back(minutes=21)
    reminders.run_due()
    assert reminder_rows()[0][1] == "missed"


def test_a_missed_reminder_is_reported_once_and_never_retried(tgacc, model):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем")
    go_back(hours=2)
    reminders.run_due()
    reminders.run_due()
    reminders.run_due()
    assert sum(1 for ref, t in tgacc.sent if t.startswith("[missed]")) == 1
    with SessionLocal() as s:
        assert reminders.pending_view(s, "7", True) == []


def test_nothing_is_said_about_a_missed_reminder_while_the_account_is_away_but_it_is_when_it_returns(tgacc, model, monkeypatch):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем")
    go_back(hours=5)
    connected = userbot.ready
    monkeypatch.setattr(userbot, "ready", lambda: False)
    reminders.run_due()
    assert reminder_rows()[0][1] == "pending" and not any(t.startswith("[missed]") for _, t in tgacc.sent)
    monkeypatch.setattr(userbot, "ready", connected)
    reminders.run_due()
    assert reminder_rows()[0][1] == "missed" and tgacc.sent[-1][1].startswith("[missed]")


def test_a_late_retry_is_judged_by_its_own_time_not_by_the_original(tgacc, model):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем")
    with SessionLocal() as s:       # Ivan got it on time, Anna's attempt failed and its retry was due 3 hours ago
        s.query(Reminder).update({"delivered": ["@ivan"], "attempts": 1, "due_at": now() - dt.timedelta(hours=3)})
        s.commit()
    reminders.run_due()
    assert reminder_rows()[0][1] == "missed"
    assert not any(ref == "@anna" for ref, _ in tgacc.sent)
    assert "for=['Анна Орлова']" in tgacc.sent[-1][1]                        # only the one who was left out


def test_a_punctual_reminder_is_not_late_because_the_queue_before_it_was_long(tgacc, model):
    make_meeting()
    director(model, lambda facts: remind(everyone(facts)), "напомни всем")
    due = now() - dt.timedelta(hours=1)
    with SessionLocal() as s:
        s.query(Reminder).update({"due_at": due})
        s.commit()
    reminders._deliver(1, noticed=due + dt.timedelta(minutes=1))            # the loop saw it a minute after its time
    assert reminder_rows()[0][1] == "sent"


def test_parse_when():
    base = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc)
    assert reminders.parse_when(None, base) == base and reminders.parse_when("", base) == base
    local = reminders.parse_when("2026-10-12 17:00", base)
    assert local.astimezone(reminders.zone()).strftime("%H:%M") == "17:00"
    assert reminders.parse_when("2026-10-12T17:00:00", base) == local
    with pytest.raises(reminders.WhenError):
        reminders.parse_when("2026-10-12 09:00", base)
