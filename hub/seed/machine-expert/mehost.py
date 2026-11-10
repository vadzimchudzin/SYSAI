"""Finding EcoStruxure Machine Expert on the computer and running one job in it.

A job is a JSON file; Machine Expert is started with the script `me_job.py`, which reads the job (its path is in the
environment variable SYSAI_ME_JOB), does it through the scripting interface and writes `result.json` next to it.
The process is started hidden, without a window, and is closed by the script; one job at a time (a lock file).

How Machine Expert takes a script differs between installations, so the ways are tried in order until one of them
leaves a result:

1. `LogicBuilderShell.exe <script>` — the console of Machine Expert for scripts;
2. `LogicBuilder.exe --runscript=<script> --noUI [--profile=<name>]` — the program itself, without the interface;
3. `CODESYS.exe --profile=<name> --noUI --runscript=<script>` — plain CODESYS (the same engine).

SYSAI_ME_EXE names the program, SYSAI_ME_CMD the whole command line (`{exe}`, `{script}`, `{profile}` are put in).
"""
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'me_job.py')
DEFAULT_TIMEOUT = 900
PATTERNS = (
    r'{pf}\Schneider Electric\EcoStruxure Machine Expert\*\LogicBuilderShell.exe',
    r'{pf}\Schneider Electric\EcoStruxure Machine Expert\*\LogicBuilder.exe',
    r'{pf}\Schneider Electric\EcoStruxure Machine Expert\LogicBuilderShell.exe',
    r'{pf}\Schneider Electric\EcoStruxure Machine Expert\LogicBuilder.exe',
    r'{pf}\Schneider Electric\SoMachine Software\*\LogicBuilderShell.exe',
    r'{pf}\Schneider Electric\SoMachine Software\*\LogicBuilder.exe',
    r'{pf}\CODESYS*\CODESYS\Common\CODESYS.exe',
    r'{pf}\3S CODESYS\CODESYS\Common\CODESYS.exe',
)


class HostError(RuntimeError):
    pass


def _program_files() -> list:
    out = []
    for key in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432'):
        v = os.environ.get(key)
        if v and v not in out:
            out.append(v)
    return out or [r'C:\Program Files', r'C:\Program Files (x86)']


def _kind(exe: str) -> str:
    base = os.path.basename(exe).lower()
    return 'shell' if base == 'logicbuildershell.exe' else 'codesys' if base == 'codesys.exe' else 'logicbuilder'


def _version(exe: str) -> str:
    m = re.search(r'(V?\d+(?:\.\d+)+|SP\d+)', os.path.basename(os.path.dirname(exe)) + ' ' + os.path.dirname(exe), re.I)
    return m.group(1) if m else ''


def _profile(exe: str) -> str:
    """The newest profile of the installation (CODESYS and LogicBuilder ask for one when several are installed)."""
    folder = os.path.dirname(exe)
    names = []
    for root in (folder, os.path.dirname(folder), os.path.dirname(os.path.dirname(folder))):
        names += [os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(root, 'Profiles', '*.profile'))]
    return sorted(set(names))[-1] if names else ''


def installs() -> list:
    """Programs that can run the script, the preferred first: [{'exe', 'kind', 'product', 'version', 'profile'}]."""
    found, seen = [], set()
    named = os.environ.get('SYSAI_ME_EXE') or ''
    paths = [named] if named else []
    for pf in _program_files():
        for pattern in PATTERNS:
            paths += sorted(glob.glob(pattern.format(pf=pf)), reverse=True)
    for exe in paths:
        key = os.path.normcase(os.path.abspath(exe))
        if key in seen or not os.path.isfile(exe):
            continue
        seen.add(key)
        kind = _kind(exe)
        found.append({'exe': exe, 'kind': kind, 'product': 'CODESYS' if kind == 'codesys' else 'EcoStruxure Machine Expert',
                      'version': _version(exe), 'profile': _profile(exe)})
    return found


def commands(script: str) -> list:
    """The command lines to try, in order: [(title, argv or command string)]."""
    template = os.environ.get('SYSAI_ME_CMD') or ''
    found = installs()
    if template:
        first = found[0] if found else {'exe': '', 'profile': ''}
        line = template.format(exe=first['exe'], script=script, profile=first['profile'])
        return [('SYSAI_ME_CMD', line if os.name == 'nt' else shlex.split(line))]
    out = []
    for inst in found:
        if inst['kind'] == 'shell':
            out.append((os.path.basename(inst['exe']), [inst['exe'], script]))
        else:
            argv = [inst['exe']] + ([f'--profile={inst["profile"]}'] if inst['profile'] else []) + ['--noUI', f'--runscript={script}']
            out.append((os.path.basename(inst['exe']) + ' --runscript', argv))
    return out


def work_dir() -> str:
    base = os.environ.get('SYSAI_ME_WORK') or os.path.join(
        os.environ.get('LOCALAPPDATA') or tempfile.gettempdir(), 'SYSAI', 'machine-expert')
    os.makedirs(base, exist_ok=True)
    return base


