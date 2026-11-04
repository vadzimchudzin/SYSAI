"""Отделы и папки секретаря: the company structure is the default — the sorting names the department of a letter by
its meaning, the letter goes to the one responsible for the mail of that department (in Telegram) and is moved into
the folder of the department in the box itself (Yandex 360: «|» between the parts of a name, UTF-7 names, MOVE).
A letter that waits for a decision stays in the inbox; passed on, it follows; «не моё» brings it back."""
import datetime as dt
import types

import pytest

from app import mail, mail_folders, mail_route, settings_store
from app.db import Department, Employee, EmployeeDepartment, MailMessage, SessionLocal, now
from test_ai_mode import FakeBackend, incoming, tgacc  # noqa: F401  (fixtures)
from test_mail import FakeBox, letter, rows  # noqa: F401
from test_mail_route import _data, mm  # noqa: F401  (fixtures)


class FolderBox(FakeBox):
    """A mailbox that also makes folders and moves letters (records what it was asked)."""

    def __init__(self):
        super().__init__()
        self.placed, self.created, self.answer, self.refuse = [], [], {}, ""

    def place(self, moves, create):
        if self.refuse:
            raise mail.MailError(self.refuse)
        self.placed.append([dict(m) for m in moves])
        made = ["/".join(p) for p in create]
        self.created += made
        return {"delimiter": "|", "created": made, "moved": {m["id"]: self.answer.get(m["id"], "ok") for m in moves}}


@pytest.fixture
def fbox(monkeypatch):
    fake = FolderBox()
    monkeypatch.setattr(mail, "_box", lambda cfg: fake)
    settings_store.set_many({"mail_enabled": True, "mail_service": "yandex", "mail_address": "office@stroydom.by",
                             "mail_password": mail.seal("app-pass-123"), "company_name": "СтройДом"})
    return fake


def org():
    """Бухгалтерия (Анна — head, Ольга) and Снабжение (Игорь — head, Борис without Telegram)."""
    with SessionLocal() as s:
        buh = Department(name="Бухгалтерия", description="счета, оплаты, акты сверки, налоги")
        sn = Department(name="Снабжение", description="закупки материалов, поставщики, доставка на объекты")
        anna = Employee(name="Анна Орлова", position="главный бухгалтер", telegram_chat_id="@anna")
        olga = Employee(name="Ольга Белова", position="бухгалтер", telegram_chat_id="@olga")
        igor = Employee(name="Игорь Сидоров", position="снабженец", telegram_chat_id="@igor")
        boris = Employee(name="Борис Ким", position="кладовщик")
        s.add_all([buh, sn, anna, olga, igor, boris])
        s.flush()
        s.add_all([EmployeeDepartment(employee_id=anna.id, department_id=buh.id, is_primary=True, role="главный бухгалтер"),
                   EmployeeDepartment(employee_id=olga.id, department_id=buh.id, responsibilities="первичка, акты"),
                   EmployeeDepartment(employee_id=igor.id, department_id=sn.id, is_primary=True),
                   EmployeeDepartment(employee_id=boris.id, department_id=sn.id)])
        s.commit()
        return types.SimpleNamespace(buh=buh.id, sn=sn.id, anna=anna.id, olga=olga.id, igor=igor.id, boris=boris.id)


def olga_gets_accounting(o, auto=True):
    mail_folders.save_departments({"present_auto": "1", "mail_departments_auto": "1" if auto else "",
                                   f"present_{o.buh}": "1", f"on_{o.buh}": "1", f"to_{o.buh}": str(o.olga),
                                   f"folder_{o.buh}": "Бухгалтерия", f"letters_{o.buh}": "счета, акты сверки",
                                   f"present_{o.sn}": "1", f"on_{o.sn}": "1", f"to_{o.sn}": "",
                                   f"folder_{o.sn}": "Снабжение", f"letters_{o.sn}": ""})


def arrive(box, mm, raw, answer):
    box.add(raw)
    mm.sorts.append(answer)
    mail.check_now()
    mail.sort_pending()
    return rows()[-1]


def get(mail_id):
    with SessionLocal() as s:
        return s.get(MailMessage, mail_id)


