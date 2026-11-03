"""№28 «Битрикс24»: tasks given here go to Bitrix24 and their status, deadline, title and person follow the side where
they changed (three-way: a field changed on both sides is taken from Bitrix24); new tasks of Bitrix24 for our people
come here; the companies of the CRM and the client cards are kept the same (name, УНП, kind; phones and contact persons
as a block of the card's contacts); new client cards go to the CRM. The portal is a fake answering like the REST API."""
import datetime as dt
import re

import pytest

from app import bitrix, bitrix_sync, config, settings_store
from app.db import BitrixLink, Employee, Entity, SessionLocal, Task

MSK = dt.timezone(dt.timedelta(hours=3))


class FakeBitrix:
    """The REST API of a portal: tasks (camelCase answers), users, companies, contacts, requisites (UPPER_CASE)."""

    def __init__(self):
        self.clock = dt.datetime.now(MSK).replace(microsecond=0)  # what happens there happens from now on
        self.tasks, self.companies, self.contacts, self.requisites = {}, {}, {}, {}
        self.users = [{"ID": "7", "NAME": "Анна", "LAST_NAME": "Смирнова", "EMAIL": "anna@firm.by", "WORK_POSITION": "ГИП"},
                      {"ID": "8", "NAME": "Борис", "LAST_NAME": "Котов", "EMAIL": "b.kotov@firm.by"},
                      {"ID": "9", "NAME": "Пётр", "LAST_NAME": "Новиков", "EMAIL": ""}]
        self.calls = []
        self.next_id = 100

    def tick(self) -> str:
        self.clock += dt.timedelta(seconds=30)
        return self.clock.isoformat()

    def new_id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    # what people do in Bitrix24 itself
    def add_task(self, title, responsible, deadline=None, status="2", created=None):
        tid = self.new_id()
        stamp = self.tick()
        self.tasks[tid] = {"id": tid, "title": title, "status": status, "responsibleId": str(responsible),
                           "deadline": f"{deadline}T19:00:00+03:00" if deadline else None, "description": "[B]Важно[/B]",
                           "createdDate": created or stamp, "changedDate": stamp}
        return tid

    def edit_task(self, tid, **fields):
        self.tasks[tid].update(fields)
        self.tasks[tid]["changedDate"] = self.tick()

    def edit_company(self, cid, **fields):
        self.companies[cid].update(fields)
        self.companies[cid]["DATE_MODIFY"] = self.tick()

    @staticmethod
    def _page(items, params, size=2):
        start = int(params.get("start") or 0)
        chunk = items[start:start + size]
        out = {"result": chunk, "total": len(items)}
        if start + size < len(items):
            out["next"] = start + size
        return out

    @staticmethod
    def _since(items, params, field, key):
        mark = (params.get("filter") or {}).get(key)
        rows = [i for i in items if not mark or dt.datetime.fromisoformat(i[field]) >= dt.datetime.fromisoformat(mark)]
        return sorted(rows, key=lambda i: (dt.datetime.fromisoformat(i[field]), int(i.get("id") or i.get("ID"))))

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "user.current":
            return {"result": {"ID": "1", "NAME": "Админ", "LAST_NAME": "Портала"}}
        if method == "scope":
            return {"result": ["task", "user", "crm"]}
        if method == "user.get":
            return self._page(self.users, params)
        if method == "tasks.task.list":
            rows = self._since(list(self.tasks.values()), params, "changedDate", ">=CHANGED_DATE")
            page = self._page(rows, params)
            page["result"] = {"tasks": [dict(r) for r in page["result"]]}
            return page
        if method == "tasks.task.get":
            tid = str(params["taskId"])
            if tid not in self.tasks:
                raise bitrix.BitrixError("tasks.task.get: TASK_NOT_FOUND_OR_NOT_ACCESSIBLE")
            return {"result": {"task": dict(self.tasks[tid])}}
        if method == "tasks.task.add":
            f = params["fields"]
            tid = self.new_id()
            stamp = self.tick()
            deadline = f.get("DEADLINE")
            self.tasks[tid] = {"id": tid, "title": f["TITLE"], "status": "2", "responsibleId": str(f["RESPONSIBLE_ID"]),
                               "deadline": deadline + "+03:00" if deadline else None, "description": f.get("DESCRIPTION", ""),
                               "createdDate": stamp, "changedDate": stamp}
            return {"result": {"task": {"id": tid}}}
        if method.startswith("tasks.task."):
            tid = str(params["taskId"])
            if tid not in self.tasks:
                raise bitrix.BitrixError(f"{method}: TASK_NOT_FOUND_OR_NOT_ACCESSIBLE")
            action = method.rsplit(".", 1)[1]
            if action == "update":
                f = params["fields"]
                mapping = {"TITLE": "title", "RESPONSIBLE_ID": "responsibleId"}
                for k, v in f.items():
                    if k == "DEADLINE":
                        self.tasks[tid]["deadline"] = v + "+03:00" if v else None
                    elif k in mapping:
                        self.tasks[tid][mapping[k]] = str(v)
            else:
                self.tasks[tid]["status"] = {"complete": "5", "start": "3", "defer": "6", "renew": "2",
                                             "pause": "2"}[action]
            self.tasks[tid]["changedDate"] = self.tick()
            return {"result": {"task": dict(self.tasks[tid])}}
        if method == "crm.company.list":
            return self._page(self._since(list(self.companies.values()), params, "DATE_MODIFY", ">=DATE_MODIFY"), params)
        if method == "crm.company.get":
            return {"result": dict(self.companies[str(params["id"])])}
        if method == "crm.company.add":
            cid = self.new_id()
            self.companies[cid] = {"ID": cid, **params["fields"], "DATE_MODIFY": self.tick()}
            return {"result": int(cid)}
        if method == "crm.company.update":
            self.edit_company(str(params["id"]), **params["fields"])
            return {"result": True}
        if method == "crm.contact.list":
            return self._page(self._since(list(self.contacts.values()), params, "DATE_MODIFY", ">=DATE_MODIFY"), params)
        if method == "crm.requisite.list":
            ids = {str(i) for i in params["filter"]["@ENTITY_ID"]}
            return self._page([r for r in self.requisites.values() if r["ENTITY_ID"] in ids], params, size=50)
        if method == "crm.requisite.preset.list":
            return {"result": [{"ID": "1", "NAME": "Организация", "ACTIVE": "Y"}]}
        if method == "crm.requisite.add":
            rid = self.new_id()
            f = params["fields"]
            self.requisites[rid] = {"ID": rid, "ENTITY_ID": str(f["ENTITY_ID"]), "RQ_INN": f.get("RQ_INN", "")}
            return {"result": int(rid)}
        if method == "crm.requisite.update":
            self.requisites[str(params["id"])].update(params["fields"])
            return {"result": True}
        raise AssertionError(f"unexpected method {method}")

    def count(self, method):
        return sum(1 for m, _ in self.calls if m == method)


