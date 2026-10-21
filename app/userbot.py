"""The AI employee's Telegram account (MTProto via Telethon): messages go out from a person's account, not from a bot.

The module keeps one connection alive in a background thread, exposes plain blocking functions to the rest of the
app (same shape as `telegram.send_message` / `telegram.send_document`) and calls `on_message` for every private text
message that arrives. The session lives encrypted in the database, so a Render restart does not log the account out.
The Telegram client itself sits behind a small backend interface; tests plug a fake one in.
"""
import asyncio
import base64
import datetime as dt
import hashlib
import io
import json
import logging
import random
import re
import secrets
import threading
import time
import zoneinfo

from cryptography.fernet import Fernet, InvalidToken

from . import config, settings_store, telegram

log = logging.getLogger("sysai.userbot")

PAUSE_RANGE = (2.0, 6.0)        # seconds between two outgoing messages (looks like a person, protects the account)
DAILY_LIMIT = 150               # outgoing messages per rolling 24 hours; the real load is 20-100 a week
TYPING_MAX = 6.0                # seconds of the "typing..." indicator before a long message
MAX_VOICE_SECONDS = 600            # longer voice messages are not recognised (cost, and a meeting belongs in the panel)
MAX_VOICE_BYTES = 15 * 1024 * 1024
PEER_FLOOD_PAUSE = 6 * 3600     # after Telegram's PeerFlood answer nothing is sent for this long
LEASE_TTL = 90                  # seconds a running instance keeps the account to itself (renewed every watchdog tick)
INSTANCE = secrets.token_hex(4)  # this process; during a deploy the old and the new instance run side by side


class LoginError(RuntimeError):
    """A problem with signing in that the person can fix; the message is shown in the panel (Russian)."""


class RefError(ValueError):
    """An address of a person or group that cannot be understood (Russian message)."""


# ----------------------------------------------------------------------------- secrets at rest
def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(("userbot:" + config.SECRET_KEY).encode()).digest())
    return Fernet(key)


