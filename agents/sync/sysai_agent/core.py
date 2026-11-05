"""Connection, one synchronization and the pulse; the loop of «sysai-agent run»."""
import datetime as dt
import logging
import logging.handlers
import os
import platform
import shutil
import sys
import time
from pathlib import Path

from . import __version__, fsutil, hermes, hubsync, paths, secret, signing
from .client import ApiError, Client

log = logging.getLogger("sysai_agent")
EVERY = 300          # seconds between pulses
FULL_EVERY = 3600    # a full synchronization at least this often, even when nothing changed on the server


class SyncError(Exception):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def setup_logging(home: Path, console: bool = True) -> None:
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    (Path(home) / "logs").mkdir(parents=True, exist_ok=True)
    file = logging.handlers.RotatingFileHandler(Path(home) / "logs" / "sync.log", maxBytes=1_000_000, backupCount=3,
                                                encoding="utf-8")
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(file)
    if console and sys.stderr is not None:
        out = logging.StreamHandler()
        out.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(out)


def load_agent(home: Path) -> dict:
    cfg = fsutil.read_json(Path(home) / "agent.json", None)
    if not isinstance(cfg, dict) or not cfg.get("agent") or not cfg.get("server") or not cfg.get("public_key"):
        raise SyncError("Компьютер не подключён: получите код на странице агента в SYSAI и выполните "
                        "sysai-agent connect --server <адрес SYSAI> --code <код>")
    return cfg


_hermes_version = {"at": 0.0, "value": ""}


def versions() -> dict:
    """Shown on the agent's page; Hermes is asked once an hour."""
    if time.time() - _hermes_version["at"] > 3600:
        _hermes_version.update(at=time.time(), value=hermes.version())
    out = {"sysai-agent": __version__, "python": platform.python_version()}
    if _hermes_version["value"]:
        out["hermes"] = _hermes_version["value"]
    return out


def launcher() -> str:
    """The full path of «sysai-agent», for the MCP servers in the Hermes configuration."""
    found = shutil.which("sysai-agent")
    if found:
        return found
    here = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if here and here.stem.lower().startswith("sysai-agent") and here.exists():
        return str(here.with_name("sysai-agent" + here.suffix))
    return ""


def connect(server: str, code: str, home: Path | None = None, transport=None, profile: str = "",
            hermes_home: str = "") -> dict:
    """Trades the one-time code for the agent's token; pins the key the rules are signed with."""
    home = Path(home or paths.home())
    server = (server or "").strip().rstrip("/")
    if not server.startswith(("https://", "http://")):
        raise SyncError("Адрес SYSAI — вида https://sysai.example.ru")
    if not (code or "").strip():
        raise SyncError("Нет кода подключения: получите его на странице агента в SYSAI")
    client = Client(server, transport=transport)
    try:
        answer = client.json("POST", "/agents/api/connect", {"code": code.strip(), "host": platform.node(),
                                                             "harness": "hermes", "versions": versions()})
    except ApiError as exc:
        raise SyncError(str(exc)) from exc
    key = answer.get("public_key") or {}
    if not answer.get("agent") or not answer.get("token") or not key.get("key"):
        raise SyncError("SYSAI ответил не так, как ждали: обновите синхронизатор (запустите установщик снова)")
    home.mkdir(parents=True, exist_ok=True)
    old = fsutil.read_json(home / "agent.json", {}) or {}
    same = old.get("agent") == answer["agent"]
    cfg = {"agent": answer["agent"], "server": server, "public_key": key["key"], "key_id": key.get("key_id", ""),
           "profile": profile or (old.get("profile") if same else "") or answer["agent"],
           "hermes_home": hermes_home or (old.get("hermes_home") if same else "") or "",
           "host": platform.node(), "connected_at": now()}
    where = secret.save(home, cfg["agent"], answer["token"])
    fsutil.write_json(home / "agent.json", cfg)
    if not same:
        fsutil.write_json(home / "state.json", {})
    log.info("Подключено: агент %s, сервер %s (токен: %s)", cfg["agent"], server,
             "хранилище Windows" if where == "keyring" else "файл в папке SYSAI")
    return {**cfg, "token_store": where}


def client_for(home: Path, cfg: dict, transport=None) -> tuple[Client, str]:
    token = secret.load(home, cfg["agent"])
    if not token:
        raise SyncError("Нет токена агента: подключите компьютер заново (новый код на странице агента)")
    return Client(cfg["server"], token, transport), token


