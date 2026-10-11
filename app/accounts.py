"""Accounts: sign-up requests that a superuser approves, plus the superuser taken from ADMIN_USERNAME/ADMIN_PASSWORD.

There is no open registration. Anyone can leave a request (status "pending"); it cannot sign in until a
superuser approves it. The superuser from the environment is re-created on every start, which also makes it
the recovery path when every panel superuser is lost.
"""
import base64
import datetime as dt
import hashlib
import hmac
import os
import re
import threading
import time
import zoneinfo

from sqlalchemy.exc import IntegrityError

from . import config, settings_store
from .db import SessionLocal, User, now

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
MIN_PASSWORD = 8
MAX_PASSWORD = 128
MAX_PENDING = 50            # unanswered requests kept at once
REGISTER_LIMIT = 5          # sign-up attempts per address ...
REGISTER_WINDOW = 3600      # ... per this many seconds
ROLES = ("user", "superuser")
ACTIONS = ("approve", "reject", "disable", "promote", "demote", "delete")

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()


class AccountError(ValueError):
    """A problem the person can fix; the message is shown in the interface (Russian)."""


# ----------------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}$".format(**_SCRYPT) + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_DUMMY_HASH = hash_password("not-a-real-password")  # burns the same time for unknown logins


# ----------------------------------------------------------------------------- validation
def normalize(username: str) -> str:
    return (username or "").strip().lower()


def _valid_username(username: str) -> str:
    name = normalize(username)
    if not USERNAME_RE.match(name):
        raise AccountError("Логин: 3-32 символа, латиница в нижнем регистре, цифры, точка, дефис, подчёркивание.")
    return name


def _valid_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AccountError(f"Пароль не короче {MIN_PASSWORD} символов.")
    if len(password) > MAX_PASSWORD:
        raise AccountError(f"Пароль не длиннее {MAX_PASSWORD} символов.")


def registration_open() -> bool:
    return settings_store.get("registration_open") is not False


def allow_registration(ip: str) -> bool:
    """Per-address limit on sign-up attempts (counts every attempt, successful or not)."""
    cutoff = time.monotonic() - REGISTER_WINDOW
    with _attempts_lock:
        recent = [t for t in _attempts.get(ip, []) if t > cutoff]
        if len(recent) >= REGISTER_LIMIT:
            _attempts[ip] = recent
            return False
        recent.append(time.monotonic())
        _attempts[ip] = recent
        if len(_attempts) > 5000:
            for key in [k for k, v in _attempts.items() if not v or v[-1] <= cutoff]:
                del _attempts[key]
        return True


# ----------------------------------------------------------------------------- sign-up and sign-in
def register(username: str, password: str, display_name: str = "") -> None:
    """Leave a sign-up request. It stays "pending" until a superuser approves it."""
    if not registration_open():
        raise AccountError("Приём заявок закрыт. Обратитесь к администратору.")
    name = _valid_username(username)
    _valid_password(password)
    with SessionLocal() as s:
        if s.query(User).filter_by(status="pending").count() >= MAX_PENDING:
            raise AccountError("Слишком много необработанных заявок. Попробуйте позже.")
        if s.query(User).filter_by(username=name).first():
            raise AccountError("Этот логин уже занят.")
        s.add(User(username=name, display_name=(display_name or "").strip()[:120], password_hash=hash_password(password),
                   role="user", status="pending"))
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            raise AccountError("Этот логин уже занят.") from None


def authenticate(username: str, password: str):
    """Returns (user, "") on success, else (None, code): "1" wrong login or password (also for unknown logins),
    or the account status ("pending", "rejected", "disabled") once the password was right."""
    name = normalize(username)
    with SessionLocal() as s:
        user = s.query(User).filter_by(username=name).first()
        if user is None:
            verify_password(password, _DUMMY_HASH)
            return None, "1"
        if not verify_password(password, user.password_hash):
            return None, "1"
        if user.status != "approved":
            return None, user.status
        user.last_login_at = now()
        s.commit()
        return user, ""


