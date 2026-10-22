"""Live check of Sprint 4: 20 questions about the company and people, asked in Telegram by the director, answered by
the real model with the memory (fake Telegram, temporary database), plus learning from a meeting transcript.

    OPENROUTER_API_KEY=... python3 scripts/memory_live_check.py [model]
"""
import os
import sys
import tempfile

tmp = tempfile.mkdtemp(prefix="sysai_mem_")
os.environ.update(SYSAI_TESTING="1", DATA_DIR=tmp, DATABASE_URL=f"sqlite:///{tmp}/m.db", SECRET_KEY="live-check",
                  ADMIN_PASSWORD="pw", TELEGRAM_BOT_TOKEN="")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))

from app import agent, db, memory, settings_store, userbot  # noqa: E402
from app.db import Meeting, Note, SessionLocal  # noqa: E402
from test_ai_mode import FakeBackend  # noqa: E402
import test_memory as tm  # noqa: E402

EXPECT = [["директор", "вадим"], ["вентиляц"], ["застройщик", "торгов"], ["понедельник"], ["анн"], ["пётр", "петр"],
          ["ольг"], ["иван"], ["игор"], ["pdf"], ["7:30", "8"], ["вентснаб"], ["15 декабря"], ["строймонтаж"],
          ["20"], ["директор"], ["5 рабоч", "пять рабоч", "5 дн"], ["кс-2", "кс‑2"], ["12 480", "12480"],
          ["3 рабоч", "три рабоч", "скрыт"]]


def main(model):
    db.init_db()
    fake = FakeBackend()
    fake.users["@boss"] = 7
    userbot.backend = fake
    settings_store.set_many({"ai_employee": True, "approver_chat_ids": ["@boss"], "director_chat_ids": ["@boss"],
                             "agent_model": model, "quiet_from_hour": 0, "quiet_to_hour": 0})
    userbot.login_start("1234567", "a" * 32, "+375291234567")
    userbot.login_code("12345")
    ids = tm.seed_company()
    agent.invalidate_roster()
    ok = 0
    for n, ((question, _), keys) in enumerate(zip(tm.GOLDEN, EXPECT), start=1):
        before = len(fake.sent)
        agent.handle_incoming({"user_id": 7, "username": "boss", "phone": "", "name": "Вадим", "text": question,
                               "message_id": 1000 + n})
        reply = " ".join(t for _, t in fake.sent[before:]) or "(нет ответа)"
        good = any(k in reply.lower() for k in keys)
        ok += good
        print(f"{'✓' if good else '✗'} {n:2}. {question}\n      → {reply[:300]}")
    print(f"\nВопросы: {ok} из {len(tm.GOLDEN)}")

    with SessionLocal() as s:
        m = Meeting(title="Планёрка", status="done", transcript=tm.TRANSCRIPT, report={}, options={})
        s.add(m)
        s.commit()
        mid = m.id
    settings_store.set_many({"memory_autolearn": True})
    kept = memory.learn_from_meeting(mid)
    print(f"\nПосле совещания добавлено фактов: {kept}")
    with SessionLocal() as s:
        for note in s.query(Note).filter_by(meeting_id=mid):
            print(f"  + [{note.confidence}] {note.text}  «{note.quote}»")
    return 0 if ok == len(tm.GOLDEN) and kept >= 1 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "google/gemini-2.5-flash"))