def _offline_or_refused(exc: ApiError) -> SyncError:
    if exc.status == 401:
        return SyncError("SYSAI не принял токен агента: агента отключили или подключили на другом компьютере. "
                         "Получите новый код на странице агента и запустите установщик снова")
    return SyncError(str(exc))


def sync_once(home: Path | None = None, transport=None, restart: bool = True) -> dict:
    """Everything once: the rules (signature checked) → the Hermes profile; the hub; proposals; the pulse."""
    home = Path(home or paths.home())
    cfg = load_agent(home)
    client, token = client_for(home, cfg, transport)
    state = fsutil.read_json(home / "state.json", {}) or {}
    report = {"agent": cfg["agent"], "changed": [], "hub": {}, "proposals": [], "errors": [], "restart": "", "access": ""}
    try:
        signed = client.json("GET", "/agents/api/config")
    except ApiError as exc:
        raise _offline_or_refused(exc) from exc
    if not signing.verify(signed, cfg["public_key"], cfg.get("key_id", "")):
        raise SyncError("Подпись настроек не сходится с ключом SYSAI, полученным при подключении: ничего не применено. "
                        "Если ключ сервера сменили — подключите компьютер заново")
    doc = signed["doc"]
    if doc.get("agent") != cfg["agent"]:
        raise SyncError(f"Настройки для другого агента ({doc.get('agent')}): ничего не применено")
    fsutil.write_json(home / "access.json", signed)  # the MCP servers check their calls by it
    harness = doc.get("harness_config") or {}
    fsutil.write_json(home / "sysai.json", harness.get("sysai") or {})
    profile_dir = paths.hermes_profile_dir(cfg)
    applied = True
    if doc.get("harness") == "hermes" and harness:
        try:
            report["changed"] = hermes.apply(profile_dir, harness, doc.get("soul") or "", home, token, cfg["server"],
                                             launcher())
        except (OSError, ValueError) as exc:
            applied = False
            report["errors"].append(f"Профиль Hermes ({profile_dir}): {exc}")
    try:
        manifest = client.json("GET", "/agents/api/hub/manifest")
        report["hub"] = hubsync.sync(home, client, manifest, state, profile_dir)
        report["errors"] += report["hub"]["errors"]
        state["conflicts"] = report["hub"]["conflicts"]
        if report["hub"]["errors"]:  # not taken whole: the next pulse sees another revision and tries again
            state.pop("hub_rev", None)
        else:
            state["hub_rev"] = manifest.get("rev", "")
    except ApiError as exc:
        report["errors"].append(f"SYSAI_Hub: {exc}")
    try:
        report["proposals"] = hubsync.propose(home, client, state, doc.get("rules") or {})
    except ApiError as exc:
        report["errors"].append(f"Предложения в хаб: {exc}")
    if applied:
        if doc.get("rev", "") != state.get("access_rev", ""):
            report["access"] = doc.get("rev", "")
        state["access_rev"] = doc.get("rev", "")
        state["applied_at"] = now()
    if report["changed"] or (report["hub"] or {}).get("changed") or (report["hub"] or {}).get("removed"):
        state["restart_pending"] = True
    if state.get("restart_pending") and not hermes.gateway_running(profile_dir):
        state.pop("restart_pending", None)  # nothing to restart: Hermes reads the new settings when it starts
        report["restart"] = "шлюз Hermes не запущен: новые настройки он возьмёт при запуске"
    elif restart and state.get("restart_pending"):
        done, said = hermes.restart(cfg.get("profile") or cfg["agent"], profile_dir)
        report["restart"] = said
        if done:
            state.pop("restart_pending", None)
        else:
            log.info("Hermes не перезапущен (%s): попробую при следующей синхронизации", said)
    state["synced_at"] = now()
    state["error"] = "; ".join(report["errors"])[:2000]
    try:
        report["pulse"] = pulse(home, client, state)
    except ApiError as exc:
        report["errors"].append(f"Пульс: {exc}")
    fsutil.write_json(home / "state.json", state)  # after the pulse: its time is kept too («sysai-agent status»)
    return report


def pulse(home: Path, client: Client, state: dict, error: str | None = None) -> dict:
    data = {"access_rev": state.get("access_rev", ""), "versions": versions(), "host": platform.node(),
            "hub": hubsync.summary(state), "error": state.get("error", "") if error is None else error}
    answer = client.json("POST", "/agents/api/heartbeat", data)
    state["pulse_at"] = now()
    return answer