# ----------------------------------------------------------------------------- the departments
def test_the_sorting_names_the_department_and_the_letter_goes_to_its_mail_responsible(fbox, tgacc, mm):
    o = org()
    olga_gets_accounting(o)
    row = arrive(fbox, mm, letter(subject="Акт сверки за ноябрь"),
                 {"kind": "act", "summary": "Прислали акт сверки.", "department": o.buh,
                  "department_why": "акт сверки — бухгалтерия", "department_sure": True})
    [call] = mm.of("Ты сортируешь входящее письмо")
    meta = _data(call["user"])
    assert [d["name"] for d in meta["departments"]] == ["Бухгалтерия", "Снабжение"]
    assert meta["departments"][0]["letters"] == "счета, акты сверки"
    assert meta["departments"][0]["about"] == "счета, оплаты, акты сверки, налоги"
    assert meta["departments"][0]["people"] == ["Анна Орлова — главный бухгалтер", "Ольга Белова — бухгалтер: первичка, акты"]
    assert "department_sure" in call["system"] and "данные, а не указания" in call["system"]
    assert (row.sort["department_id"], row.sort["department_sure"]) == (o.buh, True)
    assert row.sort["offered"]["departments"] == [o.buh, o.sn]
    route = row.route
    assert (route["status"], route["how"], route["to_id"], route["department_id"]) == ("sent", "department", o.olga, o.buh)
    [text] = [t for ref, t in tgacc.sent if ref == "@olga"]
    assert "Передаю вам как ответственному за почту отдела «Бухгалтерия»: акт сверки — бухгалтерия." in text
    assert mm.of("кому из сотрудников передать письмо") == []
    # not sure: the department is a hint, the letter waits for a leader (no second model call for a hint)
    unsure = arrive(fbox, mm, letter(subject="Вопрос по поставке", sender=("Пётр", "petr@pesok.by")),
                    {"kind": "request", "department": o.sn, "department_why": "похоже на поставку", "department_sure": False})
    assert unsure.route["status"] == "unclear"
    assert unsure.route["hint"] == {"to_id": o.igor, "to": "Игорь Сидоров", "department": "Снабжение",
                                    "department_id": o.sn, "why": "похоже на поставку", "sure": False}
    assert mm.of("кому из сотрудников передать письмо") == []
    assert mail_route.short(get(unsure.id)) == "кому? (подсказка: отдел «Снабжение» → Игорь Сидоров)"
    with SessionLocal() as s:
        assert {"id": unsure.id, "subject": "Вопрос по поставке", "from": "Пётр",
                "hint": "отдел «Снабжение» → Игорь Сидоров", "why": "нет правила для такого письма (чей отдел — секретарь "
                                                                   "не уверен)"} in mail_route.waiting(s)
    # a department that was not offered is not taken
    odd = arrive(fbox, mm, letter(subject="Привет", sender=("Оля", "olya@gmail.com")),
                 {"kind": "other", "department": 999, "department_sure": True})
    assert (odd.sort["department_id"], odd.sort["department_sure"]) == (None, False) and odd.route["status"] == "unclear"
    # «сразу передавать» off: a sure department waits too, as a sure hint
    settings_store.set_many({"mail_departments_auto": False})
    held = arrive(fbox, mm, letter(subject="Счёт № 7", sender=("Склад", "sklad@beton.by")),
                  {"kind": "invoice", "department": o.buh, "department_sure": True})
    assert held.route["status"] == "unclear" and (held.route["hint"]["to_id"], held.route["hint"]["sure"]) == (o.olga, True)