@pytest.fixture
def portal(monkeypatch):
    fake = FakeBitrix()
    monkeypatch.setattr(bitrix, "call_full", fake)
    settings_store.set_many({"bitrix_webhook_url": "https://firm.bitrix24.by/rest/1/abcdef123"})
    return fake


def anna():
    with SessionLocal() as s:
        e = Employee(name="Анна Смирнова", telegram_chat_id="1001", email="anna@firm.by", bitrix_user_id="7")
        s.add(e)
        s.commit()
        return e.id


def order_task(person_id, title="Согласовать смету", deadline=dt.date(2026, 3, 20), status="sent") -> int:
    with SessionLocal() as s:
        t = Task(meeting_id=None, title=title, assignee_id=person_id, assignee_name="Анна", executor="person",
                 deadline=deadline, deadline_source="stated", status=status, source_quote="Поручение из чата от директора")
        s.add(t)
        s.commit()
        return t.id


def task(task_id):
    with SessionLocal() as s:
        return s.get(Task, task_id)


def test_a_task_given_here_goes_to_bitrix_and_its_status_and_deadline_follow_both_ways(portal):
    person = anna()
    bitrix_sync.switched({"bitrix_enabled": True, "bitrix_sync_tasks": True})
    tid = order_task(person)
    state = bitrix_sync.sync()
    assert state["ok"] is True, state
    remote_id = task(tid).bitrix_task_id
    assert remote_id and portal.tasks[remote_id]["responsibleId"] == "7"
    assert portal.tasks[remote_id]["deadline"].startswith("2026-03-20")
    assert "Поручение из чата" in portal.tasks[remote_id]["description"]
    assert "поставлено задач в Битрикс24: 1" in state["summary"]
    # done here → completed there
    with SessionLocal() as s:
        s.get(Task, tid).status = "done"
        s.commit()
    bitrix_sync.sync()
    assert portal.tasks[remote_id]["status"] == "5"
    calls = len(portal.calls)
    bitrix_sync.sync()  # nothing changed: nothing is sent again
    assert not [m for m, _ in portal.calls[calls:] if m.startswith("tasks.task.") and m != "tasks.task.list"]
    # reopened and moved in Bitrix24 → here too
    portal.edit_task(remote_id, status="3", deadline="2026-03-25T19:00:00+03:00")
    bitrix_sync.sync()
    t = task(tid)
    assert t.status == "in_progress" and t.deadline == dt.date(2026, 3, 25) and t.done_at is None
    # both sides changed the title: Bitrix24 wins, the log says so
    with SessionLocal() as s:
        s.get(Task, tid).title = "Согласовать смету с заказчиком"
        s.commit()
    portal.edit_task(remote_id, title="Смета: согласовать до пятницы")
    state = bitrix_sync.sync()
    assert task(tid).title == "Смета: согласовать до пятницы"
    assert any("и здесь, и в Битрикс24" in l["text"] for l in state["log"])
    # only here: the deadline goes there
    with SessionLocal() as s:
        s.get(Task, tid).deadline = dt.date(2026, 4, 1)
        s.commit()
    bitrix_sync.sync()
    assert portal.tasks[remote_id]["deadline"].startswith("2026-04-01")


