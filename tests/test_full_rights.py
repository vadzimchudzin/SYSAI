"""People with full rights (director, reviewer, full access) are recognised and marked red in the directory."""
from app import agent, settings_store
from app.db import Employee, SessionLocal


def add(**kw):
    with SessionLocal() as s:
        e = Employee(**kw)
        s.add(e)
        s.commit()
        return e.id


def test_rights_by_username_id_and_phone():
    settings_store.set_many({"approver_chat_ids": ["@omgmzf"], "full_access_chat_ids": ["5854113570"],
                             "director_chat_ids": ["+375291391080"]})
    omg = Employee(name="OMG", telegram_chat_id="@OmgMzf")
    vadim = Employee(name="Вадим", telegram_chat_id="@someone", telegram_user_id="5854113570")
    boss = Employee(name="Директор", telegram_chat_id="+375 29 139-10-80")
    other = Employee(name="Аня", telegram_chat_id="@anya_work")
    assert agent.rights_of(omg) == "reviewer"
    assert agent.rights_of(vadim) == "full"
    assert agent.rights_of(boss) == "director"
    assert agent.rights_of(other) == ""


def test_directory_marks_full_rights_red(client):
    settings_store.set_many({"approver_chat_ids": ["@omgmzf"]})
    add(name="OMG", position="Разработчик", telegram_chat_id="@omgmzf", telegram_user_id="622744825")
    add(name="Аня", position="Бухгалтер", telegram_chat_id="@anya_work")
    html = client.get("/employees").text
    cards = {c.split("<b>")[1].split("</b>")[0]: c for c in html.split('<article class="employee-card')[1:]}
    assert "full-rights" in cards["OMG"].split(">")[0] and "Проверяющий" in cards["OMG"]
    assert "full-rights" not in cards["Аня"].split(">")[0] and "pill red" not in cards["Аня"].split("employee-details")[0]