def test_a_rule_to_a_department_and_who_is_responsible_for_its_mail(fbox, tgacc, mm):
    o = org()
    with SessionLocal() as s:
        assert mail_folders.responsible(s, s.get(Department, o.sn)).id == o.igor  # the head by default
        labels = {t["value"]: t["label"] for t in mail_route.targets(s)}
    assert labels[f"d:{o.buh}"] == "Отдел «Бухгалтерия» (ответственному за почту: Анна Орлова)"
    with pytest.raises(mail_folders.FolderError, match="нет Telegram"):
        mail_folders.save_departments({f"present_{o.sn}": "1", f"on_{o.sn}": "1", f"to_{o.sn}": str(o.boris),
                                       f"folder_{o.sn}": "Снабжение"})
    olga_gets_accounting(o)
    mail_route.save_rule(kind="claim", to=f"d:{o.buh}")
    claim = arrive(fbox, mm, letter(subject="Претензия по оплате"), {"kind": "claim"})
    assert (claim.route["how"], claim.route["to_id"], claim.route["department_id"]) == ("rule", o.olga, o.buh)
    # somebody of another department may receive the mail; when that person leaves, the head gets it again
    mail_folders.save_departments({f"present_{o.sn}": "1", f"on_{o.sn}": "1", f"to_{o.sn}": str(o.anna),
                                   f"folder_{o.sn}": "Снабжение"})
    with SessionLocal() as s:
        assert mail_folders.responsible(s, s.get(Department, o.sn)).id == o.anna
        s.get(Employee, o.anna).active = False
        s.commit()
        assert mail_folders.responsible(s, s.get(Department, o.sn)).id == o.igor
        [view] = [d for d in mail_folders.departments(s) if d["id"] == o.sn]
    assert view["person"] == "Игорь Сидоров" and "больше нет" in view["warn"]
    # nobody with Telegram in a department: the letter waits, the page says why
    mail_folders.save_departments({f"present_{o.sn}": "1", f"on_{o.sn}": "1", f"to_{o.sn}": "", f"folder_{o.sn}": ""})
    with SessionLocal() as s:
        s.get(Employee, o.igor).telegram_chat_id = ""
        s.commit()
    lost = arrive(fbox, mm, letter(subject="Доставка щебня", sender=("Пётр", "petr@pesok.by")),
                  {"kind": "request", "department": o.sn, "department_sure": True})
    assert lost.route["status"] == "no_contact" and "нет ответственного за почту с Telegram" in lost.route["why"]
    # a department switched off is not offered to the sorting
    mail_folders.save_departments({f"present_{o.sn}": "1", f"to_{o.sn}": "", f"folder_{o.sn}": ""})
    with SessionLocal() as s:
        assert [d["name"] for d in mail_folders.for_sorting(s)] == ["Бухгалтерия"]


# ----------------------------------------------------------------------------- the folders of the box
def test_sorted_letters_are_moved_into_the_folders_and_follow_their_route(client, fbox, tgacc, mm):
    o = org()
    olga_gets_accounting(o)
    act = arrive(fbox, mm, letter(subject="Акт сверки"), {"kind": "act", "department": o.buh, "department_sure": True})
    news = arrive(fbox, mm, letter(subject="Вебинар", sender=("Сервис", "news@service.by")),
                  {"kind": "newsletter", "department": o.sn, "department_sure": True})
    wait = arrive(fbox, mm, letter(subject="Вопрос", sender=("Пётр", "petr@pesok.by")),
                  {"kind": "request", "department": o.sn, "department_sure": False})
    assert mail_folders.place_pending() == 0 and fbox.placed == []  # switched off: the box is left alone
    settings_store.set_many({"mail_folders_on": True})
    assert mail_folders.place_pending() == 2
    [moves] = fbox.placed
    assert {m["id"]: (m["source"], m["target"]) for m in moves} == {act.id: ([], ["Бухгалтерия"]),
                                                                   news.id: ([], ["Рассылки"])}
    assert {(m["uid"], m["uidvalidity"]) for m in moves} == {(act.uid, "777"), (news.uid, "777")}
    assert (get(act.id).place["folder"], get(act.id).place["why"]) == ("Бухгалтерия", "передано: отдел «Бухгалтерия»")
    assert get(news.id).place["why"] == "особая папка: Рассылка"
    assert (get(wait.id).place["folder"], get(wait.id).place["status"]) == ("", "ok")  # waits in the inbox
    assert mail_folders.place_pending() == 0 and len(fbox.placed) == 1  # nothing changed — the box is not touched
    page = client.get("/mail").text
    assert "📁 Бухгалтерия" in page and "📁 Рассылки" in page
    assert "передано: отдел «Бухгалтерия»" in client.get(f"/mail/{act.id}").text
    # passed on by hand: the letter follows into the folder of the person's department
    mail_route.pass_on(wait.id, o.igor, by="Директор")
    assert mail_folders.place_pending() == 1
    assert [(m["id"], m["source"], m["target"]) for m in fbox.placed[-1]] == [(wait.id, [], ["Снабжение"])]
    # «не моё»: back into the inbox
    mail_route.pass_on(act.id, None, by="Ольга Белова")
    assert mail_folders.place_pending() == 1
    assert [(m["id"], m["source"], m["target"]) for m in fbox.placed[-1]] == [(act.id, ["Бухгалтерия"], [])]
    assert get(act.id).place["folder"] == ""
    # the folders made inside «Входящие»: the letters move there too
    assert mail_folders.save_settings({"mail_folders_on": "1", "mail_folders_root": "Входящие"}).startswith(
        "папки в ящике готовы: 3")
    assert fbox.created == ["INBOX/Бухгалтерия", "INBOX/Снабжение", "INBOX/Рассылки"]
    assert mail_folders.place_pending() == 2
    assert sorted((m["source"], m["target"]) for m in fbox.placed[-1]) == [(["Рассылки"], ["INBOX", "Рассылки"]),
                                                                          (["Снабжение"], ["INBOX", "Снабжение"])]
    assert "📁 Входящие / Снабжение" in client.get("/mail").text
    # a letter of a department without a folder stays where it is
    mail_folders.save_departments({f"present_{o.sn}": "1", f"on_{o.sn}": "1", f"to_{o.sn}": "", f"folder_{o.sn}": ""})
    mail_folders.place_pending()
    assert [(m["id"], m["target"]) for m in fbox.placed[-1]] == [(wait.id, [])]


