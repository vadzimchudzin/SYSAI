"""Sprint 11.3: an order in the chat («запакуй и отправь ему») becomes a case that ends only when the files are sent.
The scenario of 25.10: Вадим needs screenshots in a ZIP, OMG (reviewer) sends 4 photos and says «запакуй и отправь»."""
import hashlib
import io
import zipfile

import pytest

from app import agent, cases, settings_store
from app.db import Artifact, Case, CaseEvent, CaseStep, Document, Employee, SessionLocal
from test_ai_mode import FakeBackend, tgacc  # noqa: F401  (fixtures)
from test_cases import CaseModel, cmodel, incoming, doc  # noqa: F401

JPG = [b"\xff\xd8 screenshot %d" % i for i in range(4)]


@pytest.fixture
def team(tgacc):
    tgacc.users.update({"@vadim": 50, "@omg": 60})
    settings_store.set_many({"approver_chat_ids": ["@omg"], "director_chat_ids": [], "quiet_from_hour": 0,
                             "quiet_to_hour": 0, "case_autonomy": "auto"})
    with SessionLocal() as s:
        vadim = Employee(name="Вадим", position="Разработчик", telegram_chat_id="@vadim")
        omg = Employee(name="OMG", position="Разработчик", telegram_chat_id="@omg")
        s.add_all([vadim, omg])
        s.commit()
        ids = {"vadim": vadim.id, "omg": omg.id}
    agent.invalidate_roster()
    return ids


def send_photos(start=700):
    for i, data in enumerate(JPG):
        agent.handle_incoming(incoming(60, "omg", message_id=start + i, document=doc(data, "фото.jpg", "image/jpeg")))


def order(cmodel, team, zip_it=True):
    """The model reads recent_files and recent_cases and orders the delivery (what a good answer looks like)."""
    seen = {}

    def decide(facts):
        seen.update(facts)
        ids = [d["id"] for d in facts["recent_files"]]
        return {"reply": "Упакую и отправлю Вадиму", "actions": [
            {"type": "deliver_files", "to": team["vadim"], "document_ids": ids, "zip": zip_it,
             "archive_name": "Скриншоты для Вадима", "note": "Скриншоты для Вадима"}]}
    cmodel.decisions.append(decide)
    agent.handle_incoming(incoming(60, "omg", text="Это всё нужно запаковать и отправить зипом", message_id=800))
    return seen


def delivery_case():
    with SessionLocal() as s:
        case = s.query(Case).filter_by(kind="deliver").one()
        return case.id, case.status, [(x.tool, x.status) for x in case.steps]


def test_photos_are_zipped_sent_to_vadim_and_the_case_closes_on_delivery(team, tgacc, cmodel):
    send_photos()
    seen = order(cmodel, team)
    assert len(seen["recent_files"]) == 4  # the model saw every photo, not one

    case_id, status, steps = delivery_case()
    assert status == "done"
    assert steps == [("zip_pack", "done"), ("send_files", "done"), ("report", "done"), ("close", "done")]
    ref, filename, caption = tgacc.files[-1]
    assert ref == "@vadim" and filename == "Скриншоты для Вадима.zip" and caption.startswith("[deliver]")
    with zipfile.ZipFile(io.BytesIO(tgacc.file_data[-1])) as z:
        assert sorted(z.namelist()) == ["фото (2).jpg", "фото (3).jpg", "фото (4).jpg", "фото.jpg"]
        assert sorted(z.read(n) for n in z.namelist()) == sorted(JPG)  # the very bytes OMG sent
    assert any(r == "@omg" and t.startswith("[report]") for r, t in tgacc.sent)  # the reviewer is told it is done
    with SessionLocal() as s:
        kinds = [e.kind for e in s.query(CaseEvent).filter_by(case_id=case_id).order_by(CaseEvent.id)]
        assert kinds[0] == "planned" and "zip_packed" in kinds and "files_sent" in kinds and kinds[-1] == "done"
        assert all(d.case_id == case_id for d in s.query(Document).all())
        sent = s.query(CaseStep).filter_by(case_id=case_id, tool="send_files").one().result
        assert sent["message_ids"] and sent["archive"] == "Скриншоты для Вадима.zip" and len(sent["files"]) == 4


def test_without_zip_each_file_goes_as_it_is(team, tgacc, cmodel):
    send_photos()
    order(cmodel, team, zip_it=False)
    assert delivery_case()[1] == "done"
    assert [f[0] for f in tgacc.files[-4:]] == ["@vadim"] * 4
    assert sorted(tgacc.file_data[-4:]) == sorted(JPG)


