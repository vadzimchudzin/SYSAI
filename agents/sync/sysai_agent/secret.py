"""The agent's token: Windows Credential Manager (keyring); a file only the user reads when there is no keyring."""
import os
from pathlib import Path

from . import fsutil

SERVICE = "SYSAI agent"


def _keyring():
    if os.environ.get("SYSAI_KEYRING", "1") == "0":
        return None
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:  # noqa: BLE001  no keyring, no backend: the file
        return None


def save(home: Path, agent: str, token: str) -> str:
    """Where the token went: «keyring» or «file»."""
    path = Path(home) / "token"
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, agent, token)
            path.unlink(missing_ok=True)
            return "keyring"
        except Exception:  # noqa: BLE001
            pass
    fsutil.atomic_write(path, token, private=True)
    return "file"


def load(home: Path, agent: str) -> str:
    env = os.environ.get("SYSAI_AGENT_TOKEN", "").strip()
    if env:
        return env
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE, agent)
            if value:
                return value
        except Exception:  # noqa: BLE001
            pass
    return fsutil.read_text(Path(home) / "token").strip()


def forget(home: Path, agent: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, agent)
        except Exception:  # noqa: BLE001
            pass
    (Path(home) / "token").unlink(missing_ok=True)