def test_tasks_before_the_switch_drafts_and_people_without_a_bitrix_id_are_not_put(portal):
    person = anna()
    old = order_task(person, title="Старая задача")
    with SessionLocal() as s:
        s.get(Task, old).created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
        nobody = Employee(name="Пётр", telegram_chat_id="1002")
        s.add(nobody)
        s.commit()
        nobody_id = nobody.id
    bitrix_sync.switched({"bitrix_enabled": True})
    draft = order_task(person, title="Черновик", status="draft")
    unknown = order_task(nobody_id, title="Петру")
    bitrix_sync.sync()
    assert portal.count("tasks.task.add") == 0
    assert not task(old).bitrix_task_id and not task(draft).bitrix_task_id and not task(unknown).bitrix_task_id


def test_new_tasks_of_bitrix_for_our_people_come_here_once(portal):
    person = anna()
    portal.add_task("Задача до включения", 7, created=(portal.clock - dt.timedelta(days=3)).isoformat())
    bitrix_sync.switched({"bitrix_import_tasks": True, "bitrix_sync_tasks": True})
    portal.clock = dt.datetime.now(MSK).replace(microsecond=0)
    mine = portal.add_task("Проверить акт КС-2", 7, deadline="2026-03-12")
    portal.add_task("Чужому человеку", 99)
    portal.add_task("Уже сделано", 7, status="5")
    bitrix_sync.sync()
    bitrix_sync.sync()
    with SessionLocal() as s:
        rows = s.query(Task).all()
        assert [t.title for t in rows] == ["Проверить акт КС-2"]
        t = rows[0]
        assert t.assignee_id == person and t.status == "sent" and t.deadline == dt.date(2026, 3, 12)
        assert t.bitrix_task_id == mine and t.meeting_id is None and t.description == "Важно"
        assert "Битрикс24" in t.source_quote
        tid = t.id
    with SessionLocal() as s:
        s.get(Task, tid).status = "done"
        s.commit()
    bitrix_sync.sync()
    assert portal.tasks[mine]["status"] == "5"


def test_tasks_put_into_bitrix_before_the_sync_are_linked_and_the_further_status_wins(portal):
    person = anna()
    a = portal.add_task("Сделали в Битрикс24", 7, status="5")
    b = portal.add_task("Сделали здесь", 7)
    with SessionLocal() as s:
        for title, remote, status in (("Сделали в Битрикс24", a, "sent"), ("Сделали здесь", b, "done"),
                                      ("Удалили там", "999", "sent")):
            s.add(Task(meeting_id=None, title=title, assignee_id=person, executor="person", status=status,
                       bitrix_task_id=remote))
        s.commit()
    bitrix_sync.switched({"bitrix_sync_tasks": True})
    state = bitrix_sync.sync()
    assert state["ok"] is True, state
    with SessionLocal() as s:
        by_title = {t.title: t for t in s.query(Task)}
        assert by_title["Сделали в Битрикс24"].status == "done"
        assert s.query(BitrixLink).filter_by(remote_id="999").one().state == "gone"
    assert portal.tasks[b]["status"] == "5"


def test_a_person_unknown_here_does_not_change_the_assignee(portal):
    person = anna()
    bitrix_sync.switched({"bitrix_enabled": True, "bitrix_sync_tasks": True})
    tid = order_task(person)
    bitrix_sync.sync()
    remote_id = task(tid).bitrix_task_id
    portal.edit_task(remote_id, responsibleId="55")
    state = bitrix_sync.sync()
    assert task(tid).assignee_id == person
    assert any("пользователь 55" in l["text"] for l in state["log"])
    calls = portal.count("tasks.task.update")
    bitrix_sync.sync()
    assert portal.count("tasks.task.update") == calls  # the old person is not sent back