def _lock(home: Path):
    """One «run» per computer: a lock file with the process id; a dead owner's lock is taken over."""
    path = Path(home) / "run.lock"
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return path
        except FileExistsError:
            try:
                pid = int(fsutil.read_text(path).strip() or 0)
            except ValueError:
                pid = 0
            if pid and pid != os.getpid() and hermes._alive(pid):
                return None
            path.unlink(missing_ok=True)
    return None


def run(home: Path | None = None, every: int = EVERY, transport=None, cycles: int = 0, sleep=time.sleep) -> int:
    """The loop: a full synchronization at the start, then a pulse every `every` seconds; a full one again when the
    server's revisions differ from the applied ones, after an error, or once an hour. `cycles` > 0 — for tests."""
    home = Path(home or paths.home())
    (home / "stop").unlink(missing_ok=True)
    lock = _lock(home)
    if lock is None:
        log.info("Синхронизатор уже работает на этом компьютере")
        return 0
    try:
        last_full, failed, n = 0.0, True, 0
        while True:
            n += 1
            try:
                cfg = load_agent(home)
                client, _ = client_for(home, cfg, transport)
                state = fsutil.read_json(home / "state.json", {}) or {}
                need = failed or time.time() - last_full > FULL_EVERY or state.get("restart_pending")
                if not need:
                    answer = pulse(home, client, state)
                    fsutil.write_json(home / "state.json", state)
                    need = answer.get("access_rev") != state.get("access_rev") or \
                        answer.get("hub_rev") != state.get("hub_rev")
                if need:
                    report = sync_once(home, transport)
                    last_full, failed = time.time(), False
                    if report["changed"] or (report["hub"] or {}).get("changed") or report["proposals"] or \
                            report.get("access"):
                        log.info("Синхронизировано: %s", describe(report))
                    for e in report["errors"]:
                        log.warning("%s", e)
            except (SyncError, ApiError, OSError) as exc:
                failed = True
                log.warning("%s", exc)
            if cycles and n >= cycles:
                return 0
            if _wait(home, every, sleep):
                log.info("Синхронизатор остановлен (его попросили: установщик или sysai-agent stop)")
                return 0
    finally:
        lock.unlink(missing_ok=True)


def _wait(home: Path, seconds: int, sleep) -> bool:
    """Sleeps; True when asked to stop (the file «stop» in the SYSAI folder — the installer does so before updating)."""
    stop = Path(home) / "stop"
    for _ in range(max(1, int(seconds))):
        if stop.exists():
            stop.unlink(missing_ok=True)
            return True
        sleep(1)
    return False


def stop(home: Path | None = None, wait: int = 60, sleep=time.sleep) -> bool:
    """Asks the running loop to stop; True once it has (or none ran)."""
    home = Path(home or paths.home())
    lock = home / "run.lock"
    if not lock.exists():
        return True
    (home / "stop").write_text("stop", encoding="utf-8")
    for _ in range(wait):
        if not lock.exists():
            return True
        sleep(1)
    return not lock.exists()


def describe(report: dict) -> str:
    parts = []
    if report.get("changed"):
        parts.append("Hermes: " + ", ".join(report["changed"]))
    hub = report.get("hub") or {}
    if hub.get("changed"):
        parts.append("из хаба: " + ", ".join(hub["changed"]))
    if hub.get("removed"):
        parts.append("убрано: " + ", ".join(hub["removed"]))
    if report.get("proposals"):
        parts.append("предложено в хаб: " + ", ".join(report["proposals"]))
    if report.get("access") and not report.get("changed"):
        parts.append("доступы: " + report["access"])
    return "; ".join(parts) or "без изменений"


def status(home: Path | None = None) -> dict:
    home = Path(home or paths.home())
    cfg = fsutil.read_json(home / "agent.json", {}) or {}
    state = fsutil.read_json(home / "state.json", {}) or {}
    out = {"home": str(home), "connected": bool(cfg.get("agent")), "agent": cfg.get("agent", ""),
           "server": cfg.get("server", ""), "version": __version__, "access_rev": state.get("access_rev", ""),
           "hub_rev": state.get("hub_rev", ""), "synced_at": state.get("synced_at", ""), "pulse_at": state.get("pulse_at", ""),
           "error": state.get("error", ""), "skills": sorted(state.get("skills") or {}), "tools": sorted(state.get("tools") or {}),
           "conflicts": state.get("conflicts") or [], "restart_pending": bool(state.get("restart_pending"))}
    if cfg.get("agent"):
        out["hermes"] = hermes.profile_info(paths.hermes_profile_dir(cfg))
    return out
