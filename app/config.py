"""Environment configuration. Secrets live only in environment variables (.env on the server)."""
import hashlib
import os
import pathlib


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", BASE_DIR.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def database_url(value: str | None, data_dir: pathlib.Path) -> str:
    url = (value or "").strip() or f"sqlite:///{data_dir / 'sysai.db'}"
    # Provider URLs omit the installed SQLAlchemy psycopg driver name.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
            break
    # Neon only accepts TLS connections; a string pasted without sslmode must not fail on that.
    if url.startswith("postgresql") and ".neon.tech" in url.split("?")[0] and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


DATABASE_URL = database_url(os.getenv("DATABASE_URL"), DATA_DIR)
# Seconds after boot before the heavy background work (Telegram account, queues) starts: the platform's port and health
# checks must pass first, a free instance has a tenth of a CPU and 512 MB (Telethon alone takes ~130 MB to import).
STARTUP_DELAY = int(os.getenv("STARTUP_DELAY", "20"))

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "SYSAI")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "SYSAI")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
PYANNOTEAI_API_KEY = os.getenv("PYANNOTEAI_API_KEY", "")  # legacy only
VOICE_SERVICE_URL = os.getenv("SYSAI_VOICE_SERVICE_URL", "").strip().rstrip("/")
VOICE_SERVICE_TOKEN = os.getenv("SYSAI_VOICE_SERVICE_TOKEN", "").strip()
try:
    VOICE_MATCH_THRESHOLD = float(os.getenv("SYSAI_VOICE_MATCH_THRESHOLD", "0.55"))
except ValueError:
    VOICE_MATCH_THRESHOLD = 0.55
try:
    VOICE_MATCH_MARGIN = float(os.getenv("SYSAI_VOICE_MATCH_MARGIN", "0.08"))
except ValueError:
    VOICE_MATCH_MARGIN = 0.08

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_MODE = os.getenv("TELEGRAM_MODE", "webhook")  # webhook | polling | off
# Telegram allows only [A-Za-z0-9_-] in the secret token, so derive it from SECRET_KEY
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET") or hashlib.sha256(SECRET_KEY.encode()).hexdigest()[:48]
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or
              (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}" if os.getenv("RAILWAY_PUBLIC_DOMAIN") else "")).rstrip("/")

BITRIX_WEBHOOK_URL = os.getenv("BITRIX_WEBHOOK_URL", "").rstrip("/")

MIC_INBOX_DIR = pathlib.Path(os.getenv("MIC_INBOX_DIR", DATA_DIR / "inbox"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Minsk")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
WORKERS = int(os.getenv("WORKERS", "2"))
TESTING = _bool("SYSAI_TESTING")

DEFAULT_SECRET_KEY = "change-me-in-production"
DEFAULT_ADMIN_PASSWORD = "SYSAI"


def weak_secret_key() -> bool:
    """The key is the public default (or empty): anyone who has read the repository could forge a session cookie."""
    return not TESTING and SECRET_KEY.strip() in ("", DEFAULT_SECRET_KEY)


def weak_admin_password() -> bool:
    """ADMIN_PASSWORD is the default from the README (or empty): the superuser login stays closed until it is changed."""
    return not TESTING and ADMIN_PASSWORD.strip().upper() in ("", DEFAULT_ADMIN_PASSWORD.upper())


def _panel(key: str) -> str:
    """Value pasted in the web panel (Settings → Подключения); env variable is the fallback."""
    try:
        from . import settings_store
        return (settings_store.get(key) or "").strip()
    except Exception:  # database not ready yet
        return ""


def openrouter_key() -> str:
    return _panel("openrouter_api_key") or OPENROUTER_API_KEY


def pyannoteai_key() -> str:
    """Deprecated compatibility helper; the active provider is the Vercel microservice."""
    return _panel("pyannoteai_api_key") or PYANNOTEAI_API_KEY


def voice_service_url() -> str:
    """Server-to-server endpoint of the dedicated speaker-embedding service."""
    return VOICE_SERVICE_URL


def voice_service_token() -> str:
    """Shared Render→Vercel secret; never expose it to browser templates."""
    return VOICE_SERVICE_TOKEN


def voice_service_enabled() -> bool:
    return bool(voice_service_url() and voice_service_token())


def telegram_token() -> str:
    return _panel("telegram_bot_token") or TELEGRAM_BOT_TOKEN


def bitrix_url() -> str:
    return (_panel("bitrix_webhook_url") or BITRIX_WEBHOOK_URL).rstrip("/")


def llm_enabled() -> bool:
    return bool(openrouter_key())


def telegram_enabled() -> bool:
    return bool(telegram_token()) and TELEGRAM_MODE != "off"
