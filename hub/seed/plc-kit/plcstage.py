"""The course of a project «с нуля под ключ»: `pipeline.json` in the project folder.

The agent marks where the work is (the tool `plc_stage_update`); the window «Агент SYSAI» shows it as a strip of
stages over the project, and the next agent (or the same one tomorrow) reads it instead of guessing. The stages go in
this order; a stage is `todo`, `work`, `done` or `problem` (something the engineer must settle — the note says what).
"""
import datetime
import json
import os

FILE = 'pipeline.json'
STAGES = [
    ('materials', 'Материалы', 'Прочитаны ТЗ и схемы; перечень механизмов и сигналов в project.md'),
    ('decisions', 'Решения', 'Контроллер, панель, связь (OPC UA / Modbus), стиль'),
    ('plc', 'Программа ПЛК', 'Файлы ST в plc/, проверка plc_check без ошибок'),
    ('build', 'Компиляция', 'Проект Machine Expert собран и скомпилирован без ошибок'),
    ('hmi', 'Экраны', 'Проект панели в hmi/, теги привязаны, cmtp_check без ошибок'),
    ('docs', 'Документы', 'Руководство пользователя и паспорт в docs/'),
]
STATES = ('todo', 'work', 'done', 'problem')
FACTS = ('controller', 'panel', 'resolution', 'link', 'style', 'machine_expert', 'project_file', 'hmi_file', 'doc_file')


class StageError(ValueError):
    pass


def read(project_dir: str) -> dict:
    """The pipeline of the project (an empty one when the file is not there or is broken)."""
    data = {}
    try:
        with open(os.path.join(project_dir, FILE), encoding='utf-8-sig') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    stages = data.get('stages') if isinstance(data.get('stages'), dict) else {}
    out = {'version': 1, 'stages': {}, 'facts': {}}
    for key, _, _ in STAGES:
        s = stages.get(key) if isinstance(stages.get(key), dict) else {}
        state = s.get('state') if s.get('state') in STATES else 'todo'
        out['stages'][key] = {'state': state, 'note': str(s.get('note') or '')[:500], 'updated': str(s.get('updated') or '')}
    facts = data.get('facts') if isinstance(data.get('facts'), dict) else {}
    out['facts'] = {k: facts[k] for k in FACTS if facts.get(k) not in (None, '')}
    return out


def update(project_dir: str, stage: str = '', state: str = '', note=None, facts=None) -> dict:
    """Mark a stage and / or write down facts (the controller, the panel, the link…); the whole pipeline back."""
    if not os.path.isdir(project_dir):
        raise StageError(f'нет папки проекта {project_dir}')
    data = read(project_dir)
    now = datetime.datetime.now().replace(microsecond=0).isoformat()
    if stage:
        if stage not in data['stages']:
            raise StageError(f'нет этапа «{stage}»; этапы: ' + ', '.join(k for k, _, _ in STAGES))
        if state not in STATES:
            raise StageError('state: todo (не начат), work (идёт), done (готов), problem (нужен инженер)')
        order = [k for k, _, _ in STAGES]
        if state == 'done':
            late = [k for k in order[:order.index(stage)] if data['stages'][k]['state'] not in ('done',)]
            # the documentation may be made without the build: the engineer may compile later
            late = [k for k in late if not (stage in ('hmi', 'docs') and k == 'build')]
            if late:
                raise StageError('этап нельзя закрыть раньше предыдущих: не готовы ' + ', '.join(late))
        data['stages'][stage] = {'state': state, 'note': str(data['stages'][stage]['note'] if note is None else note)[:500],
                                 'updated': now}
    elif state or note is not None:
        raise StageError('назовите этап (stage)')
    if isinstance(facts, dict):
        for k, v in facts.items():
            if k not in FACTS:
                raise StageError(f'нет такого поля «{k}»; поля: ' + ', '.join(FACTS))
            if v in (None, ''):
                data['facts'].pop(k, None)
            else:
                data['facts'][k] = v if isinstance(v, (str, int, float, list)) else str(v)
    tmp = os.path.join(project_dir, FILE + '.tmp')
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, os.path.join(project_dir, FILE))
    return view(data)


# What a folder of results holds; everything else belongs to _work/ (drafts the window shows folded).
RESULTS = {'plc': ('.project', '.projectarchive'), 'hmi': ('.cmtp', '.emtp', '.cxob', '.exob'), 'docs': ('.docx', '.pdf')}
# What Machine Expert and EasyBuilder keep next to a project themselves (the lock of an open project, the options of
# the user, the caches of a compilation, backups): not a result and not a draft — it stays where the program put it.
SERVICE = ('.~u', '.opt', '.precompilecache', '.bootinfo', '.bootinfo_guids', '.compileinfo', '.backup', '.bak')


def extra(project_dir: str, limit: int = 200) -> list:
    """Files in plc/, hmi/, docs/ that are not a result: sources, scripts, tables, notes — ['plc/Main.st', …]."""
    out = []
    for part, exts in RESULTS.items():
        root = os.path.join(project_dir, part)
        for base, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
            for name in sorted(files):
                if name.startswith(('.', '~$')) or name.lower().endswith(exts + SERVICE):
                    continue
                out.append(os.path.relpath(os.path.join(base, name), project_dir).replace(os.sep, '/'))
                if len(out) >= limit:
                    return out
    return out


def view(data: dict) -> dict:
    """The pipeline with the titles of the stages and the stage to do next."""
    rows = [{'stage': k, 'title': title, 'goal': goal, **data['stages'][k]} for k, title, goal in STAGES]
    nxt = next((r['stage'] for r in rows if r['state'] != 'done'), '')
    return {'stages': rows, 'facts': data['facts'], 'next': nxt}