def test_ask_report_offers_the_next_step(team, tgacc, cmodel):
    from test_cases import make_task
    from app import assistant
    office = {"vadim": team["vadim"]}
    task_id = make_task(office, kind="ask", counterparts=(("recipient", "vadim"),), target="vadim",
                        title="Узнать у Вадима, что ему нужно")
    assistant.run_task(task_id)
    agent.handle_incoming(incoming(50, "vadim", text="нужны скриншоты и документы, запаковать в зип", message_id=900))
    report = [w for w in cmodel.writes if w["purpose"] == "report"][-1]
    assert "скриншоты" in report["answers"][0]["text"]
    assert "упакую в ZIP и передам" in open(cases.__file__, encoding="utf-8").read()  # the report offers the next step
    # the reviewer sees the finished case with Вадим's answer: «перешли ему» can be understood
    view = cases.recent_view()
    assert view[0]["people"][0]["name"] == "Вадим" and "скриншоты" in view[0]["answers"][0]


def test_order_checks(team, tgacc, cmodel):
    send_photos()
    ctx_holder = {}

    def bad(facts):
        ctx_holder.update(facts)
        return {"reply": "Отправлю", "actions": [{"type": "deliver_files", "to": team["vadim"], "document_ids": [9999],
                                                  "zip": True}]}
    cmodel.decisions += [bad, bad]
    agent.handle_incoming(incoming(60, "omg", text="отправь ему", message_id=810))
    with SessionLocal() as s:
        assert s.query(Case).filter_by(kind="deliver").count() == 0  # unknown files: nothing is sent
    ctx = dict(ctx_holder)
    employee_view = {"team": ctx["team"], "recent_files": ctx["recent_files"]}
    action = {"type": "deliver_files", "to": team["vadim"], "document_ids": [ctx["recent_files"][0]["id"]]}
    assert agent._check(action, employee_view, None, False, False).startswith("deliver_files доступно только")
    assert agent._denied(action, None, False, False)
    assert agent._check(dict(action, to=424242), employee_view, None, True, True).startswith("deliver_files: to")


def test_changed_file_stops_the_delivery(team, tgacc, cmodel):
    send_photos()
    with SessionLocal() as s:
        d = s.query(Document).first()
        d.data = b"tampered"
        s.commit()
    order(cmodel, team)
    with SessionLocal() as s:
        assert s.query(Case).filter_by(kind="deliver").count() == 0  # refused before anything is packed or sent
    assert not any(f[0] == "@vadim" for f in tgacc.files)


def test_file_changed_after_the_order_stops_at_packing(team, tgacc, cmodel):
    send_photos()
    with SessionLocal() as s:
        ids = [d.id for d in s.query(Document).order_by(Document.id)]
    case_id = cases.start_delivery("OMG", team["vadim"], ids, True, "a")
    with SessionLocal() as s:
        s.get(Document, ids[0]).data = b"tampered"
        s.commit()
    cases.advance(case_id)
    _, status, steps = delivery_case()
    assert status == "attention" and steps[0] == ("zip_pack", "failed")
    assert not any(f[0] == "@vadim" for f in tgacc.files)


def test_failed_send_is_never_repeated_automatically(team, tgacc, cmodel):
    send_photos()
    tgacc.fail = RuntimeError("network down")
    order(cmodel, team)
    tgacc.fail = None
    case_id, status, steps = delivery_case()
    assert status == "attention" and ("send_files", "uncertain") in steps
    cases.advance(case_id)
    assert not any(f[0] == "@vadim" for f in tgacc.files)


def test_same_names_do_not_overwrite():
    taken = set()
    assert [cases._unique(n, taken) for n in ("акт.pdf", "акт.pdf", "Акт.PDF", "фото")] == \
        ["акт.pdf", "акт (2).pdf", "Акт (3).PDF", "фото"]
    unsafe = cases._safe_name("../../etc/passwd")
    assert "/" not in unsafe and "\\" not in cases._safe_name("a\\b.txt") and unsafe


def test_reports_go_to_reviewers_not_the_observing_director(team, tgacc, cmodel):
    settings_store.set_many({"director_chat_ids": ["@boss"], "approver_chat_ids": ["@omg"]})
    assert cases._leaders() == ["@omg"]
    settings_store.set_many({"approver_chat_ids": []})
    assert cases._leaders() == ["@boss"]  # only when nobody else decides
