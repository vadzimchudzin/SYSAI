"""Settings editable in the web panel (stored in the database)."""
import json

from .db import SessionLocal, Setting

POSITIONS = ["Директор", "Заместитель директора", "Руководитель отдела", "Менеджер проекта", "Менеджер по продажам",
             "Маркетолог", "Бухгалтер", "Юрист", "HR-менеджер", "Разработчик", "Дизайнер", "Аналитик",
             "Офис-менеджер", "Помощник руководителя"]

DEFAULTS = {
    # AI employee: writes from a Telegram account instead of a bot
    "ai_employee": False,
    "director_chat_ids": [],         # the director: the AI employee answers to them directly. Once somebody is listed, only
                                     # they may ask about other people, give orders, ask for reports and reminders for others
    "full_access_chat_ids": [],      # contacts (besides the reviewers) who may ask "who does what" and get reports
    "persona": "",                   # how the AI employee talks; empty = built-in default
    "agent_model": "google/gemini-2.5-flash",
    "userbot_api_id": "", "userbot_api_hash": "", "userbot_session": "", "userbot_phone": "",  # sealed, see userbot.py
    "userbot_pending": "", "userbot_me": {},
    # access
    "registration_open": True,       # people can leave a sign-up request; a superuser approves it
    # spending and quiet hours
    "daily_budget_usd": 5.0,         # OpenRouter spend per UTC day; 0 = no limit
    "quiet_from_hour": 21,           # no messages to people from this hour (service time zone) ...
    "quiet_to_hour": 9,              # ... until this hour; equal hours mean no quiet time
    # processing
    "auto_ingest": False,            # pick up recordings from the microphone automatically (mocked inbox in MVP)
    "approval_required": True,       # draft goes to approvers before anything is sent
    "approver_chat_ids": [],         # telegram chats that approve drafts
    # delivery
    "report_chat_ids": [],           # where the full report goes (people, groups, channels)
    "send_tasks_to_assignees": True,
    "include_transcript_in_pdf": False,
    "report_style": "balanced",          # balanced | executive | detailed | technical
    # deadlines
    "deadline_mode": "default",      # none | default | ask
    "default_deadline_days": 3,      # business days
    "ask_timeout_hours": 24,         # if nobody answers, fall back to the default
    # models (OpenRouter)
    "transcribe_model": "google/gemini-2.5-flash",
    "report_model": "google/gemini-2.5-pro",
    "conversation_model": "openrouter/free",
    "dialogue_enabled": True,
    "conversation_tone": "Доброжелательно и по делу. Короткие сообщения, без канцелярита и лишних эмодзи.",
    "allow_deadline_proposals": True,
    "secretary_chat_ids": [],
    "chunk_minutes": 30,
    "language": "ru",
    "glossary": "SYSAI, Bitrix24, Telegram, OpenRouter, CRM, KPI, roadmap, deadline",  # mocked terms for now
    # look
    "company_name": "Компания",
    "accent_color": "#c98179",
    # integrations (keys pasted in the panel; environment variables are the fallback)
    "bitrix_enabled": False,
    "openrouter_api_key": "",
    "telegram_bot_token": "",
    "bitrix_webhook_url": "",
    # site owner: shown in the footer, on /legal and in the privacy policy (defaults from sysat.by)
    "legal_name": "ООО «СисЭйТи»",
    "legal_unp": "",
    "legal_address": "222520, Минская обл., г. Борисов, ул. 1 Июля, д. 6",
    "legal_email": "company@sysat.by",
    "legal_phone": "+375 29 139-10-80",
    "legal_site": "https://sysat.by",
    "pd_responsible": "",            # ФИО и должность ответственного за обработку персональных данных
}

LEGAL_KEYS = ("legal_name", "legal_unp", "legal_address", "legal_email", "legal_phone", "legal_site", "pd_responsible")


def get(key):
    with SessionLocal() as s:
        row = s.get(Setting, key)
        return json.loads(row.value) if row else DEFAULTS.get(key)


def all_settings() -> dict:
    out = dict(DEFAULTS)
    with SessionLocal() as s:
        for row in s.query(Setting).all():
            out[row.key] = json.loads(row.value)
    return out


MEETING_OPTIONS = ("approval_required", "approver_chat_ids", "deadline_mode", "report_chat_ids", "send_tasks_to_assignees",
                   "include_transcript_in_pdf", "report_style")


def for_meeting(options: dict | None) -> dict:
    """Global settings with per-meeting overrides chosen on the upload page."""
    out = all_settings()
    for k, v in (options or {}).items():
        if k in MEETING_OPTIONS:
            out[k] = v
    return out


def set_many(values: dict):
    with SessionLocal() as s:
        for k, v in values.items():
            if k not in DEFAULTS:
                continue
            row = s.get(Setting, k)
            if row:
                row.value = json.dumps(v, ensure_ascii=False)
            else:
                s.add(Setting(key=k, value=json.dumps(v, ensure_ascii=False)))
        s.commit()


SECRET_KEYS = ("openrouter_api_key", "telegram_bot_token", "bitrix_webhook_url")
# Never sent to the browser (sealed Telegram account data).
HIDDEN_KEYS = ("userbot_api_id", "userbot_api_hash", "userbot_session", "userbot_phone", "userbot_pending", "userbot_me")


def mask(v: str) -> str:
    v = v or ""
    return "" if not v else ("•" * 8 + v[-4:] if len(v) > 8 else "•" * len(v))