def test_employees_are_matched_to_the_users_by_mail_and_by_name(portal):
    with SessionLocal() as s:
        s.add_all([Employee(name="Анна Смирнова", email="ANNA@firm.by"), Employee(name="Котов Борис Ильич"),
                   Employee(name="Пётр"), Employee(name="Ирина", bitrix_user_id="12")])
        s.commit()
    users = bitrix_sync.refresh_users()
    assert {u["id"] for u in users} == {"7", "8", "9"}
    matched = bitrix_sync.match_employees(users)
    assert sorted(matched) == ["Анна Смирнова", "Котов Борис Ильич"]
    with SessionLocal() as s:
        ids = {e.name: e.bitrix_user_id for e in s.query(Employee)}
    assert ids == {"Анна Смирнова": "7", "Котов Борис Ильич": "8", "Пётр": "", "Ирина": "12"}


def test_companies_of_the_crm_and_client_cards_are_kept_the_same(portal):
    from app import entities
    portal.companies = {
        "1": {"ID": "1", "TITLE": "ОАО «Криница»", "COMPANY_TYPE": "CUSTOMER", "DATE_MODIFY": portal.tick(),
              "PHONE": [{"VALUE": "+375 17 111-22-33", "VALUE_TYPE": "WORK"}], "EMAIL": [{"VALUE": "info@krinica.by"}],
              "WEB": [], "ADDRESS": "Минск, ул. Радиальная, 52"},
        "2": {"ID": "2", "TITLE": "ООО «СтройМонтаж»", "COMPANY_TYPE": "SUPPLIER", "DATE_MODIFY": portal.tick(),
              "PHONE": [], "EMAIL": []},
        "3": {"ID": "3", "TITLE": "Своя Компания", "COMPANY_TYPE": "OTHER", "DATE_MODIFY": portal.tick()},
    }
    portal.requisites = {"50": {"ID": "50", "ENTITY_ID": "1", "RQ_INN": "190000001"}}
    portal.contacts = {"70": {"ID": "70", "NAME": "Иван", "LAST_NAME": "Петров", "POST": "главный инженер",
                              "COMPANY_ID": "1", "PHONE": [{"VALUE": "+375 29 555-44-33"}], "DATE_MODIFY": portal.tick()}}
    settings_store.set_many({"company_name": "Своя Компания"})
    with SessionLocal() as s:
        s.add(Entity(kind="client", name="Криница", aliases=[], contacts="бухгалтерия: доб. 12", source="manual"))
        s.commit()
    bitrix_sync.switched({"bitrix_sync_contacts": True})
    state = bitrix_sync.sync()
    assert state["ok"] is True, state
    with SessionLocal() as s:
        cards = {e.name: e for e in s.query(Entity)}
        assert set(cards) == {"Криница", "ООО «СтройМонтаж»"}  # our own company is not a client
        k = cards["Криница"]
        assert k.unp == "190000001" and k.address.startswith("Минск")
        assert k.contacts.startswith("бухгалтерия: доб. 12")
        assert "[Битрикс24]" in k.contacts and "+375 17 111-22-33" in k.contacts and "info@krinica.by" in k.contacts
        assert "Петров Иван (главный инженер) +375 29 555-44-33" in k.contacts
        assert cards["ООО «СтройМонтаж»"].kind == "contractor" and cards["ООО «СтройМонтаж»"].source == "bitrix"
        kid = k.id
    # renamed here → in the CRM; renamed in the CRM → here, the old name stays a spelling
    entities.save(kid, name="Криница-Минск", kind="client", unp="190000001", address="Минск",
                  contacts=cards["Криница"].contacts)
    bitrix_sync.sync()
    assert portal.companies["1"]["TITLE"] == "Криница-Минск"
    portal.edit_company("1", TITLE="ОАО «Криница-Минск Холдинг»")
    bitrix_sync.sync()
    with SessionLocal() as s:
        k = s.get(Entity, kid)
        assert k.name == "ОАО «Криница-Минск Холдинг»" and "Криница-Минск" in k.aliases
    # the contact person moved away: out of the block
    portal.contacts["70"].update(COMPANY_ID="2", DATE_MODIFY=portal.tick())
    bitrix_sync.sync()
    with SessionLocal() as s:
        assert "Петров" not in s.get(Entity, kid).contacts
        assert "Петров Иван" in s.query(Entity).filter_by(name="ООО «СтройМонтаж»").one().contacts
    # a new client card here → a company of the CRM with its УНП, phone and mail
    with SessionLocal() as s:
        s.add(Entity(kind="client", name="ЧУП «Новый заказчик»", aliases=[], unp="191234567",
                     contacts="тел. +375 29 123-45-67, zakaz@new.by", source="manual"))
        s.add(Entity(kind="object", name="Объект на Притыцкого", aliases=[], source="manual"))
        s.commit()
    bitrix_sync.sync()
    bitrix_sync.sync()
    added = [p["fields"] for m, p in portal.calls if m == "crm.company.add"]
    assert len(added) == 1 and added[0]["TITLE"] == "ЧУП «Новый заказчик»" and added[0]["COMPANY_TYPE"] == "CUSTOMER"
    assert added[0]["PHONE"][0]["VALUE"] == "+375 29 123-45-67" and added[0]["EMAIL"][0]["VALUE"] == "zakaz@new.by"
    assert any(r["RQ_INN"] == "191234567" for r in portal.requisites.values())