def test_a_refusing_box_and_a_letter_that_cannot_be_moved_are_tried_again_later(fbox, tgacc, mm):
    o = org()
    olga_gets_accounting(o)
    settings_store.set_many({"mail_folders_on": True})
    a = arrive(fbox, mm, letter(subject="Счёт 1"), {"kind": "invoice", "department": o.buh, "department_sure": True})
    b = arrive(fbox, mm, letter(subject="Счёт 2"), {"kind": "invoice", "department": o.buh, "department_sure": True})
    fbox.refuse = "сервер не умеет перекладывать письма (нет команд MOVE и UIDPLUS)"
    assert mail_folders.place_pending() == 2
    state = settings_store.get("mail_folders_state")
    assert state["ok"] is False and "MOVE" in state["error"] and get(a.id).place is None
    fbox.refuse = ""
    assert mail_folders.place_pending() == 0 and fbox.placed == []  # not knocked on again for 15 minutes
    fbox.answer = {a.id: "gone", b.id: "error: NO [CANNOT] нельзя"}
    assert mail_folders.place_pending(force=True) == 2
    assert get(a.id).place["status"] == "gone" and "переложили или удалили" in get(a.id).place["error"]
    place = get(b.id).place
    assert (place["status"], place["tries"], place["want"], place["error"], place.get("folder")) == (
        "error", 1, "Бухгалтерия", "NO [CANNOT] нельзя", "")
    assert mail_folders.place_pending() == 0  # gone — never again; the error — after a while
    with SessionLocal() as s:
        row = s.get(MailMessage, b.id)
        row.place = {**row.place, "at": mail._iso(now() - dt.timedelta(minutes=16))}
        s.commit()
    fbox.answer = {}
    assert mail_folders.place_pending() == 1
    assert (get(b.id).place["status"], get(b.id).place["folder"]) == ("ok", "Бухгалтерия")
    state = settings_store.get("mail_folders_state")
    assert state["ok"] is True and state["moved"] == 1 and state["delimiter"] == "|"


def test_the_labels_go_into_the_box_before_the_letter_moves(fbox, tgacc, mm):
    o = org()
    olga_gets_accounting(o)
    settings_store.set_many({"mail_folders_on": True, "mail_labels": True})
    a = arrive(fbox, mm, letter(subject="Счёт"), {"kind": "invoice", "department": o.buh, "department_sure": True})
    assert mail_folders.place_pending() == 0  # the labels are put by the UID of the inbox — first
    assert mail.label_pending() == 1 and fbox.labels
    assert mail_folders.place_pending() == 1
    with SessionLocal() as s:
        row = s.get(MailMessage, a.id)
        row.sort = {k: v for k, v in row.sort.items() if k != "box"}
        s.commit()
    assert mail.label_pending() == 0  # moved: not labelled again in the inbox


