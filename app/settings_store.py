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
    "full_access_chat_ids": [],
    "observer_chat_ids": [],
    # summary of the assistant's cases for the reviewers (digest.py)
    "digest_frequency": "daily",     # off | daily | weekly
    "digest_hour": 9,                # service time zone
    "digest_minute": 0,
    "digest_weekday": 0,             # 0 = Monday, for weekly
    "digest_recipients": [],         # employee ids who get it (with Telegram); empty = the reviewers
    "digest_last_sent": "",          # the day / week already covered         # see everything, never asked or reported to by the assistant (access.py)      # contacts (besides the reviewers) who may ask "who does what" and get reports
    "persona": "",                   # how the AI employee talks; empty = built-in default
    "agent_model": "google/gemini-2.5-flash",
    "agent_transport": "json",       # how the AI employee asks for actions: json (an answer with "actions") | tools
    "agent_skill_modes": {},         # skill -> auto | approve | suggest (Skills page); nothing set = everything runs at once
    "agent_skills_off": [],          # skills turned off on the Skills page (for everybody)
    "agent_skill_who": {},           # skill -> leaders | director: who may use it, narrowed on the Skills page
    "agent_budget_month_usd": 0.0,   # what the AI employee may cost a month (checking dialogs compare models with it)
    "userbot_api_id": "", "userbot_api_hash": "", "userbot_session": "", "userbot_phone": "",  # sealed, see userbot.py
    "userbot_pending": "", "userbot_me": {},
    # access
    "registration_open": True,       # people can leave a sign-up request; a superuser approves it
    # spending and quiet hours
    "daily_budget_usd": 5.0,         # OpenRouter spend per UTC day; 0 = no limit
    "quiet_from_hour": 21,           # no messages to people from this hour (service time zone) ...
    "quiet_to_hour": 9,              # ... until this hour; equal hours mean no quiet time
    "quiet_days": [5, 6],            # days off (0 = Monday): nobody is written to first; answers still go
    "person_hourly_limit": 4,        # messages the assistant may start with one person in an hour; 0 = no limit
    "silence_working_hours": True,   # the hours of silence before a reminder or a report: the night and days off not counted
    "audio_cleanup": True,           # speech clean-up before recognition (rumble, steady noise, levels)
    "speech_fix": True,              # after recognition: names and terms corrected by the directory and the glossary
    "speech_detector": True,         # voice detector (vad.py): cut in pauses, hear again voice with no words, drop
                                     # phrases invented over silence (verify.py)
    "speech_doubts": True,           # doubtful words (low engine confidence, out of place by sense) heard again
    "stt_keyterms": True,            # names and terms sent to the engine as a list (STT «keyterms»)
    "stt_diarize": True,             # the engine labels the speaker of every word (STT «diarize»)
    "speaker_naming": True,          # «Спикер 1» becomes a name when the conversation proves who it is
    "speaker_name_threshold": 0.75,  # …surely enough (speakers.py); below it «Кто это?» asks with candidates
    "company_profile": "",           # what the company does, clients, products, rules everyone should know (memory)
    "doc_intake": True,              # incoming files: requisites, sums, deadlines, routing by the rules (doc_intake.py)
    "mail_enabled": False,           # read the company mailbox (mail.py, №22): IMAP, reading only
    "mail_service": "",              # mail.PRESETS key: the servers of a known mail service
    "mail_address": "",              # the box (its login)
    "mail_password": "",             # app password, sealed (mail.py); never sent to the browser
    "mail_imap_host": "", "mail_imap_port": 993, "mail_smtp_host": "", "mail_smtp_port": 465,
    "mail_folder": "INBOX",
    "mail_poll_minutes": 3,          # how often the box is looked at
    "mail_history_days": 3,          # the first look takes the letters of these last days
    "mail_labels": False,            # the type and the labels are put into the box too (needs writing to the box)
    "mail_kinds": [],                # the types of letters [{"key", "name", "hint"}]; empty = mail.DEFAULT_KINDS
    "mail_state": {},                # {"uidvalidity", "last_uid", "checked_at", "ok", "error", "new"} (mail.py)
    "mail_forward": True,            # the sorted letters go to the people who handle them (mail_route.py, №24)
    "mail_hints_auto": False,        # no rule fits: forward by the agent's hint when it is sure (otherwise it waits)
    "mail_drafts_auto": True,        # a letter that waits for an answer comes with a draft of the answer (№25)
    # Отделы и папки секретаря (mail_folders.py): who of a department gets its letters, the folders of the box
    "mail_departments": {},          # {department id: {"on", "to" (employee id; None = the head), "folder", "letters"}}
    "mail_departments_auto": True,   # no rule fits: the letter goes to the department the sorting named, when it is sure
    "mail_folders_on": False,        # the sorted letters are moved into the folders of the box (needs writing to the box)
    "mail_folders_root": "",         # the folder the folders of the secretary are made in; "" = the top level
    "mail_folder_rules": [{"kind": "newsletter", "senders": "", "keywords": "", "folder": "Рассылки"}],
    "mail_folders_state": {},        # the last work with the folders: when, ok, error, the delimiter, made, moved
    "mail_sender_name": "",          # the name in «From» of the answers; "" = the company name
    "mail_signature": "",            # added under every answer
    "mail_reply_examples": "",       # good answers of the company (the style of the drafts), separated by «---»
    # Календарь (calendars.py, №26): the addresses for subscription and the company calendar read for the participants
    "calendar_feed_on": True,        # the personal ICS addresses answer (off: every address says «нет»)
    "calendar_salt": "",             # part of every address; «Сменить ссылки» replaces it
    "calendar_ics_url": "",          # the secret ICS address of the company calendar, sealed; never sent to the browser
    "calendar_state": {},            # the last reading: when, ok, error, how many meetings
    # Битрикс24 и Google Drive (bitrix_sync.py, gdrive.py, №28); «Ставить задачи в Битрикс24» is bitrix_enabled below
    "bitrix_sync_tasks": False,      # the status, the deadline, the title and the person of linked tasks: both ways
    "bitrix_import_tasks": False,    # new tasks set in Bitrix24 for our people come here
    "bitrix_sync_contacts": False,   # companies (with their contact persons) of the CRM ↔ the cards of clients
    "bitrix_state": {},              # the last sync: when, ok, error, what was done, the change marks of each list
    "bitrix_users": [],              # the users of the portal [{"id", "name", "email", "position"}] for the matching
    "gdrive_key": "",                # the JSON key of the service account, sealed; never sent to the browser
    "gdrive_folder": "",             # the id of the company folder (shared with the service account as an editor)
    "gdrive_inbox": True,            # files put into «Входящие» come to «Документы»
    "gdrive_upload": True,           # the documents of SYSAI go to «Документы» by month (not the confidential ones)
    "gdrive_templates": True,        # «Шаблоны» ↔ «Документы по шаблону», both ways
    "gdrive_state": {},              # the folders, the last sync, errors; upload_blocked when Google gives no space
    # the agent network (app/net, docs/opo): agents on the employees' computers, SYSAI_Hub, MCP and the model gateway
    "agents_on": False,              # off: every address of the network answers 404, SYSAI works as before
    "opo_on": False,                 # the ОПО section (PLC projects from the drawings of ТО), built on the network
    "agents_models": {},             # tier → model behind sysai/cheap|mid|strong|vision ("" = the default of gateway.py)
    "agents_hub_hosts": ["sysat.by", "se.com", "schneider-electric.com", "localhost", "127.0.0.1"],  # sites scripts of skills may call
    "agents_mcp_per_minute": 120,    # MCP calls of one agent per minute
    "agents_fallback": None,         # tier → the spare model ("" = none); None — the defaults of gateway.py
    "agents_soul": None,             # the character of the agents (SOUL.md): {"common", "departments"}; None — soul.py
    # «Сервер» (server_watch.py, №29)
    "backup_copy": True,             # copy the newest nightly dump of the own server to the recordings storage
    "backup_state": {},              # the dumps copied there [{"name", "size", "at"}], the last error, when checked
    "memory_autolearn": True,        # after an approved meeting the assistant adds facts to memory (with the quote)
    "embedding_model": "openai/text-embedding-3-small",  # memory search; empty = words only
    "vision_model": "google/gemini-2.5-flash",           # reading scans and photos of documents
    "case_autonomy": "auto",         # cases the assistant runs itself: auto | approve (director confirms the plan) | suggest
    "case_reminder_hours": 4,        # no answer this long: a polite reminder (quiet hours are respected)
    "case_max_reminders": 2,         # reminders before the director is told
    "case_escalate_hours": 24,       # no answer this long after the request: the director is told
    "sensitive_keywords": "паспорт, зарплат, персональн, конфиденц, nda, пароль, банковск, выписк, больничн, медицин, трудов",
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
    "transcribe_model": "elevenlabs/scribe-v2",           # see README «Как распознаётся речь» and /speech-compare
    "verify_model": "google/gemini-3.5-transcribe",       # second engine: hears again what the first missed or doubted
    "verify_judge_model": "google/gemini-2.5-pro",        # listens to a doubtful word and chooses between the engines
    "stt_compare": {},                                    # the last comparison of the engines (stt_compare.py)
    "diarization_model": "google/gemini-2.5-flash",
    "diarization_method": "chunks",      # chunks | voice | gemini — who speaks across the whole recording, see diarize.py
    "diarization_voice_threshold": 0.5,  # voice prints at least this alike are one person (method «voice»)
    "voice_memory": False,               # remember confirmed voices between meetings (voice_memory.py; with consent)
    "voice_memory_threshold": 0.62,      # a voice this alike to the prints of a person from past meetings is that person
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
    "org_map_root_x": 85,
    "org_map_root_y": 735,
    # integrations (keys pasted in the panel; environment variables are the fallback)
    "bitrix_enabled": False,
    "openrouter_api_key": "",
    "telegram_bot_token": "",
    "bitrix_webhook_url": "",
    "notion_parent_id": "",
    "ticnote_enabled": False,      # TicNote mode: device recordings are picked up automatically
    "ticnote_appkey": "",          # TicNote AppKey (tnovs_sk_... / tncn_sk_...)
    "ticnote_enabled_at": 0,       # ms; only recordings that reached the TicNote cloud after this are taken
    "voice_debug_mode": False,     # keep asking for confirmed meeting clips until identification looks ready       # only import TicNote exports below this Notion page/database
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


SECRET_KEYS = ("openrouter_api_key", "telegram_bot_token", "bitrix_webhook_url", "ticnote_appkey")
# Never sent to the browser (sealed Telegram account data).
HIDDEN_KEYS = ("userbot_api_id", "userbot_api_hash", "userbot_session", "userbot_phone", "userbot_pending", "userbot_me",
               "mail_password", "calendar_ics_url", "calendar_salt", "gdrive_key")


def mask(v: str) -> str:
    v = v or ""
    return "" if not v else ("•" * 8 + v[-4:] if len(v) > 8 else "•" * len(v))