def test_the_block_of_the_crm_is_replaced_and_the_rest_of_the_contacts_kept():
    text = bitrix_sync.with_block("офис: 2 этаж", bitrix_sync.block({"phones": ["+1"], "people": {}}))
    assert text == "офис: 2 этаж\n\n[Битрикс24]\nТел.: +1\n[/Битрикс24]"
    again = bitrix_sync.with_block(text, bitrix_sync.block({"emails": ["a@b.by"]}))
    assert again == "офис: 2 этаж\n\n[Битрикс24]\nПочта: a@b.by\n[/Битрикс24]"
    assert bitrix_sync.with_block(again, "") == "офис: 2 этаж"
    long = bitrix_sync.with_block("x" * 3000, bitrix_sync.block({"phones": ["+2"]}))
    assert len(long) <= 2000 and long.endswith("[/Битрикс24]")


def test_a_refused_webhook_stops_the_run_and_is_shown(portal, monkeypatch):
    anna()
    bitrix_sync.switched({"bitrix_enabled": True, "bitrix_sync_tasks": True})

    def refused(method, params):
        raise bitrix.BitrixError(f"{method}: REST is available only by subscription")
    monkeypatch.setattr(bitrix, "call_full", refused)
    state = bitrix_sync.sync()
    assert state["ok"] is False and "subscription" in state["error"]
    assert settings_store.get("bitrix_state")["log"][0]["text"].startswith("ошибка")


def test_the_page_saves_the_webhook_the_switches_and_the_matching(client, portal):
    settings_store.set_many({"bitrix_webhook_url": ""})
    with SessionLocal() as s:
        s.add(Employee(name="Анна Смирнова", email="anna@firm.by"))
        s.commit()
    page = client.get("/integrations")
    assert page.status_code == 200 and "Битрикс24" in page.text and "Google Drive" in page.text
    bad = client.post("/integrations/bitrix", data={"webhook": "https://example.com/hook", "present": "1"},
                      follow_redirects=False)
    assert "error=" in bad.headers["location"] and not config.bitrix_url()
    good = client.post("/integrations/bitrix", data={"webhook": "https://firm.bitrix24.by/rest/1/abcdef123/",
                                                     "present": "1", "bitrix_enabled": "on", "bitrix_sync_tasks": "on"},
                       follow_redirects=False)
    assert "msg=" in good.headers["location"]
    assert config.bitrix_url() == "https://firm.bitrix24.by/rest/1/abcdef123"
    assert settings_store.get("bitrix_enabled") is True and settings_store.get("bitrix_sync_tasks") is True
    assert settings_store.get("bitrix_import_tasks") is False
    assert settings_store.get("bitrix_state").get("create_since")
    assert "abcdef123" not in client.get("/integrations").text  # the webhook is never shown
    client.post("/integrations/bitrix/match")
    with SessionLocal() as s:
        e = s.query(Employee).one()
        assert e.bitrix_user_id == "7"
        eid = e.id
    page = client.get("/integrations")
    assert "Анна Смирнова · anna@firm.by (ID 7)" in page.text
    client.post("/integrations/bitrix/people", data={f"emp_{eid}": "8"})
    with SessionLocal() as s:
        assert s.get(Employee, eid).bitrix_user_id == "8"
    synced = client.post("/integrations/bitrix/sync", follow_redirects=False)
    assert "msg=" in synced.headers["location"]
    client.post("/integrations/bitrix/forget")
    assert not config.bitrix_url() and settings_store.get("bitrix_enabled") is False