def test_the_names_of_the_folders_are_checked():
    assert mail_folders.clean_path(" Отделы /  Бухгалтерия ") == "Отделы/Бухгалтерия" and mail_folders.clean_path("") == ""
    for bad in ("Входящие", "Spam/Старое", "a*b", 'a"b', "a|b", "x" * 101, "a/b/c/d/e"):
        with pytest.raises(mail_folders.FolderError):
            mail_folders.clean_path(bad)
    assert mail_folders.clean_path("Входящие/x", top=False) == "Входящие/x"
    assert mail_folders.clean_root("входящие") == "INBOX" and mail_folders.clean_root("Входящие / Отделы") == "INBOX/Отделы"
    assert mail_folders.default_folder("Снабжение/логистика") == "Снабжение-логистика"
    assert mail_folders.shown("INBOX/Бухгалтерия") == "Входящие / Бухгалтерия"
    for name in ("Входящие", "Отделы|Бухгалтерия", "A&B", "Счёт/Акт — ноябрь", "日本語"):
        assert mail.imap_utf7_decode(mail.imap_utf7(name)) == name
    data = [b'(\\HasNoChildren) "|" "INBOX"', f'(\\HasChildren) "|" "{mail.imap_utf7("Отделы")}"'.encode(),
            b'(\\Noselect) NIL "Flat"', (b'(\\HasNoChildren) "|" {6}', b'Lit|er'), b"", b"garbage"]
    assert mail.list_entries(data) == [("\\HasNoChildren", "|", "INBOX"), ("\\HasChildren", "|", "Отделы"),
                                       ("\\Noselect", "", "Flat"), ("\\HasNoChildren", "|", "Lit|er")]


def test_the_departments_and_the_folders_are_set_on_the_page(client, fbox):
    o = org()
    page = client.get("/mail").text
    assert 'id="departments"' in page and 'id="folders"' in page and "руководитель — Анна Орлова" in page
    assert "Борис Ким — нет Telegram" in page
    assert 'value="yandex" selected' in page and "admin.yandex.ru" in page
    r = client.post("/mail/departments", data={"present_auto": "1", f"present_{o.buh}": "1", f"on_{o.buh}": "1",
                                               f"to_{o.buh}": str(o.olga), f"folder_{o.buh}": "Финансы / Бухгалтерия",
                                               f"letters_{o.buh}": "  счета,   акты ", f"present_{o.sn}": "1",
                                               f"to_{o.sn}": "", f"folder_{o.sn}": "Снабжение"}, follow_redirects=False)
    assert r.headers["location"] == "/mail?msg=departments#departments"
    conf = settings_store.get("mail_departments")
    assert conf[str(o.buh)] == {"on": True, "to": o.olga, "folder": "Финансы/Бухгалтерия", "letters": "счета, акты"}
    assert conf[str(o.sn)] == {"on": False, "to": None, "folder": None, "letters": ""}
    assert settings_store.get("mail_departments_auto") is False
    bad = client.post("/mail/departments", data={f"present_{o.buh}": "1", f"folder_{o.buh}": "Спам"}, follow_redirects=False)
    assert "error=" in bad.headers["location"] and bad.headers["location"].endswith("#departments")
    assert settings_store.get("mail_departments")[str(o.buh)]["folder"] == "Финансы/Бухгалтерия"  # nothing saved
    # switched on: the folders are made in the box right away
    r = client.post("/mail/folders", data={"mail_folders_on": "1", "mail_folders_root": ""}, follow_redirects=False)
    assert fbox.created == ["Финансы/Бухгалтерия", "Рассылки"]
    assert "папки в ящике готовы: 2, создано новых: 2" in client.get(r.headers["location"]).text
    assert "error=" in client.post("/mail/folders", data={"mail_folders_root": "Спам"},
                                   follow_redirects=False).headers["location"]
    # the special folders: checked first, moved up, deleted
    client.post("/mail/folder-rules", data={"senders": "nalog.gov.by", "folder": "Налоговая"})
    assert settings_store.get("mail_folder_rules")[-1] == {"kind": "", "senders": "@nalog.gov.by", "keywords": "",
                                                           "folder": "Налоговая"}
    assert "error=" in client.post("/mail/folder-rules", data={"folder": "Всё"}, follow_redirects=False).headers["location"]
    client.post("/mail/folder-rules/1/up")
    assert [x["folder"] for x in settings_store.get("mail_folder_rules")] == ["Налоговая", "Рассылки"]
    page = client.get("/mail").text.replace("<b>", "").replace("</b>", "")
    assert "от @nalog.gov.by → 📁 Налоговая" in page and "Рассылка → 📁 Рассылки" in page
    client.post("/mail/folder-rules/0/delete")
    assert [x["folder"] for x in settings_store.get("mail_folder_rules")] == ["Рассылки"]
    assert "📁 Финансы / Бухгалтерия" in client.get("/mail").text
    r = client.post("/mail/folders/apply", follow_redirects=False)
    assert r.headers["location"] == "/mail?msg=applying#folders"
    r = client.post("/mail/folders/create", follow_redirects=False)
    assert "msg=" in r.headers["location"] and fbox.created[-2:] == ["Финансы/Бухгалтерия", "Рассылки"]