class _Lock:
    """One job at a time on this computer: Machine Expert keeps one primary project."""

    def __init__(self, wait: float = 120):
        self.path, self.wait, self.fd = os.path.join(work_dir(), 'busy.lock'), wait, None

    def __enter__(self):
        end = time.monotonic() + self.wait
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > 3600:      # a lock left by a killed process
                        os.remove(self.path)
                        continue
                except OSError:
                    continue
                if time.monotonic() > end:
                    raise HostError('Machine Expert занят другой задачей этого инструмента — повторите позже') from None
                time.sleep(1)

    def __exit__(self, *exc):
        try:
            os.close(self.fd)
            os.remove(self.path)
        except OSError:
            pass


def _tail(path: str, limit: int = 1500) -> str:
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return ''
    for enc in ('utf-8', 'cp1251', 'cp866'):
        try:
            return data.decode(enc)[-limit:]
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', 'replace')[-limit:]


def run(job: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Do the job in Machine Expert. -> the result of the script ({'ok', 'result' | 'error', 'warnings'}) plus
    'how' (the command that worked) and 'seconds'."""
    todo = commands(SCRIPT)
    if not todo:
        raise HostError('EcoStruxure Machine Expert на этом компьютере не найден. Поставьте его или назовите программу: '
                        r'переменная SYSAI_ME_EXE = путь к LogicBuilderShell.exe (обычно C:\Program Files\Schneider '
                        r'Electric\EcoStruxure Machine Expert\<версия>\).')
    folder = os.path.join(work_dir(), 'jobs', time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6])
    os.makedirs(folder)
    job_file, result_file = os.path.join(folder, 'job.json'), os.path.join(folder, 'result.json')
    with open(job_file, 'w', encoding='utf-8') as f:
        json.dump(job, f, ensure_ascii=False)
    env = dict(os.environ, SYSAI_ME_JOB=job_file, PYTHONIOENCODING='utf-8')
    tried, started = [], time.monotonic()
    try:
        return _attempts(todo, folder, job_file, result_file, env, timeout, tried, started)
    finally:
        if job.get('password'):          # the password of the controller does not stay on the disk
            try:
                with open(job_file, 'w', encoding='utf-8') as f:
                    json.dump(dict(job, password='***'), f, ensure_ascii=False)
            except OSError:
                pass
        _prune(os.path.dirname(folder))


def _prune(jobs_dir: str, keep: int = 40) -> None:
    """Old job folders (the job, the result, the console of Machine Expert) are removed; the newest stay."""
    try:
        names = sorted(n for n in os.listdir(jobs_dir) if os.path.isdir(os.path.join(jobs_dir, n)))
    except OSError:
        return
    for n in names[:-keep]:
        folder = os.path.join(jobs_dir, n)
        try:
            for f in os.listdir(folder):
                os.remove(os.path.join(folder, f))
            os.rmdir(folder)
        except OSError:
            pass


def _attempts(todo, folder, job_file, result_file, env, timeout, tried, started) -> dict:
    with _Lock():
        for n, (title, cmd) in enumerate(todo):
            log = os.path.join(folder, f'log{n}.txt')
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            try:
                with open(log, 'wb') as out:
                    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                            cwd=folder, creationflags=flags)
                    end = time.monotonic() + timeout
                    while proc.poll() is None and time.monotonic() < end:
                        # the script writes the result and then asks the program to close; some versions stay open
                        if os.path.isfile(result_file):
                            try:
                                proc.wait(timeout=20)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                            break
                        time.sleep(0.25)
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=30)
                        if not os.path.isfile(result_file):
                            tried.append(f'{title}: не ответил за {int(timeout)} с')
                            continue
            except OSError as exc:
                tried.append(f'{title}: не запустился ({exc})')
                continue
            if os.path.isfile(result_file):
                try:
                    with open(result_file, encoding='utf-8') as f:
                        result = json.load(f)
                except ValueError as exc:
                    raise HostError(f'результат скрипта не читается: {exc}') from None
                result['how'] = title
                result['seconds'] = round(time.monotonic() - started, 1)
                result['job_dir'] = folder
                return result
            tried.append(f'{title}: закрылся с кодом {proc.returncode}, результата нет. ' + _tail(log, 600).strip())
    raise HostError('Machine Expert не выполнил скрипт. Пробовали:\n- ' + '\n- '.join(tried) +
                    '\nЕсли программа запускает скрипты иначе, задайте команду целиком: переменная SYSAI_ME_CMD, '
                    'например "{exe}" --runscript="{script}" --noUI. Журналы: ' + folder)


def open_in_program(path: str) -> str:
    """Open the project in Machine Expert for the engineer (the program stays open). -> the program started."""
    if not os.path.isfile(path):
        raise HostError(f'нет файла {path}')
    ui = [i for i in installs() if i['kind'] != 'shell']
    if os.name == 'nt' and not ui:
        os.startfile(path)          # noqa: S606 — the program registered for .project
        return 'программа для .project'
    if not ui:
        raise HostError('Machine Expert на этом компьютере не найден')
    subprocess.Popen([ui[0]['exe'], path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
    return ui[0]['exe']


if __name__ == '__main__':
    print(json.dumps({'installs': installs(), 'python': sys.version}, ensure_ascii=False, indent=2))
