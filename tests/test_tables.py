"""№27 «Сводные таблицы»: tasks, cases and meetings with the filters and the pivots by people, objects and deadlines —
on the page and as one Excel workbook (written by hand: it must open in Excel, LibreOffice and Google Sheets)."""
import datetime as dt
import io
import re
import xml.etree.ElementTree as ET
import zipfile

import pytest

from app import config, tables
from app.db import (Case, CaseWait, Department, Employee, EmployeeDepartment, Entity, EntityLink, MailMessage, Meeting,
                    SessionLocal, Task)

TODAY = dt.date(2026, 3, 11)  # a Wednesday
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def at(day, hour=9):
    return dt.datetime.combine(day, dt.time(hour), tzinfo=dt.timezone.utc)


@pytest.fixture
def company(monkeypatch):
    monkeypatch.setattr(tables, "today", lambda: TODAY)
    with SessionLocal() as s:
        sales = Department(name="Продажи")
        s.add(sales)
        s.flush()
        anna, boris, vera = Employee(name="Анна Смирнова"), Employee(name="Борис Котов", department="Склад"), Employee(name="Вера Иванова")
        s.add_all([anna, boris, vera])
        s.flush()
        s.add(EmployeeDepartment(employee_id=anna.id, department_id=sales.id))
        plan = Meeting(title="Планёрка", meeting_date=dt.date(2026, 3, 2), status="done", duration_sec=1800,
                       source="ticnote", participants={"people": [{"employee_id": anna.id}, {"guest": "Подрядчик"}],
                                                       "source": "manual"})
        old = Meeting(title="Февральская", meeting_date=dt.date(2026, 2, 10), status="done", source="web")
        s.add_all([plan, old])
        s.flush()
        rows = {
            "late": Task(meeting_id=plan.id, title="Отчёт <продажи> & план", assignee_id=anna.id, status="sent",
                         deadline=dt.date(2026, 3, 6), created_at=at(dt.date(2026, 3, 2))),
            "soon": Task(meeting_id=plan.id, title="Счёт клиенту", assignee_id=anna.id, status="in_progress",
                         deadline=dt.date(2026, 3, 12), created_at=at(dt.date(2026, 3, 2))),
            "ontime": Task(title="Отгрузка", assignee_id=boris.id, status="sent", deadline=dt.date(2026, 3, 9),
                           created_at=at(dt.date(2026, 3, 3)), source_quote="Письмо от ООО «Криница»: «Отгрузка»"),
            "nodate": Task(title="Разобрать склад", assignee_id=boris.id, status="sent", created_at=at(dt.date(2026, 3, 4))),
            "feb": Task(meeting_id=old.id, title="Февральская задача", assignee_id=vera.id, status="sent",
                        deadline=dt.date(2026, 2, 20), created_at=at(dt.date(2026, 2, 10))),
            "next": Task(title="Подготовить договор", assignee_id=vera.id, status="sent", deadline=dt.date(2026, 3, 18),
                         created_at=at(dt.date(2026, 3, 10)), source_quote="Документ «Договор.pdf»"),
        }
        s.add_all(rows.values())
        s.flush()
        rows["ontime"].status = "done"
        rows["ontime"].done_at = at(dt.date(2026, 3, 8))
        client = Entity(name="Криница", kind="client")
        s.add(client)
        s.flush()
        s.add_all([EntityLink(entity_id=client.id, source_type="task", source_id=rows["late"].id),
                   EntityLink(entity_id=client.id, source_type="task", source_id=rows["ontime"].id),
                   EntityLink(entity_id=client.id, source_type="meeting", source_id=plan.id, created_at=at(dt.date(2026, 3, 2)))])
        s.add(MailMessage(mailbox="box", subject="Счёт", entity_id=client.id, sent_at=at(dt.date(2026, 3, 5))))
        case = Case(title="Спросить Анну про отчёт", kind="ask", status="waiting", task_id=rows["late"].id,
                    created_at=at(dt.date(2026, 3, 7)))
        s.add(case)
        s.flush()
        s.add(CaseWait(case_id=case.id, step_id=1, employee_id=anna.id, status="open"))
        s.add(Case(title="Закрытое дело", kind="mail", status="done", created_at=at(dt.date(2026, 3, 1)),
                   closed_at=at(dt.date(2026, 3, 3))))
        s.commit()
        return {"anna": anna.id, "boris": boris.id, "vera": vera.id, "sales": sales.id, "client": client.id,
                **{k: v.id for k, v in rows.items()}}