# ----------------------------------------------------------------------------- the IMAP client (as Yandex answers)
class YandexImap:
    """imaplib.IMAP4_SSL as Yandex 360 answers: «|» between the parts of a name, UTF-7 names, MOVE told after login."""
    instances = []
    after_login = ("IMAP4REV1", "UIDPLUS", "MOVE")
    box = {}  # the folders of the box: {name: {uid: Message-ID}} — one box behind every connection; UIDs never reused

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.commands, self.untagged_responses, self.current = [], {}, ""
        self.capabilities = ("IMAP4REV1", "AUTH=PLAIN")
        self.folders = YandexImap.box
        YandexImap.instances.append(self)

    @staticmethod
    def _name(raw):
        assert raw.startswith('"') and raw.endswith('"') and all(ord(ch) < 128 for ch in raw)  # quoted, ASCII (UTF-7)
        return mail.imap_utf7_decode(raw[1:-1])

    def login(self, user, password):
        return "OK", [b"done"]

    def capability(self):
        return "OK", [" ".join(self.after_login).encode()]

    def list(self, directory='""', pattern="*"):
        self.commands.append(("LIST",))
        return "OK", [f'(\\HasNoChildren) "|" "{mail.imap_utf7(n)}"'.encode() for n in self.folders]

    def create(self, raw):
        self.commands.append(("CREATE", self._name(raw)))
        self.folders.setdefault(self._name(raw), {})
        return "OK", [b"CREATE completed"]

    def subscribe(self, raw):
        self.commands.append(("SUBSCRIBE", self._name(raw)))
        return "OK", [b"done"]

    def select(self, raw, readonly=False):
        name = self._name(raw)
        self.commands.append(("EXAMINE" if readonly else "SELECT", name))
        if name not in self.folders:
            return "NO", [b"[NONEXISTENT] no such folder"]
        self.current = name
        self.untagged_responses = {"UIDVALIDITY": [b"4242" if name == "INBOX" else b"9"]}
        return "OK", [str(len(self.folders[name])).encode()]

    def uid(self, command, *args):
        args = tuple(a for a in args if a is not None)
        self.commands.append(("UID " + command,) + args)
        box = self.folders[self.current]
        if command == "SEARCH":
            found = ([u for u in box if u == int(args[1])] if args[0] == "UID"
                     else [u for u, mid in box.items() if '"' + mid + '"' == args[2]])
            return "OK", [" ".join(map(str, found)).encode()]
        if command in ("MOVE", "COPY"):
            target = self._name(args[1])
            if target not in self.folders:
                return "NO", [b"[TRYCREATE] no such folder"]
            self.folders[target][max([100] + [u for f in self.folders.values() for u in f]) + 1] = box[int(args[0])]
            if command == "MOVE":
                del box[int(args[0])]
            return "OK", [b"done"]
        if command == "STORE":
            return "OK", [b"done"]
        if command == "EXPUNGE":
            del box[int(args[0])]
            return "OK", [b"done"]
        return "NO", [b"unknown"]

    def logout(self):
        self.commands.append(("LOGOUT",))