def ensure_superuser() -> None:
    """The ADMIN_USERNAME/ADMIN_PASSWORD account always exists, is approved and is a superuser."""
    name = normalize(config.ADMIN_USERNAME)
    if not name:
        return
    with SessionLocal() as s:
        user = s.query(User).filter_by(username=name).first()
        if user is None:
            user = User(username=name, display_name=config.ADMIN_USERNAME.strip(),
                        password_hash=hash_password(config.ADMIN_PASSWORD))
            s.add(user)
        elif not verify_password(config.ADMIN_PASSWORD, user.password_hash):
            user.password_hash = hash_password(config.ADMIN_PASSWORD)
        user.role, user.status = "superuser", "approved"
        s.commit()


# ----------------------------------------------------------------------------- administration
def create_user(username: str, password: str, role: str, by: str, display_name: str = "") -> None:
    """A superuser adds an account directly (approved at once)."""
    name = _valid_username(username)
    _valid_password(password)
    if role not in ROLES:
        raise AccountError("Неизвестная роль.")
    with SessionLocal() as s:
        if s.query(User).filter_by(username=name).first():
            raise AccountError("Этот логин уже занят.")
        s.add(User(username=name, display_name=(display_name or "").strip()[:120], password_hash=hash_password(password),
                   role=role, status="approved", decided_at=now(), decided_by=by))
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            raise AccountError("Этот логин уже занят.") from None


def apply(user_id: int, action: str, by: str) -> str:
    """Approve, reject, disable, promote, demote or delete an account. Returns a message for the panel."""
    if action not in ACTIONS:
        raise AccountError("Неизвестное действие.")
    with SessionLocal() as s:
        user = s.get(User, user_id)
        if user is None:
            raise AccountError("Пользователь не найден.")
        own = user.username == by
        active_superuser = user.role == "superuser" and user.status == "approved"
        other_superusers = s.query(User).filter(User.role == "superuser", User.status == "approved",
                                                User.id != user.id).count()
        last_superuser = active_superuser and other_superusers == 0
        if action == "approve":
            if user.status == "approved":
                raise AccountError("Пользователь уже подтверждён.")
            user.status, done = "approved", "доступ открыт"
        elif action == "reject":
            if user.status != "pending":
                raise AccountError("Отклонить можно только ожидающую заявку.")
            user.status, done = "rejected", "заявка отклонена"
        elif action == "disable":
            if user.status != "approved":
                raise AccountError("Отключить можно только действующий аккаунт.")
            if own:
                raise AccountError("Нельзя отключить самого себя.")
            if last_superuser:
                raise AccountError("Нельзя отключить последнего суперпользователя.")
            user.status, done = "disabled", "доступ отключён"
        elif action == "promote":
            if user.status != "approved":
                raise AccountError("Сначала подтвердите пользователя.")
            if user.role == "superuser":
                raise AccountError("Он уже суперпользователь.")
            user.role, done = "superuser", "теперь суперпользователь"
        elif action == "demote":
            if user.role != "superuser":
                raise AccountError("Он не суперпользователь.")
            if own:
                raise AccountError("Нельзя снять права с самого себя.")
            if last_superuser:
                raise AccountError("Нельзя снять права с последнего суперпользователя.")
            user.role, done = "user", "права суперпользователя сняты"
        else:  # delete
            if own:
                raise AccountError("Нельзя удалить самого себя.")
            if last_superuser:
                raise AccountError("Нельзя удалить последнего суперпользователя.")
            username = user.username
            s.delete(user)
            s.commit()
            return f"{username}: аккаунт удалён"
        user.decided_at, user.decided_by = now(), by
        s.commit()
        return f"{user.username}: {done}"


def pending_count() -> int:
    with SessionLocal() as s:
        return s.query(User).filter_by(status="pending").count()


def _local(moment: dt.datetime | None) -> str:
    if moment is None:
        return "-"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    try:
        zone = zoneinfo.ZoneInfo(config.TIMEZONE)
    except zoneinfo.ZoneInfoNotFoundError:
        zone = dt.timezone.utc
    return moment.astimezone(zone).strftime("%d.%m.%Y %H:%M")


def list_users() -> list[dict]:
    with SessionLocal() as s:
        rows = s.query(User).all()
    rows.sort(key=lambda u: (u.status != "pending", u.role != "superuser", u.username))
    return [{"id": u.id, "username": u.username, "display_name": u.display_name or "", "role": u.role,
             "status": u.status, "created": _local(u.created_at), "last_login": _local(u.last_login_at),
             "decided_by": u.decided_by or ""} for u in rows]
