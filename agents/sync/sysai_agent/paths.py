"""Where things live on the computer."""
import os
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
PLATFORM = "windows" if WINDOWS else "linux"


def home() -> Path:
    """SYSAI_HOME: %LOCALAPPDATA%\\SYSAI on Windows, ~/.sysai elsewhere. The MCP wrapper, started by Hermes with a
    trimmed environment, finds it by its access file."""
    env = os.environ.get("SYSAI_HOME", "").strip()
    if env:
        return Path(env)
    access = os.environ.get("SYSAI_ACCESS_FILE", "").strip()
    if access:
        return Path(access).parent
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA", "").strip()
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "SYSAI"
    return Path.home() / ".sysai"


def hermes_root() -> Path:
    """The Hermes home the profiles live in: %LOCALAPPDATA%\\hermes on Windows, ~/.hermes elsewhere."""
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA", "").strip()
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "hermes"
    return Path.home() / ".hermes"


def hermes_profile_dir(agent: dict) -> Path:
    """The folder of the agent's Hermes profile: the one the installer found (`hermes profile show`), else the
    default place of a named profile."""
    if agent.get("hermes_home"):
        return Path(agent["hermes_home"])
    profile = agent.get("profile") or agent["agent"]
    return hermes_root() if profile == "default" else hermes_root() / "profiles" / profile
