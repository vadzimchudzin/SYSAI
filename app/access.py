"""Who may do what: access levels of people, kept in the settings lists and edited in the employee card.

    employee  Сотрудник       own tasks and deadlines, questions to the reviewer, reminders to themselves
    full      Полный доступ   the whole team, reports, reminders to others, orders to the assistant, sending files
    reviewer  Проверяющий     full access + approves minutes and decides on cases; case reports go to them
    observer  Наблюдатель     sees everything, the assistant never asks or reports to them on its own

The settings lists stay the single source (approver_chat_ids, full_access_chat_ids, observer_chat_ids, and the legacy
director_chat_ids): the card only puts the person's Telegram reference into the right list. So drafts, reports and the
AI employee's rules keep working the same way whichever page the rights were set on.
"""
from . import settings_store, userbot

LEVELS = ("employee", "full", "reviewer", "observer")
LEVEL_RU = {"employee": "Сотрудник", "full": "Полный доступ", "reviewer": "Проверяющий", "observer": "Наблюдатель",
            "director": "Директор"}
LEVEL_HINT = {
    "employee": "свои задачи и сроки, вопросы проверяющему, напоминания себе",
    "full": "вся команда, отчёты, напоминания другим, поручения ассистенту, пересылка файлов",
    "reviewer": "полный доступ + одобряет протоколы и решает по делам; ему идут отчёты ассистента",
    "observer": "видит всё, но ассистент его не тревожит: без вопросов и отчётов",
}
SETTING = {"full": "full_access_chat_ids", "reviewer": "approver_chat_ids", "observer": "observer_chat_ids"}
STRONG = ("director", "reviewer", "full")  # marked red in the directory: their word is an order for the assistant


class AccessError(ValueError):
    pass


def probe(employee) -> dict:
    """The employee as an incoming-message sender, to compare with stored references (@name, +phone, id)."""
    chat = (employee.telegram_chat_id or "").strip()
    out = {"user_id": (employee.telegram_user_id or "").strip(),
           "username": chat[1:] if chat.startswith("@") else "",
           "phone": chat if chat.startswith("+") else ""}
    if chat and chat.lstrip("-").isdigit() and not out["user_id"]:
        out["user_id"] = chat
    return out


def _listed(setting: str, who: dict) -> bool:
    return any(userbot.same_person(ref, who) for ref in settings_store.get(setting) or [])


def level_of(employee) -> str:
    """'director' | 'reviewer' | 'full' | 'observer' | 'employee'."""
    who = probe(employee)
    if not (who["user_id"] or who["username"] or who["phone"]):
        return "employee"
    for level, setting in (("director", "director_chat_ids"), ("reviewer", "approver_chat_ids"),
                           ("full", "full_access_chat_ids"), ("observer", "observer_chat_ids")):
        if _listed(setting, who):
            return level
    return "employee"


def set_level(employee, level: str) -> bool:
    """Put the person into exactly one of the lists (or none for 'employee'). Returns True when something changed.
    The legacy director list is not touched here."""
    if level not in LEVELS:
        raise AccessError("Неизвестный уровень доступа")
    ref = (employee.telegram_chat_id or "").strip()
    if level != "employee" and not ref:
        raise AccessError("Сначала укажите Telegram сотрудника: права привязываются к его аккаунту")
    who = probe(employee)
    updates = {}
    for lvl, setting in SETTING.items():
        current = list(settings_store.get(setting) or [])
        kept = [r for r in current if not userbot.same_person(r, who) and r != ref]
        if lvl == level:
            kept.append(ref)
        if kept != current:
            updates[setting] = kept
    if updates:
        settings_store.set_many(updates)
    return bool(updates)
