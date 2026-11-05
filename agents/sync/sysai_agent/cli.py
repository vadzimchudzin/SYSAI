"""sysai-agent connect | sync | run | status | stop | desktop | mcp <name> | hook | version."""
import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, fsutil, paths


def _parser() -> argparse.ArgumentParser:
    from . import core
    p = argparse.ArgumentParser(prog="sysai-agent", description="Синхронизатор агента SYSAI на компьютере сотрудника")
    p.add_argument("--home", help="папка SYSAI на компьютере (по умолчанию %%LOCALAPPDATA%%\\SYSAI)")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("connect", help="подключить компьютер одноразовым кодом со страницы агента")
    c.add_argument("--server", default=os.environ.get("SYSAI_URL", ""), help="адрес SYSAI, https://…")
    c.add_argument("--code", default="", help="код подключения (или переменная SYSAI_CODE)")
    c.add_argument("--profile", default="", help="профиль Hermes (по умолчанию — имя агента)")
    c.add_argument("--hermes-home", default="", help="папка профиля Hermes, если не стандартная")
    s = sub.add_parser("sync", help="синхронизировать сейчас: доступы, характер, навыки, пульс")
    s.add_argument("--hermes-home", default="", help="запомнить папку профиля Hermes и синхронизировать")
    s.add_argument("--no-restart", action="store_true", help="не перезапускать шлюз Hermes")
    r = sub.add_parser("run", help="работать в фоне: пульс каждые 5 минут, синхронизация при изменениях")
    r.add_argument("--every", type=int, default=core.EVERY, help="секунд между пульсами (по умолчанию 300)")
    st = sub.add_parser("status", help="что на этом компьютере")
    st.add_argument("--json", action="store_true", help="в JSON (для установщика)")
    m = sub.add_parser("mcp", help="запустить MCP-сервер из хаба под охраной доступов (так его зовёт Hermes)")
    m.add_argument("name")
    sub.add_parser("hook", help="проверить действие Hermes по доступам SYSAI (так его спрашивает Hermes перед действием)")
    sub.add_parser("stop", help="остановить фоновую синхронизацию (до следующего входа в Windows)")
    d = sub.add_parser("desktop", help="открыть окно агента «Агент SYSAI» (поставит его, если его нет)")
    d.add_argument("--install-only", action="store_true", help="только поставить или обновить окно и ярлыки (так делает установщик)")
    d.add_argument("files", nargs="*", help="файлы — сразу показать в окне")
    sub.add_parser("version", help="версия")
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv == ["hook"]:  # asked before every action of Hermes: no more than it needs, stdout is the answer
        from . import hook
        return hook.main(paths.home())
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    from . import core, mcpwrap
    args = _parser().parse_args(argv)
    home = Path(args.home) if args.home else paths.home()
    if args.cmd == "hook":
        from . import hook
        return hook.main(home)
    if args.cmd == "mcp":  # stdout is the MCP channel: nothing else may be written there
        return mcpwrap.serve(args.name, home)
    if args.cmd == "version":
        print(__version__)
        return 0
    if args.cmd == "stop":
        done = core.stop(home)
        print("Синхронизатор остановлен" if done else "Синхронизатор не остановился за минуту")
        return 0 if done else 1
    if args.cmd == "status":
        info = core.status(home)
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            _print_status(info)
        return 0 if info["connected"] else 1
    core.setup_logging(home, console=True)
    try:
        if args.cmd == "connect":
            code = args.code or os.environ.get("SYSAI_CODE", "")
            if not code and sys.stdin and sys.stdin.isatty():
                code = input("Код подключения со страницы агента: ")
            core.connect(args.server, code, home, profile=args.profile, hermes_home=args.hermes_home)
            return 0
        if args.cmd == "sync":
            if args.hermes_home:
                cfg = core.load_agent(home)
                cfg["hermes_home"] = str(Path(args.hermes_home))
                fsutil.write_json(home / "agent.json", cfg)
            report = core.sync_once(home, restart=not args.no_restart)
            core.log.info("Синхронизировано: %s", core.describe(report))
            if report.get("restart"):
                core.log.info("Hermes: %s", report["restart"])
            for e in report["errors"]:
                core.log.warning("Ошибка: %s", e)
            return 1 if report["errors"] else 0
        if args.cmd == "run":
            return core.run(home, every=max(30, args.every))
        if args.cmd == "desktop":
            from . import desktop
            try:
                return desktop.cli(home, args.files, args.install_only)
            except (desktop.DesktopError, OSError) as exc:
                core.log.error("Окно агента: %s", exc)
                return 1
    except core.SyncError as exc:
        core.log.error("%s", exc)
        return 1
    _parser().print_help()
    return 2


def main_windowless() -> int:
    """«sysai-agentw»: the background loop without a console window (started at Windows logon); logs only."""
    from . import core
    home = paths.home()
    core.setup_logging(home, console=False)
    args = sys.argv[1:] or ["run"]
    if args[0] != "run":
        return main(args)
    try:
        return core.run(home)
    except Exception as exc:  # noqa: BLE001  the background loop must leave a trace, not a dialog
        core.log.exception("Синхронизатор остановился: %s", exc)
        return 1


def _print_status(info: dict) -> None:
    if not info["connected"]:
        print("Компьютер не подключён к SYSAI. Код подключения — на странице агента в SYSAI.")
        return
    print(f"Агент: {info['agent']}  ·  сервер: {info['server']}  ·  синхронизатор {info['version']}")
    print(f"Доступы: {info['access_rev'] or '—'}  ·  синхронизация: {info['synced_at'] or '—'}  ·  пульс: {info['pulse_at'] or '—'}")
    h = info.get("hermes") or {}
    print(f"Профиль Hermes: {h.get('dir', '—')}  ·  модель: {h.get('model') or '—'}  ·  "
          f"шлюз {'работает' if h.get('gateway') else 'не запущен'}")
    print("Проверка действий агента по доступам SYSAI: " + ("включена" if h.get("hook") else "нет — sysai-agent sync"))
    print(f"Навыки из хаба: {', '.join(info['skills']) or '—'}")
    print(f"Инструменты: {', '.join(info['tools']) or '—'}")
    if info["conflicts"]:
        print(f"Совпадают по имени со своими навыками профиля: {', '.join(info['conflicts'])}")
    if info["restart_pending"]:
        print("Hermes ждёт перезапуска (идёт задача?) — синхронизатор повторит")
    window = info.get("desktop") or {}
    print(f"Окно агента «Агент SYSAI»: {window['version']} (Electron {window['electron']})" if window.get("version") else
          "Окно агента «Агент SYSAI»: не установлено (sysai-agent desktop)")
    if info["error"]:
        print(f"Последняя ошибка: {info['error']}")
    print(f"Папка SYSAI: {info['home']}")