def seal(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def unseal(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


# ----------------------------------------------------------------------------- addresses
def normalize_ref(value: str) -> str:
    """@username, +phone, numeric id or t.me link into one canonical form. Raises RefError."""
    ref = (value or "").strip()
    link = re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/(.+)$", ref, re.I)
    if link:
        tail = link.group(1).split("?")[0].strip("/")
        if tail.startswith("+") or tail.lower().startswith("joinchat"):
            raise RefError("Ссылку-приглашение в группу указать нельзя: нужен @имя группы или её числовой ID.")
        ref = "@" + tail
    if re.fullmatch(r"-?\d{5,20}", ref):
        return ref
    phone = re.sub(r"[\s()\-]", "", ref)
    if re.fullmatch(r"\+\d{7,15}", phone):
        return phone
    name = re.fullmatch(r"@?([A-Za-z][A-Za-z0-9_]{4,31})", ref)
    if name:
        return "@" + name.group(1)
    raise RefError(f"Не понял адрес «{ref[:40]}». Нужен @username, номер в формате +375… или числовой ID.")


def same_person(ref: str, msg: dict) -> bool:
    """Does a stored reference (@name, +phone, id) point at the sender of an incoming message?"""
    ref = (ref or "").strip()
    if not ref:
        return False
    if re.fullmatch(r"-?\d+", ref):
        return str(msg.get("user_id")) == ref
    if ref.startswith("@"):
        return ref[1:].lower() == (msg.get("username") or "").lower()
    if ref.startswith("+"):
        return re.sub(r"\D", "", ref) == re.sub(r"\D", "", msg.get("phone") or "")
    return False


# ----------------------------------------------------------------------------- Telethon backend
class TelethonBackend:
    """Runs Telethon on its own asyncio loop in a daemon thread and offers blocking calls."""

    def __init__(self):
        self._loop = None
        self._thread = None
        self._client = None
        self._on_message = None
        self._watch = None
        self.wants = None  # optional pre-filter: only messages from people the app serves are handed over
        self._guard = threading.Lock()

    # plumbing
    def _run(self, coro, timeout=90):
        with self._guard:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="tg-userbot")
                self._thread.start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    @staticmethod
    def _telethon():
        from telethon import TelegramClient, errors, events, functions, types
        from telethon.sessions import StringSession
        return TelegramClient, StringSession, errors, events, functions, types

    # login: three requests (code, then maybe password), the half-done session travels in `pending`
    def login_start(self, api_id, api_hash, phone):
        TelegramClient, StringSession, errors, *_ = self._telethon()

        async def go():
            client = TelegramClient(StringSession(), int(api_id), api_hash)
            try:
                await client.connect()
                sent = await client.send_code_request(phone)
                return {"session": client.session.save(), "phone": phone, "hash": sent.phone_code_hash}
            except errors.ApiIdInvalidError:
                raise LoginError("Telegram не принял api_id и api_hash: проверьте, что скопировали их полностью.") from None
            except errors.PhoneNumberInvalidError:
                raise LoginError("Telegram не принял номер телефона. Нужен формат +375291234567.") from None
            except errors.FloodWaitError as exc:
                raise LoginError(f"Telegram просит подождать {exc.seconds} с перед новым кодом.") from None
            finally:
                await client.disconnect()
        return self._run(go())

    def login_code(self, pending, code):
        TelegramClient, StringSession, errors, *_ = self._telethon()

        async def go():
            client = TelegramClient(StringSession(pending["session"]), int(pending["api_id"]), pending["api_hash"])
            try:
                await client.connect()
                try:
                    await client.sign_in(phone=pending["phone"], code=code, phone_code_hash=pending["hash"])
                except errors.SessionPasswordNeededError:
                    return "password", dict(pending, session=client.session.save())
                except errors.PhoneCodeInvalidError:
                    raise LoginError("Неверный код. Проверьте цифры в сообщении от Telegram.") from None
                except errors.PhoneCodeExpiredError:
                    raise LoginError("Код устарел. Запросите новый.") from None
                return "ok", client.session.save()
            finally:
                await client.disconnect()
        return self._run(go())

    def login_password(self, pending, password):
        TelegramClient, StringSession, errors, *_ = self._telethon()

        async def go():
            client = TelegramClient(StringSession(pending["session"]), int(pending["api_id"]), pending["api_hash"])
            try:
                await client.connect()
                try:
                    await client.sign_in(password=password)
                except errors.PasswordHashInvalidError:
                    raise LoginError("Неверный пароль двухэтапной проверки.") from None
                return client.session.save()
            finally:
                await client.disconnect()
        return self._run(go())

    # running connection
    def connect(self, api_id, api_hash, session, on_message):
        TelegramClient, StringSession, errors, events, *_ = self._telethon()

        async def go():
            if self._client is not None:
                await self._client.disconnect()
                self._client = None
            client = TelegramClient(StringSession(session), int(api_id), api_hash, connection_retries=10,
                                    retry_delay=5, auto_reconnect=True)
            client.parse_mode = None  # plain text: the model writes without markup
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                raise LoginError("Сессия аккаунта недействительна: войдите в аккаунт заново.")
            me = await client.get_me()
            self._on_message = on_message
            client.add_event_handler(self._handle, events.NewMessage(incoming=True))
            self._client = client
            if self._watch is not None:
                self._watch.cancel()
            self._watch = asyncio.ensure_future(self._watch_unread())
            return {"id": me.id, "name": " ".join(x for x in (me.first_name, me.last_name) if x),
                    "first_name": me.first_name or "", "username": me.username or "", "phone": me.phone or ""}
        return self._run(go())

    async def _handle(self, event):
        if not event.is_private:
            return
        sender = await event.get_sender()
        await self._dispatch(sender, event.message)

    @staticmethod
    def _voice_of(message):
        """A voice message, an audio note or a round video as a small description with a lazy download, else None.
        The download happens only when the app asks for it (strangers and repeats never cost anything)."""
        file = getattr(message, "file", None)
        if file is None or not (getattr(message, "voice", None) or getattr(message, "audio", None)
                                or getattr(message, "video_note", None)):
            return None
        seconds = float(getattr(file, "duration", 0) or 0)
        size = int(getattr(file, "size", 0) or 0)
        mime = str(getattr(file, "mime_type", "") or "")
        if seconds > MAX_VOICE_SECONDS or size > MAX_VOICE_BYTES:
            return {"too_long": True, "seconds": seconds, "mime": mime}
        loop = asyncio.get_running_loop()

        def fetch():  # called from a worker thread of the app
            return asyncio.run_coroutine_threadsafe(message.download_media(file=bytes), loop).result(120)
        return {"fetch": fetch, "seconds": seconds, "mime": mime}

    async def _dispatch(self, sender, message):
        text = (message.raw_text or "").strip()
        if sender is None or getattr(sender, "bot", False):
            return
        voice = None
        if not text:
            voice = self._voice_of(message)
            if voice is None:
                return
        msg = {"user_id": sender.id, "username": sender.username or "", "phone": getattr(sender, "phone", "") or "",
               "name": " ".join(x for x in (sender.first_name, sender.last_name) if x), "text": text,
               "message_id": message.id, "reply_to": getattr(message, "reply_to_msg_id", None)}
        if not await self._served(sender):
            return
        if voice is not None:
            msg["voice"] = voice
        await asyncio.get_running_loop().run_in_executor(None, self._on_message, msg)

    async def _served(self, person) -> bool:
        """Strangers are dropped before anything touches the database (the account may also be a private one)."""
        if self.wants is None:
            return True
        probe = {"user_id": person.id, "username": getattr(person, "username", "") or "",
                 "phone": getattr(person, "phone", "") or ""}
        try:
            return bool(await asyncio.get_running_loop().run_in_executor(None, self.wants, probe))
        except Exception:  # noqa: BLE001
            log.exception("sender check failed, handing the message over anyway")
            return True

    async def _scan_unread(self):
        """After a sleep or reconnect messages may have arrived unseen: hand unread private texts over again.
        The receiver ignores duplicates by message id."""
        client = self._client
        async for dialog in client.iter_dialogs(limit=40):
            if dialog.is_user and dialog.unread_count and not getattr(dialog.entity, "bot", False):
                if not await self._served(dialog.entity):
                    continue  # nobody we work with: do not even fetch their messages
                for message in reversed(await client.get_messages(dialog.entity, limit=min(dialog.unread_count, 20))):
                    if not message.out:
                        await self._dispatch(dialog.entity, message)
                await asyncio.sleep(0.5)  # gentle on the account and on a small instance

    async def _watch_unread(self):
        await asyncio.sleep(30)  # the first scan waits: the connection and the web server settle first
        while self._client is not None:
            try:
                await self._scan_unread()
            except Exception:  # noqa: BLE001
                log.exception("unread scan failed")
            await asyncio.sleep(300)

    def disconnect(self):
        async def go():
            client, self._client = self._client, None
            if client is not None:
                await client.disconnect()
        if self._loop is not None:
            self._run(go())

    def log_out(self):
        async def go():
            client, self._client = self._client, None
            if client is not None:
                await client.log_out()
        if self._loop is not None:
            self._run(go())

    # sending
    async def _entity(self, client, ref):
        _, _, errors, _, functions, types = self._telethon()
        try:
            if ref.startswith("+"):
                try:
                    return await client.get_entity(ref)
                except ValueError:
                    found = await client(functions.contacts.ImportContactsRequest([types.InputPhoneContact(
                        client_id=random.randrange(1 << 30), phone=ref, first_name=ref, last_name="")]))
                    if not found.users:
                        raise
                    return found.users[0]
            if re.fullmatch(r"-?\d+", ref):
                try:
                    return await client.get_entity(int(ref))
                except ValueError:
                    await client.get_dialogs(limit=200)  # fills the entity cache
                    return await client.get_entity(int(ref))
            return await client.get_entity(ref)
        except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
            raise telegram.TelegramError(f"Получатель {ref} не найден в Telegram", retryable=False) from None

    def _map(self, exc):
        _, _, errors, *_ = self._telethon()
        if isinstance(exc, errors.FloodWaitError):
            return telegram.TelegramError(f"Telegram просит подождать {exc.seconds} с", retryable=True,
                                          retry_after=exc.seconds)
        if isinstance(exc, errors.PeerFloodError):
            return PeerFlood("Telegram ограничил отправку сообщений с этого аккаунта")
        if isinstance(exc, (errors.UserPrivacyRestrictedError, errors.UserIsBlockedError,
                            errors.InputUserDeactivatedError, errors.ChatWriteForbiddenError)):
            return telegram.TelegramError("Получатель не принимает сообщения от этого аккаунта", retryable=False)
        return None

    def is_connected(self) -> bool:
        return self._client is not None and bool(self._client.is_connected())

    def _online(self):
        if self._client is None:
            raise telegram.TelegramError("Аккаунт Telegram сейчас не подключён", retryable=True, retry_after=20)
        return self._client

    @staticmethod
    def _user_id(entity):
        _, _, _, _, _, types = TelethonBackend._telethon()
        return entity.id if isinstance(entity, types.User) else None

    def send(self, ref, text, reply_to=None, typing=0.0):
        async def go():
            client = self._online()
            entity = await self._entity(client, ref)
            try:
                await client.send_read_acknowledge(entity)
            except Exception:  # noqa: BLE001
                pass
            if typing:
                async with client.action(entity, "typing"):
                    await asyncio.sleep(typing)
            message = await client.send_message(entity, text, reply_to=reply_to, link_preview=False)
            return {"message_id": message.id, "user_id": self._user_id(entity)}
        return self._call(go)

    def send_file(self, ref, filename, content, caption=""):
        async def go():
            client = self._online()
            entity = await self._entity(client, ref)
            data = io.BytesIO(content)
            data.name = filename
            message = await client.send_file(entity, data, caption=caption or None, force_document=True)
            return {"message_id": message.id, "user_id": self._user_id(entity)}
        return self._call(go)

    def _call(self, go):
        try:
            return self._run(go(), timeout=180)
        except Exception as exc:  # noqa: BLE001
            mapped = self._map(exc)
            if mapped is not None:
                raise mapped from None
            raise


class PeerFlood(telegram.TelegramError):
    """Telegram refuses further sending from the account (too many complaints or too many new contacts)."""


class QuietHours(telegram.TelegramError):
    """Messages to people wait for the morning: the quiet hours are on (see quiet_until)."""

    def __init__(self, seconds_left: float):
        super().__init__("Тихие часы: сообщения уйдут утром", retryable=True, retry_after=max(60, int(seconds_left)))


def _zone():
    try:
        return zoneinfo.ZoneInfo(config.TIMEZONE)
    except zoneinfo.ZoneInfoNotFoundError:
        return dt.timezone.utc


def local_time(moment) -> str:
    return moment.astimezone(_zone()).strftime("%H:%M %d.%m")


def quiet_until(now_utc=None):
    """When the quiet hours end, if they are on at that moment; otherwise None.

    Settings quiet_from_hour and quiet_to_hour are hours of the service time zone; equal hours mean no quiet time.
    Tests see no quiet hours unless they pass now_utc explicitly."""
    if now_utc is None and config.TESTING:
        return None
    start, end = settings_store.get("quiet_from_hour"), settings_store.get("quiet_to_hour")
    if start is None or end is None:
        return None
    start, end = int(start), int(end)
    if start == end:
        return None
    now = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(_zone())
    hour = now.hour
    inside = start <= hour < end if start < end else (hour >= start or hour < end)
    if not inside:
        return None
    opens = now.replace(hour=end, minute=0, second=0, microsecond=0)
    if opens <= now:
        opens += dt.timedelta(days=1)
    return opens.astimezone(dt.timezone.utc)


# ----------------------------------------------------------------------------- the account used by the app
backend = None
_on_message = None
_state = {"me": None, "error": "", "connected": False}
_send_lock = threading.Lock()
_sent_at: list[float] = []
_last_send = [0.0]
_paused_until = [0.0]


def _backend():
    global backend
    if backend is None:
        backend = TelethonBackend()
    return backend


def _default_handler(msg: dict) -> None:
    from . import agent
    agent.handle_incoming(msg)


def _credentials():
    return (unseal(settings_store.get("userbot_api_id") or ""), unseal(settings_store.get("userbot_api_hash") or ""),
            unseal(settings_store.get("userbot_session") or ""))


def configured() -> bool:
    return bool(settings_store.get("userbot_session"))


def ai_mode() -> bool:
    return bool(settings_store.get("ai_employee"))


def ready() -> bool:
    return bool(_state["connected"])


def me() -> dict:
    return dict(_state["me"] or {})


def status() -> dict:
    step = pending_step()
    phone = settings_store.get("userbot_phone") or ""
    if step:  # the number of the login that is still in progress
        try:
            phone = json.loads(unseal(settings_store.get("userbot_pending") or "") or "{}").get("phone", phone)
        except ValueError:
            pass
    return {"configured": configured(), "connected": ready(), "me": me(), "error": _state["error"], "step": step,
            "phone": phone, "paused": time.time() < _paused_until[0]}


def take_lease() -> bool:
    """Only one running instance may use the account: Telegram revokes a session that two machines use at once, and
    during a deploy the old and the new instance overlap. The holder renews the lease; a stopped one releases it."""
    from .db import SessionLocal, Setting
    from sqlalchemy.exc import IntegrityError
    stamp = time.time()
    with SessionLocal() as s:
        row = s.get(Setting, "userbot_lease", with_for_update=True)
        current = json.loads(row.value) if row else {}
        if current.get("id") not in (None, INSTANCE) and float(current.get("until") or 0) > stamp:
            return False
        value = json.dumps({"id": INSTANCE, "until": stamp + LEASE_TTL})
        if row:
            row.value = value
        else:
            s.add(Setting(key="userbot_lease", value=value))
        try:
            s.commit()
        except IntegrityError:  # another instance created the row first: it wins this round
            s.rollback()
            return False
    return True


def release_lease() -> None:
    from .db import SessionLocal, Setting
    with SessionLocal() as s:
        row = s.get(Setting, "userbot_lease", with_for_update=True)
        if row and json.loads(row.value).get("id") == INSTANCE:
            row.value = json.dumps({"id": INSTANCE, "until": 0})
            s.commit()


def start(on_message=None) -> bool:
    """Connect with the stored session. Returns True when the account is online."""
    global _on_message
    if on_message is not None:
        _on_message = on_message
    api_id, api_hash, session = _credentials()
    if not (api_id and api_hash and session):
        return False
    if not take_lease():
        _state.update(connected=False, error="Аккаунт сейчас занят предыдущей копией сервиса (идёт обновление). Подключусь сам, как только она остановится.")
        return False
    try:
        from . import agent
        _backend().wants = agent.wants  # the account may be a private one: only the people the app serves get through
    except AttributeError:
        pass
    try:
        info = _backend().connect(api_id, api_hash, session, _on_message or _default_handler)
    except LoginError as exc:
        _state.update(connected=False, error=str(exc))
        return False
    except Exception as exc:  # noqa: BLE001
        log.exception("Telegram account did not connect")
        _state.update(connected=False, error=f"Не удалось подключиться к Telegram: {str(exc)[:200]}")
        return False
    _state.update(me=info, error="", connected=True)
    settings_store.set_many({"userbot_me": info})
    log.info("Telegram account online: %s", info.get("name"))
    return True


_booting = threading.Lock()


def _boot(on_message=None):
    if _booting.acquire(blocking=False):  # one connection attempt at a time
        try:
            start(on_message)
        finally:
            _booting.release()


def start_in_background(on_message=None):
    threading.Thread(target=_boot, args=(on_message,), daemon=True, name="tg-userbot-start").start()


def watchdog(on_message=None) -> None:
    """Keeps the account online: connects when it never came up and reconnects when the connection dropped."""
    if not (ai_mode() and configured()):
        return
    if ready():
        if not take_lease():  # somebody else holds the account (should not happen): step aside, never fight over it
            log.warning("Another instance took the Telegram account, disconnecting here")
            stop()
            return
        try:
            alive = getattr(_backend(), "is_connected", lambda: True)()
        except Exception:  # noqa: BLE001
            alive = False
        if alive:
            return
        _state["connected"] = False
        log.warning("Telegram account lost its connection, reconnecting")
    _boot(on_message)


def stop():
    if _state["connected"]:
        try:
            _backend().disconnect()
        except Exception:  # noqa: BLE001
            log.exception("disconnect failed")
    _state["connected"] = False
    try:
        release_lease()
    except Exception:  # noqa: BLE001
        log.exception("could not release the account lease")


def sync(on_message=None):
    """Bring the connection in line with the mode switch: online in AI-employee mode, offline otherwise."""
    if ai_mode() and configured():
        if not ready():
            start_in_background(on_message)
    else:
        stop()


# ----------------------------------------------------------------------------- login from the panel
def pending_step():
    raw = settings_store.get("userbot_pending") or ""
    if not raw:
        return None
    try:
        return json.loads(unseal(raw) or "{}").get("step")
    except ValueError:
        return None


def _pending() -> dict:
    try:
        data = json.loads(unseal(settings_store.get("userbot_pending") or "") or "{}")
    except ValueError:
        data = {}
    if not data:
        raise LoginError("Вход не начат или устарел: начните заново с номера телефона.")
    return data


def login_start(api_id: str, api_hash: str, phone: str) -> None:
    api_id, api_hash = (api_id or "").strip(), (api_hash or "").strip()
    phone = re.sub(r"[\s()\-]", "", phone or "")
    if not re.fullmatch(r"\d{4,12}", api_id):
        raise LoginError("api_id это число из 5-9 цифр со страницы my.telegram.org/apps.")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise LoginError("api_hash это 32 символа (цифры и буквы a-f) со страницы my.telegram.org/apps.")
    if not re.fullmatch(r"\+\d{7,15}", phone):
        raise LoginError("Номер телефона нужен в международном формате, например +375291234567.")
    pending = _backend().login_start(api_id, api_hash, phone)
    pending.update(api_id=api_id, api_hash=api_hash, step="code")
    settings_store.set_many({"userbot_pending": seal(json.dumps(pending))})


def _finish(session: str, pending: dict) -> None:
    settings_store.set_many({
        "userbot_api_id": seal(pending["api_id"]), "userbot_api_hash": seal(pending["api_hash"]),
        "userbot_session": seal(session), "userbot_phone": pending["phone"], "userbot_pending": ""})
    _state["error"] = ""
    if not start():
        raise LoginError(_state["error"] or "Вход выполнен, но подключиться к Telegram не удалось.")


def login_code(code: str) -> str:
    """Returns "ok" when signed in or "password" when the account asks for its two-step password."""
    pending = _pending()
    result, data = _backend().login_code(pending, re.sub(r"\D", "", code or ""))
    if result == "password":
        data["step"] = "password"
        settings_store.set_many({"userbot_pending": seal(json.dumps(data))})
        return "password"
    _finish(data, pending)
    return "ok"


def login_password(password: str) -> None:
    pending = _pending()
    _finish(_backend().login_password(pending, password or ""), pending)


def logout() -> None:
    try:
        if _state["connected"]:
            _backend().log_out()
    except Exception:  # noqa: BLE001
        log.exception("log out failed")
    _state.update(connected=False, me=None, error="")
    settings_store.set_many({key: "" for key in ("userbot_api_id", "userbot_api_hash", "userbot_session",
                                                 "userbot_phone", "userbot_pending")})
    settings_store.set_many({"userbot_me": {}})


# ----------------------------------------------------------------------------- sending
def _guard_send(urgent: bool = False):
    now = time.time()
    if now < _paused_until[0]:
        raise telegram.TelegramError("Telegram ограничил отправку с аккаунта. Сообщения не уходят, пока не закончится пауза.")
    until = None if urgent else quiet_until()  # a message somebody asked for at a given time is not held back
    if until is not None:
        raise QuietHours((until - dt.datetime.now(dt.timezone.utc)).total_seconds())
    if not ready():
        if configured():
            raise telegram.TelegramError("Аккаунт Telegram подключается, подождите минуту", retryable=True, retry_after=20)
        raise telegram.TelegramError("Telegram-аккаунт не подключён: Настройки → Telegram-аккаунт")
    _sent_at[:] = [t for t in _sent_at if t > now - 86400]
    if len(_sent_at) >= DAILY_LIMIT:
        raise telegram.TelegramError("Достигнут суточный лимит исходящих сообщений аккаунта")


def _pace(typing_chars: int = 0) -> float:
    """Sleeps a human-looking pause between outgoing messages; returns the seconds to show "typing..."."""
    if config.TESTING:
        return 0.0
    wait = _last_send[0] + random.uniform(*PAUSE_RANGE) - time.time()
    if wait > 0:
        time.sleep(wait)
    return min(TYPING_MAX, 0.6 + typing_chars / 45) if typing_chars else 0.0


def _record_send():
    _last_send[0] = time.time()
    _sent_at.append(_last_send[0])


def _flood(exc: PeerFlood):
    _paused_until[0] = time.time() + PEER_FLOOD_PAUSE
    log.error("PeerFlood: sending paused for %s hours", PEER_FLOOD_PAUSE // 3600)


def send_message(chat_id, text: str, buttons=None, reply_to=None, urgent: bool = False) -> dict:
    """Same shape as telegram.send_message. Buttons do not exist for a personal account and are ignored.
    `urgent` skips the quiet hours (reminders that were asked for at a given time)."""
    with _send_lock:
        _guard_send(urgent)
        try:
            result = _backend().send(str(chat_id), text, reply_to=reply_to, typing=_pace(len(text)))
        except PeerFlood as exc:
            _flood(exc)
            raise
        _record_send()
        return result


def send_document(chat_id, filename: str, content: bytes, caption: str = "") -> dict:
    with _send_lock:
        _guard_send()
        _pace()
        try:
            result = _backend().send_file(str(chat_id), filename, content, caption)
        except PeerFlood as exc:
            _flood(exc)
            raise
        _record_send()
        return result
