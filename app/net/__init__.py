"""The agent network (docs/opo/TASKS.md): agents on the employees' computers connect to SYSAI, get their access from
the settings (no confirmations in Telegram), take skills from the one shared SYSAI_Hub, use the SYSAI MCP server and
reach the models only through the SYSAI gateway. Switched on by the setting ``agents_on``; switched off, every address
of the network answers 404 and SYSAI works as before."""
from .. import settings_store


def enabled() -> bool:
    return bool(settings_store.get("agents_on"))
