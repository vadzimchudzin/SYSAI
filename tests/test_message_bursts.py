"""Sprint 11.2: an album or a quick burst of messages is answered once, with all files together."""
from app import agent
from app.db import ConversationMessage, SessionLocal


def row(chat, text, doc=None, status="agent_pending", **extra):
    with SessionLocal() as s:
        r = ConversationMessage(chat_id=chat, message_id=row.n, role="user", status=status, text=text,
                                result={"attempts": 0, "employee_id": 18, "full_access": True, "reviewer": True,
                                        "document": doc, **extra})
        row.n += 1
        s.add(r)
        s.commit()
        return r.id


row.n = 1


def get(rid):
    with SessionLocal() as s:
        return s.get(ConversationMessage, rid)


def photo(i):
    return {"document_id": i, "filename": "фото.jpg", "kind": "image", "size": 1000}


def test_text_plus_album_becomes_one_message():
    first = row("622744825", "Я ща скину тебе, перешлёшь ему")
    photos = [row("622744825", "(прислал файл «фото.jpg»)", photo(i), grouped_id="77") for i in range(1, 5)]
    lead = agent._merge_burst("622744825")
    assert lead == first
    m = get(first)
    assert m.text.splitlines() == ["Я ща скину тебе, перешлёшь ему",
                                   "(прислал 4 файл(ов) одним разом: «фото.jpg», «фото.jpg», «фото.jpg», «фото.jpg»)"]
    assert [d["document_id"] for d in m.result["documents"]] == [1, 2, 3, 4]
    assert m.result["merged"] == photos
    assert all(get(p).status == "agent_merged" and get(p).result["merged_into"] == first for p in photos)


def test_single_message_is_left_alone():
    only = row("111", "Привет")
    assert agent._merge_burst("111") == only
    assert get(only).text == "Привет" and "merged" not in get(only).result


def test_other_people_and_answered_messages_are_not_merged():
    mine = row("222", "раз")
    theirs = row("333", "чужое")
    deciding = row("222", "уже обдумываю", decision={"reply": "x", "actions": []})
    later = row("222", "два")
    assert agent._merge_burst("222") == mine
    assert get(mine).text == "раз\nдва"
    assert get(theirs).status == "agent_pending" and get(deciding).status == "agent_pending"


def test_case_link_of_any_part_is_kept():
    a = row("444", "вот акт")
    b = row("444", "(прислал файл «акт.pdf»)", photo(9), case={"case_id": 5, "title": "Акт"})
    agent._merge_burst("444")
    assert get(a).result["case"]["case_id"] == 5 and get(b).status == "agent_merged"


def test_processing_a_merged_part_answers_the_burst_once(monkeypatch):
    answered = []
    monkeypatch.setattr(agent, "_process", lambda rid: answered.append(rid))
    first = row("555", "смотри")
    parts = [row("555", "(прислал файл «фото.jpg»)", photo(i)) for i in (11, 12)]
    agent.process(parts[-1])   # whichever part the worker picks up first
    assert answered == [first]
    agent.process(parts[0])    # the others are merged: nothing more to answer
    assert answered == [first]


def test_files_received_in_prompt_context():
    assert "files_received" in agent.ACTIONS_HELP


def test_schedule_waits_for_quiet_then_answers_once(monkeypatch):
    import time
    monkeypatch.setattr(agent, "BURST_QUIET", 0.3)
    submitted = []
    monkeypatch.setattr(agent.POOL, "submit", lambda fn, rid: submitted.append(rid))
    first = row("666", "раз")
    agent._schedule("666")
    time.sleep(0.1)
    row("666", "(прислал файл «фото.jpg»)", photo(21))
    agent._schedule("666")
    time.sleep(0.1)
    agent._schedule("666")
    assert submitted == []          # still within the quiet window
    time.sleep(0.6)
    assert submitted == [first]     # one answer for the whole burst


def test_schedule_never_waits_longer_than_max(monkeypatch):
    import time
    monkeypatch.setattr(agent, "BURST_QUIET", 5)
    monkeypatch.setattr(agent, "BURST_MAX", 0.2)
    submitted = []
    monkeypatch.setattr(agent.POOL, "submit", lambda fn, rid: submitted.append(rid))
    first = row("777", "раз")
    agent._schedule("777")
    time.sleep(0.3)
    agent._schedule("777")          # past the cap: answered right away
    time.sleep(0.2)
    assert submitted == [first]


def test_retried_or_old_messages_are_answered_on_their_own():
    import datetime as dt
    failed = row("888", "первое", attempts=1)
    new = row("888", "второе")
    assert agent._merge_burst("888") == failed  # earliest waiting, but nothing merged into it
    assert get(new).status == "agent_pending" and get(failed).text == "первое"
    old = row("999", "утром")
    with SessionLocal() as s:
        s.get(ConversationMessage, old).created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        s.commit()
    fresh = row("999", "сейчас")
    agent._merge_burst("999")
    assert get(fresh).status == "agent_pending" and get(old).text == "утром"