def collect(**params):
    with SessionLocal() as s:
        return tables.collect(s, tables.filters(params))


def test_the_periods():
    assert tables.period("week", at=TODAY) == (dt.date(2026, 3, 9), dt.date(2026, 3, 15))
    assert tables.period("month", at=TODAY) == (dt.date(2026, 3, 1), dt.date(2026, 3, 31))
    assert tables.period("prev_month", at=TODAY) == (dt.date(2026, 2, 1), dt.date(2026, 2, 28))
    assert tables.period("quarter", at=dt.date(2026, 11, 5)) == (dt.date(2026, 10, 1), dt.date(2026, 12, 31))
    assert tables.period("year", at=TODAY) == (dt.date(2026, 1, 1), dt.date(2026, 12, 31))
    assert tables.period("all") == (None, None)
    assert tables.period("custom", "2026-03-20", "2026-03-01") == (dt.date(2026, 3, 1), dt.date(2026, 3, 20))
    assert tables.filters({"period": "nonsense", "state": "x", "employee": "abc"})["period"] == "month"


def test_the_tasks_of_the_period_and_the_filters(company):
    data = collect(period="month")
    titles = {t["title"] for t in data["tasks"]}
    assert "Февральская задача" not in titles and len(titles) == 5
    late = next(t for t in data["tasks"] if t["id"] == company["late"])
    assert late["overdue"] and late["late_days"] == 5 and late["department"] == "Продажи"
    assert late["source"] == "Совещание «Планёрка» 02.03.2026" and late["entities"] == ["Криница"]
    assert late["case"] == "ждёт ответа"
    done = next(t for t in data["tasks"] if t["id"] == company["ontime"])
    assert done["done"] and done["on_time"] is True and done["source"] == "Письмо" and done["done_on"] == dt.date(2026, 3, 8)
    assert {t["title"] for t in collect(period="all")["tasks"]} >= {"Февральская задача"}
    assert [t["title"] for t in collect(period="month", employee=str(company["anna"]))["tasks"]] == [
        "Отчёт <продажи> & план", "Счёт клиенту"]
    assert {t["person"] for t in collect(period="month", department=str(company["sales"]))["tasks"]} == {"Анна Смирнова"}
    assert {t["title"] for t in collect(period="month", entity=str(company["client"]))["tasks"]} == {
        "Отчёт <продажи> & план", "Отгрузка"}
    assert [t["title"] for t in collect(period="month", state="overdue")["tasks"]] == ["Отчёт <продажи> & план"]
    assert [t["title"] for t in collect(period="month", state="done")["tasks"]] == ["Отгрузка"]
    assert len(collect(period="month", state="open")["tasks"]) == 4
    # a task of February still open and due in the period «прошлый месяц»
    assert [t["title"] for t in collect(period="prev_month")["tasks"]] == ["Февральская задача"]


def test_the_cases_and_the_meetings(company):
    data = collect(period="month")
    assert [(c["title"], c["waiting_for"], c["open"]) for c in data["cases"]] == [
        ("Спросить Анну про отчёт", "Анна Смирнова", True), ("Закрытое дело", "", False)]
    assert [m["title"] for m in data["meetings"]] == ["Планёрка"]
    plan = data["meetings"][0]
    assert plan["people"] == 2 and plan["present"] == "Анна Смирнова, Подрядчик" and plan["tasks"] == 2
    assert plan["overdue"] == 1 and plan["minutes"] == 30 and plan["entities"] == "Криница"
    assert [c["title"] for c in collect(period="month", employee=str(company["anna"]))["cases"]] == ["Спросить Анну про отчёт"]