def test_the_imap_client_makes_the_folders_and_moves_the_letters_as_yandex_answers(monkeypatch):
    import imaplib
    YandexImap.instances.clear()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", YandexImap)
    monkeypatch.setattr(YandexImap, "after_login", ("IMAP4REV1", "UIDPLUS", "MOVE"))
    monkeypatch.setattr(YandexImap, "box", {"INBOX": {7: "<a@x>", 8: "<b@x>", 9: "<c@x>"}, "Sent": {}, "Spam": {}})
    cfg = {"address": "office@stroydom.by", "password": "app-pass-123", "imap_host": "imap.yandex.ru", "imap_port": 993,
           "folder": "INBOX"}
    got = mail.ImapBox(cfg).place([{"id": 1, "uid": 7, "uidvalidity": "4242", "message_id": "<a@x>", "source": [],
                                    "target": ["Отделы", "Бухгалтерия"]},
                                   {"id": 2, "uid": 99, "uidvalidity": "4242", "message_id": "<lost@x>", "source": [],
                                    "target": ["Рассылки"]}], [["Рассылки"]])
    conn = YandexImap.instances[-1]
    assert got == {"delimiter": "|", "created": ["Рассылки", "Отделы", "Отделы/Бухгалтерия"], "moved": {1: "ok", 2: "gone"}}
    assert ("CREATE", "Отделы|Бухгалтерия") in conn.commands and ("SUBSCRIBE", "Рассылки") in conn.commands
    assert ("UID MOVE", "7", '"' + mail.imap_utf7("Отделы|Бухгалтерия") + '"') in conn.commands
    assert ("UID SEARCH", "UID", "7") in conn.commands and conn.commands[-1] == ("LOGOUT",)
    assert 7 not in conn.folders["INBOX"] and list(conn.folders["Отделы|Бухгалтерия"].values()) == ["<a@x>"]
    # back into the inbox: found in the folder by its Message-ID; a letter already there (a mail filter) is fine
    conn.folders["Рассылки"][1] = "<c@x>"
    del conn.folders["INBOX"][9]
    got = mail.ImapBox(cfg).place([{"id": 1, "uid": 7, "uidvalidity": "4242", "message_id": "<a@x>",
                                    "source": ["Отделы", "Бухгалтерия"], "target": []},
                                   {"id": 3, "uid": 9, "uidvalidity": "4242", "message_id": "<c@x>", "source": [],
                                    "target": ["Рассылки"]}], [])
    conn = YandexImap.instances[-1]
    assert got["moved"] == {1: "ok", 3: "ok"} and got["created"] == []
    assert sorted(conn.folders["INBOX"].values()) == ["<a@x>", "<b@x>"] and conn.folders["Отделы|Бухгалтерия"] == {}
    assert ("UID SEARCH", "HEADER", "Message-ID", '"<a@x>"') in conn.commands
    # without MOVE: COPY, \Deleted and UID EXPUNGE of this one letter (UIDPLUS); with neither — refused
    monkeypatch.setattr(YandexImap, "after_login", ("IMAP4REV1", "UIDPLUS"))
    got = mail.ImapBox(cfg).place([{"id": 4, "uid": 8, "uidvalidity": "4242", "message_id": "<b@x>", "source": [],
                                    "target": ["Рассылки"]}], [])
    conn = YandexImap.instances[-1]
    assert got["moved"] == {4: "ok"} and ("UID STORE", "8", "+FLAGS.SILENT", "(\\Deleted)") in conn.commands
    assert ("UID EXPUNGE", "8") in conn.commands and not any(c[0] == "UID MOVE" for c in conn.commands)
    monkeypatch.setattr(YandexImap, "after_login", ("IMAP4REV1",))
    with pytest.raises(mail.MailError, match="не умеет перекладывать"):
        mail.ImapBox(cfg).place([{"id": 5, "uid": 8, "uidvalidity": "4242", "message_id": "<b@x>", "source": [],
                                  "target": ["Рассылки"]}], [])
    assert mail.ImapBox(cfg).place([], [["Новая"]])["created"] == ["Новая"]  # making folders needs neither