def test_the_pivots(company):
    data = collect(period="month")
    people = {g["person"]: g for g in tables.by_people(data)}
    assert people["Анна Смирнова"]["overdue"] == 1 and people["Анна Смирнова"]["avg_late"] == 5.0
    assert people["Анна Смирнова"]["nearest"] == dt.date(2026, 3, 12)
    assert people["Борис Котов"]["share"] == 1.0 and people["Борис Котов"]["done"] == 1
    rows = {r["person"]: r for r in tables.by_deadlines(data)}
    assert rows["Анна Смирнова"]["Просрочено"] == 1 and rows["Анна Смирнова"]["Завтра"] == 1
    assert rows["Борис Котов"]["Без срока"] == 1 and rows["Вера Иванова"]["Следующая неделя"] == 1
    assert rows["Итого"]["total"] == 4
    with SessionLocal() as s:
        places = tables.by_entities(s, data, tables.filters({"period": "month"}))
    assert [(p["name"], p["tasks"], p["open"], p["overdue"], p["done"], p["meetings"], p["letters"]) for p in places] == [
        ("Криница", 2, 1, 1, 1, 1, 1)]
    assert tables.bucket(dt.date(2026, 3, 15), TODAY) == "До конца недели" and tables.bucket(dt.date(2026, 4, 1), TODAY) == "Позже"


def _book(body: bytes) -> dict:
    z = zipfile.ZipFile(io.BytesIO(body))
    assert z.testzip() is None
    for name in z.namelist():
        if name.endswith((".xml", ".rels")):
            ET.fromstring(z.read(name))  # every part is well-formed XML
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    names = [el.get("name") for el in workbook.find("m:sheets", NS)]
    sheets = {}
    for i, name in enumerate(names, start=1):
        root = ET.fromstring(z.read(f"xl/worksheets/sheet{i}.xml"))
        rows = []
        for row in root.find("m:sheetData", NS):
            cells = {}
            for c in row:
                ref = re.match(r"[A-Z]+", c.get("r")).group(0)
                text = c.find("m:is/m:t", NS)
                value = c.find("m:v", NS)
                formula = c.find("m:f", NS)
                cells[ref] = (text.text if text is not None else value.text if value is not None else None,
                              c.get("s"), formula.text if formula is not None else None)
            rows.append(cells)
        sheets[name] = rows
    return sheets


def test_the_workbook_opens_and_keeps_the_values(company, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "https://sysai.example")
    with SessionLocal() as s:
        body = tables.export(s, tables.filters({"period": "month"}))
    book = _book(body)
    assert list(book) == ["Сводка", "Задачи", "Дела", "Совещания", "По людям", "По объектам", "По срокам"]
    head = book["Задачи"][0]
    assert head["A"][0] == "№" and head["B"][0] == "Задача" and head["A"][1] == "1"  # the bold header style
    late = next(r for r in book["Задачи"][1:] if r["B"][0] == "Отчёт <продажи> & план")  # escaped and read back
    assert late["F"][0] == str((dt.date(2026, 3, 6) - dt.date(1899, 12, 30)).days) and late["F"][1] == "2"  # a date
    assert late["N"][2] == f'HYPERLINK("https://sysai.example/meetings/{1}","Открыть")'
    total = book["По срокам"][-1]
    assert total["A"][0] == "Итого" and total["A"][1] == "5"
    summary = {r["A"][0]: r.get("B", (None,))[0] for r in book["Сводка"] if "A" in r}
    assert summary["Задач"] == "5" and summary["Из них просрочено"] == "1" and summary["Отбор"].startswith("Этот месяц")


def test_the_page_and_the_button(client, company):
    page = client.get("/tables?period=month").text
    assert "Скачать Excel" in page and "По людям" in page and "Анна Смирнова" in page and "Криница" in page
    assert 'href="/tables/export.xlsx?period=month"' in page
    answer = client.get(f"/tables/export.xlsx?period=month&employee={company['anna']}")
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert answer.headers["content-disposition"].startswith('attachment; filename="sysai-tables-')
    tasks = _book(answer.content)["Задачи"]
    assert [r["B"][0] for r in tasks[1:]] == ["Отчёт <продажи> & план", "Счёт клиенту"]


def test_an_empty_company_still_gets_a_workbook(client):
    answer = client.get("/tables/export.xlsx?period=all")
    book = _book(answer.content)
    assert len(book["Задачи"]) == 1 and len(book["По срокам"]) == 1
    assert "Задач по этому отбору нет" in client.get("/tables").text


def test_sheet_names_and_columns():
    used = set()
    assert tables._sheet_name("Отчёт: продажи/план [март]", used) == "Отчёт  продажи план  март"
    assert tables._sheet_name("Отчёт  продажи план  март", used).endswith(" 2")
    assert [tables._col(i) for i in (0, 25, 26, 27, 701, 702)] == ["A", "Z", "AA", "AB", "ZZ", "AAA"]
