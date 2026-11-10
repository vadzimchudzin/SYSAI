# -*- coding: utf-8 -*-
"""The script that runs INSIDE EcoStruxure Machine Expert (or CODESYS): one job, one result.

Machine Expert starts it with its own Python (IronPython 2.7) and gives it the scripting objects as globals:
`projects`, `system`, `online`, `device_repository` and the enumerations (`PouType`, `Severity`, `OnlineChangeOption`…).
The job is a JSON file named by the environment variable SYSAI_ME_JOB; the result is written next to it as
`result.json`. Nothing else is read or written except the files the job names.

The code must stay valid for Python 2.7 and Python 3 alike (the tests run it on a stand-in for Machine Expert).
Every call of the scripting interface that differs between versions of Machine Expert is tried in the documented
forms one after another; what did not work is told in `warnings`, never swallowed.
"""
from __future__ import print_function

import io
import json
import os
import re
import sys
import traceback

try:                                    # the names are globals inside Machine Expert; the import covers module use
    from scriptengine import *          # noqa: F401,F403
except Exception:                       # noqa: BLE001
    pass

WARNINGS = []
BUILD_CATEGORY = '{97F48D64-A2A3-4856-B640-75C046E37EA9}'


class JobError(Exception):
    pass


def g(name):
    """A scripting object by name, or None in a version of Machine Expert that has no such object."""
    return globals().get(name)


def warn(text):
    WARNINGS.append(text)


def text_of(value):
    try:
        return u'%s' % (value,)
    except Exception:                   # noqa: BLE001
        return repr(value)


def name_of(obj):
    try:
        return text_of(obj.get_name(False))
    except Exception:                   # noqa: BLE001
        try:
            return text_of(obj.get_name())
        except Exception:               # noqa: BLE001
            return text_of(getattr(obj, 'name', ''))


def children(obj, recursive=False):
    try:
        return list(obj.get_children(recursive))
    except Exception:                   # noqa: BLE001
        return []


def flag(obj, attr):
    try:
        return bool(getattr(obj, attr, False))
    except Exception:                   # noqa: BLE001
        return False


# ------------------------------------------------------------------------------------------------ the project
def close_all():
    p = g('projects')
    try:
        if p is not None and p.primary:
            p.primary.close()
    except Exception as exc:            # noqa: BLE001
        warn(u'открытый проект не закрылся: %s' % text_of(exc))


def open_project(path):
    if not os.path.isfile(path):
        raise JobError(u'нет файла проекта %s' % path)
    close_all()
    try:
        return g('projects').open(path)
    except Exception as exc:            # noqa: BLE001
        raise JobError(u'Machine Expert не открыл проект %s: %s' % (path, text_of(exc)))


def find_application(proj):
    try:
        app = proj.active_application
        if app is not None:
            return app
    except Exception:                   # noqa: BLE001
        pass
    for obj in children(proj, True):
        if flag(obj, 'is_application'):
            try:
                proj.active_application = obj
            except Exception:           # noqa: BLE001
                pass
            return obj
    raise JobError(u'в проекте нет приложения (Application): добавьте контроллер или возьмите шаблон проекта')


def find_device(proj, app=None):
    """The controller: the device the application sits in, else the first device of the project."""
    obj = app
    for _ in range(8):
        if obj is None:
            break
        if flag(obj, 'is_device'):
            return obj
        obj = getattr(obj, 'parent', None)
    for obj in children(proj, False):
        if flag(obj, 'is_device'):
            return obj
    return None


def version_key(version):
    return [int(x) for x in re.findall(r'\d+', text_of(version))]


def repository_devices(model):
    """Devices of the repository whose name holds `model`: [{'name', 'vendor', 'type', 'id', 'version'}]."""
    repo = g('device_repository')
    if repo is None:
        raise JobError(u'в этой версии Machine Expert скриптам не виден репозиторий устройств')
    found = None
    for call in (lambda: repo.get_all_devices(model), lambda: repo.get_all_devices(name=model),
                 lambda: repo.get_all_devices()):
        try:
            found = list(call())
            break
        except Exception:               # noqa: BLE001
            continue
    if found is None:
        raise JobError(u'репозиторий устройств не отвечает (get_all_devices)')
    out, low = [], text_of(model).lower()
    for d in found:
        try:
            info, did = d.device_info, d.device_id
            name = text_of(info.name)
            if low and low not in name.lower():
                continue
            out.append({'name': name, 'vendor': text_of(getattr(info, 'vendor', '')), 'type': int(did.type),
                        'id': text_of(did.id), 'version': text_of(did.version),
                        'order_number': text_of(getattr(info, 'order_number', '') or '')})
        except Exception:               # noqa: BLE001
            continue
    return out


def add_controller(proj, model, device_name):
    found = repository_devices(model)
    exact = [d for d in found if d['name'].lower() == model.lower() or d['order_number'].lower() == model.lower()]
    pool = exact or found
    # a controller is a device of type 4096 (PLC); others (modules, fieldbus) are not put at the top of a project
    plcs = [d for d in pool if d['type'] == 4096] or pool
    if not plcs:
        raise JobError(u'контроллера «%s» нет в репозитории устройств Machine Expert: проверьте модель (me_devices) '
                       u'или дайте шаблон проекта (template_file)' % model)
    plcs.sort(key=lambda d: version_key(d['version']))
    d = plcs[-1]
    try:
        proj.add(device_name, d['type'], d['id'], d['version'])
    except Exception as exc:            # noqa: BLE001
        raise JobError(u'контроллер %s %s не добавился в проект: %s' % (d['name'], d['version'], text_of(exc)))
    return d


# ------------------------------------------------------------------------------------------------ objects and text
def set_text(doc, text):
    for call in (lambda: doc.replace(new_text=text), lambda: doc.replace(text)):
        try:
            call()
            return
        except TypeError:
            continue
    raise JobError(u'текст объекта не записался (ScriptTextDocument.replace)')


def create_object(app, item):
    name, what, kind = item['name'], item['what'], item.get('kind')
    pou_type = g('PouType')
    if what == 'gvl':
        return app.create_gvl(name)
    if what == 'type':
        return app.create_dut(name)
    if kind == 'function':
        ret = item.get('returns') or 'BOOL'
        for call in (lambda: app.create_pou(name, pou_type.Function, None, ret), lambda: app.create_function(name, ret)):
            try:
                return call()
            except (TypeError, AttributeError):
                continue
    elif kind == 'function_block':
        for call in (lambda: app.create_pou(name, pou_type.FunctionBlock), lambda: app.create_function_block(name)):
            try:
                return call()
            except (TypeError, AttributeError):
                continue
    else:
        for call in (lambda: app.create_pou(name, pou_type.Program), lambda: app.create_program(name)):
            try:
                return call()
            except (TypeError, AttributeError):
                continue
    raise JobError(u'объект %s не создался: в этой версии Machine Expert другой вызов создания POU' % name)


def apply_objects(app, objects):
    """Write the objects of the ST folder into the application: new ones are created, those already there get the
    new text. -> {'created': [...], 'updated': [...], 'same': [...], 'extra': names in the application that the
    folder does not have}."""
    have = {}
    for obj in children(app, True):
        if flag(obj, 'has_textual_declaration') and not flag(obj, 'is_task'):
            have.setdefault(name_of(obj).lower(), obj)
    created, updated, same = [], [], []
    for item in objects:
        low = item['name'].lower()
        obj = have.get(low)
        fresh = obj is None
        if fresh:
            try:
                obj = create_object(app, item)
            except JobError:
                raise
            except Exception as exc:    # noqa: BLE001
                raise JobError(u'объект %s не создался: %s' % (item['name'], text_of(exc)))
        changed = fresh
        decl = item['declaration'].replace('\r\n', '\n')
        try:
            if text_of(obj.textual_declaration.text).replace('\r\n', '\n').strip() != decl.strip():
                set_text(obj.textual_declaration, decl)
                changed = True
        except AttributeError:
            raise JobError(u'у объекта %s нет текстового объявления (графический язык?)' % item['name'])
        if item['what'] == 'pou':
            body = (item.get('body') or '').replace('\r\n', '\n')
            if not flag(obj, 'has_textual_implementation'):
                raise JobError(u'объект %s в проекте написан не на ST: текст в него не записать' % item['name'])
            if text_of(obj.textual_implementation.text).replace('\r\n', '\n').strip() != body.strip():
                set_text(obj.textual_implementation, body)
                changed = True
        (created if fresh else updated if changed else same).append(item['name'])
        have[low] = obj
    wanted = set(i['name'].lower() for i in objects)
    extra = sorted(name_of(o) for k, o in have.items() if k not in wanted and getattr(o, 'parent', None) is not None
                   and not flag(getattr(o, 'parent', None), 'has_textual_declaration'))
    return {'created': created, 'updated': updated, 'same': same, 'extra': extra}


def split_interval(value):
    m = re.search(r'(\d+(?:\.\d+)?)\s*(us|ms|s)?', text_of(value).lower().replace('t#', ''))
    if not m:
        return '20', 'ms'
    return m.group(1), m.group(2) or 'ms'


def apply_tasks(app, tasks):
    if not tasks:
        return []
    config = None
    for obj in children(app, True):
        if flag(obj, 'is_task_configuration'):
            config = obj
            break
    if config is None:
        try:
            config = app.create_task_configuration()
        except Exception as exc:        # noqa: BLE001
            warn(u'конфигурация задач не создалась: %s — задайте задачи в Machine Expert' % text_of(exc))
            return []
    have = dict((name_of(t).lower(), t) for t in children(config, False) if flag(t, 'is_task'))
    done = []
    for t in tasks:
        name = t['name']
        task = have.get(name.lower())
        try:
            if task is None:
                task = config.create_task(name)
            kind = g('KindOfTask')
            if kind is not None:
                task.kind_of_task = kind.Cyclic
            number, unit = split_interval(t.get('interval'))
            task.interval = number
            task.interval_unit = unit
            if t.get('priority') not in (None, ''):
                task.priority = text_of(t['priority'])
            current = []
            for x in list(task.pous):
                current.append(text_of(getattr(x, 'name', x)))
            wanted = [text_of(p) for p in t.get('pous') or []]
            if [c.lower() for c in current] != [w.lower() for w in wanted]:
                for c in current:
                    task.pous.remove(c)
                for w in wanted:
                    task.pous.add(w)
            done.append(name)
        except Exception as exc:        # noqa: BLE001
            warn(u'задача %s не настроилась: %s — проверьте «Конфигурацию задач»' % (name, text_of(exc)))
    return done


def ensure_symbols(app):
    """The symbol configuration with OPC UA support: what the OPC UA server of the controller publishes."""
    for obj in children(app, True):
        if flag(obj, 'is_symbol_config'):
            return 'есть'
    for call in (lambda: app.create_symbol_config(True, True, None), lambda: app.create_symbol_config(True, True)):
        try:
            call()
            return 'создана'
        except Exception as exc:        # noqa: BLE001
            last = exc
    warn(u'«Конфигурация символов» не создалась (%s): добавьте её в Machine Expert к Application и включите OPC UA'
         % text_of(last))
    return ''


# ------------------------------------------------------------------------------------------------ build and messages
def severity_name(sev):
    s = text_of(sev).lower()
    for key, name in (('fatal', 'error'), ('error', 'error'), ('warn', 'warning'), ('info', 'info'), ('text', 'info')):
        if key in s:
            return name
    return {1: 'error', 2: 'error', 4: 'warning', 8: 'info', 16: 'info'}.get(sev if isinstance(sev, int) else -1, 'info')


def line_of(position_text):
    m = re.search(r'(\d+)', position_text or '')
    return int(m.group(1)) if m else None


def part_of(position_text):
    low = (position_text or '').lower()
    return 'declaration' if ('decl' in low or u'объяв' in low) else 'body' if ('impl' in low or u'реализ' in low) else ''


def clear_messages():
    system = g('system')
    try:
        for cat in list(system.get_message_categories(True)):
            system.clear_messages(cat)
    except Exception:                   # noqa: BLE001
        pass


def collect_messages():
    system = g('system')
    out, seen = [], set()
    try:
        cats = list(system.get_message_categories(True))
    except Exception:                   # noqa: BLE001
        cats = [BUILD_CATEGORY]
    for cat in cats:
        try:
            msgs = list(system.get_message_objects(cat))
        except Exception:               # noqa: BLE001
            continue
        for m in msgs:
            level = severity_name(getattr(m, 'severity', ''))
            obj = getattr(m, 'object', None)
            pos = text_of(getattr(m, 'position_text', '') or '')
            row = {'level': level, 'text': text_of(getattr(m, 'text', '')), 'object': name_of(obj) if obj is not None else '',
                   'where': pos, 'line': line_of(pos), 'part': part_of(pos),
                   'code': (text_of(getattr(m, 'prefix', '') or '') + text_of(getattr(m, 'number', '') or '')).strip()}
            key = (row['level'], row['text'], row['object'], row['where'])
            if key not in seen:
                seen.add(key)
                out.append(row)
    return out


def build(app, code=False):
    clear_messages()
    try:
        if code:
            app.generate_code()
        else:
            app.build()
    except Exception as exc:            # noqa: BLE001
        warn(u'компиляция прервалась: %s' % text_of(exc))
    msgs = collect_messages()
    errors = [m for m in msgs if m['level'] == 'error']
    warnings = [m for m in msgs if m['level'] == 'warning']
    return {'ok': not errors, 'errors': errors[:300], 'warnings': warnings[:300],
            'counts': {'error': len(errors), 'warning': len(warnings)}}


# ------------------------------------------------------------------------------------------------ jobs
def job_devices(job):
    rows = repository_devices(job.get('filter') or '')
    rows.sort(key=lambda d: (d['name'].lower(), version_key(d['version'])))
    return {'count': len(rows), 'devices': rows[:400]}


def job_apply(job):
    """`create` (a new project from a template or from a controller model) and `update` (an existing project):
    write the ST objects, the tasks and the symbol configuration, save, build."""
    out_file = job['file']
    created_from = ''
    if job['action'] == 'create':
        if os.path.exists(out_file) and not job.get('overwrite'):
            raise JobError(u'файл %s уже есть: назовите другой или overwrite' % out_file)
        folder = os.path.dirname(out_file)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        close_all()
        if job.get('template_file'):
            proj = open_project(job['template_file'])
            if os.path.exists(out_file):
                os.remove(out_file)
            proj.save_as(out_file)
            created_from = u'шаблон %s' % job['template_file']
        else:
            if os.path.exists(out_file):
                os.remove(out_file)
            proj = g('projects').create(out_file, True)
            d = add_controller(proj, job.get('controller') or '', job.get('device_name') or 'MyController')
            created_from = u'контроллер %s, версия %s' % (d['name'], d['version'])
    else:
        proj = open_project(out_file)
    app = find_application(proj)
    result = {'file': out_file, 'from': created_from, 'application': name_of(app)}
    dev = find_device(proj, app)
    result['controller'] = name_of(dev) if dev is not None else ''
    result['objects'] = apply_objects(app, job.get('objects') or [])
    result['tasks'] = apply_tasks(app, job.get('tasks') or [])
    if (job.get('link') or {}).get('kind') == 'opcua':
        result['symbols'] = ensure_symbols(app)
    proj.save()
    result['build'] = build(app, bool(job.get('code')))
    return result


def job_build(job):
    proj = open_project(job['file'])
    app = find_application(proj)
    return {'file': job['file'], 'application': name_of(app), 'build': build(app, bool(job.get('code')))}


def task_rows(app):
    rows = []
    for obj in children(app, True):
        if not flag(obj, 'is_task'):
            continue
        row = {'name': name_of(obj)}
        for key in ('interval', 'interval_unit', 'priority'):
            try:
                row[key] = text_of(getattr(obj, key))
            except Exception:           # noqa: BLE001
                row[key] = ''
        try:
            row['pous'] = [text_of(getattr(x, 'name', x)) for x in list(obj.pous)]
        except Exception:               # noqa: BLE001
            row['pous'] = []
        rows.append(row)
    return rows


def job_export(job):
    proj = open_project(job['file'])
    app = find_application(proj)
    dev = find_device(proj, app)
    result = {'file': job['file'], 'application': name_of(app), 'controller': name_of(dev) if dev is not None else ''}
    if job.get('format') == 'plcopen':
        out = job['out_file']
        folder = os.path.dirname(out)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        done = False
        for call in (lambda: proj.export_xml([app], None, out, True, True, True),
                     lambda: app.export_xml(None, out, True, True, True),
                     lambda: app.export_xml(path=out, recursive=True)):
            try:
                call()
                done = True
                break
            except Exception as exc:    # noqa: BLE001
                last = exc
        if not done:
            raise JobError(u'экспорт PLCopen XML не удался: %s' % text_of(last))
        result['out_file'] = out
        return result
    # the folder of ST files, read straight from the objects
    out_dir = job['out_dir']
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    written, skipped = [], []
    for obj in children(app, True):
        if not flag(obj, 'has_textual_declaration') or flag(obj, 'is_task'):
            continue
        parent = getattr(obj, 'parent', None)
        if parent is not None and flag(parent, 'has_textual_declaration'):
            skipped.append(u'%s.%s (метод, действие или свойство)' % (name_of(parent), name_of(obj)))
            continue
        name = name_of(obj)
        decl = text_of(obj.textual_declaration.text).replace('\r\n', '\n').rstrip('\n')
        text = decl
        first = re.search(r'\b(PROGRAM|FUNCTION_BLOCK|FUNCTION)\b', decl, re.I)
        if first:
            if flag(obj, 'has_textual_implementation'):
                text += '\n\n' + text_of(obj.textual_implementation.text).replace('\r\n', '\n').rstrip('\n')
            else:
                text += u'\n\n// Графический язык (LD / FBD / CFC): кода текстом нет.'
                skipped.append(u'%s (графический язык: записано только объявление)' % name)
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
            skipped.append(u'%s (имя не годится для файла)' % name)
            continue
        with io.open(os.path.join(out_dir, name + '.st'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(text + u'\n')
        written.append(name + '.st')
    tasks = []
    for t in task_rows(app):
        tasks.append({'name': t['name'], 'interval': (t['interval'] + t['interval_unit']) if t['interval'] else '20ms',
                      'priority': t['priority'] or '15', 'pous': t['pous']})
    meta = {'name': os.path.splitext(os.path.basename(job['file']))[0], 'controller': result['controller'],
            'source': 'machine-expert-project', 'tasks': tasks}
    with io.open(os.path.join(out_dir, 'project.json'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(text_of(json.dumps(meta, ensure_ascii=False, indent=2)) + u'\n')
    result.update({'out_dir': out_dir, 'files': written, 'skipped': skipped, 'tasks': tasks})
    return result


def gateway(name):
    online = g('online')
    try:
        gws = list(online.gateways)
    except Exception as exc:            # noqa: BLE001
        raise JobError(u'шлюзы связи не видны скрипту: %s' % text_of(exc))
    if name:
        gws = [x for x in gws if text_of(x.name).lower() == name.lower()]
    if not gws:
        raise JobError(u'нет шлюза связи%s: в Machine Expert должен быть шлюз (Gateway)' % (u' «%s»' % name if name else u''))
    return gws[0]


def job_scan(job):
    gw = gateway(job.get('gateway') or '')
    try:
        targets = list(gw.perform_network_scan())
    except Exception as exc:            # noqa: BLE001
        raise JobError(u'поиск контроллеров в сети не удался: %s' % text_of(exc))
    rows = []
    for t in targets:
        rows.append({'name': text_of(getattr(t, 'device_name', '')), 'type': text_of(getattr(t, 'type_name', '')),
                     'vendor': text_of(getattr(t, 'vendor_name', '')), 'address': text_of(getattr(t, 'address', ''))})
    return {'gateway': text_of(gw.name), 'count': len(rows), 'controllers': rows}


def set_target(dev, job):
    """Point the controller of the project at the real one: by the address, the node name or the IP address."""
    address, node, ip = job.get('address') or '', job.get('node') or '', job.get('ip') or ''
    if not (address or node or ip):
        return u'как записано в проекте'
    tried = []
    if (ip or node) and hasattr(dev, 'set_communication_address'):         # Machine Expert's own call
        try:
            dev.set_communication_address(ip or node)
            return u'set_communication_address(%s)' % (ip or node)
        except Exception as exc:        # noqa: BLE001
            tried.append(u'set_communication_address: %s' % text_of(exc))
    gw = gateway(job.get('gateway') or '')
    if address:
        try:
            dev.set_gateway_and_address(gw, address)
            return u'шлюз %s, адрес %s' % (text_of(gw.name), address)
        except Exception as exc:        # noqa: BLE001
            tried.append(u'set_gateway_and_address: %s' % text_of(exc))
    if node:
        try:
            dev.set_gateway_and_device_name(text_of(gw.name), node)
            return u'шлюз %s, имя узла %s' % (text_of(gw.name), node)
        except Exception as exc:        # noqa: BLE001
            tried.append(u'set_gateway_and_device_name: %s' % text_of(exc))
    if ip:
        try:
            value = ip
            try:
                from System.Net import IPAddress      # noqa: PLC0415 — only inside Machine Expert (.NET)
                value = IPAddress.Parse(ip)
            except ImportError:
                pass
            dev.set_gateway_and_ip_address(text_of(gw.name), value)
            return u'шлюз %s, IP %s' % (text_of(gw.name), ip)
        except Exception as exc:        # noqa: BLE001
            tried.append(u'set_gateway_and_ip_address: %s' % text_of(exc))
    raise JobError(u'адрес контроллера не задался: ' + u'; '.join(tried))


def job_online(job):
    proj = open_project(job['file'])
    app = find_application(proj)
    dev = find_device(proj, app)
    if dev is None:
        raise JobError(u'в проекте нет контроллера')
    result = {'file': job['file'], 'application': name_of(app), 'controller': name_of(dev)}
    simulation = bool(job.get('simulation'))
    try:
        dev.set_simulation_mode(simulation)
    except Exception as exc:            # noqa: BLE001
        if simulation:
            raise JobError(u'режим симуляции не включился: %s' % text_of(exc))
    result['target'] = u'симуляция' if simulation else set_target(dev, job)
    download = bool(job.get('download')) or simulation
    if download:
        b = build(app)
        result['build'] = b
        if not b['ok']:
            result['logged_in'] = False
            result['note'] = u'проект не компилируется: в контроллер не загружен'
            return result
    online = g('online')
    if job.get('user'):
        try:
            online.set_default_credentials(job['user'], job.get('password') or '')
        except Exception as exc:        # noqa: BLE001
            warn(u'учётные данные не заданы: %s' % text_of(exc))
    option = g('OnlineChangeOption')
    oa = online.create_online_application(app)
    try:
        try:
            oa.login(option.Try if download else option.Keep, False)
        except Exception as exc:        # noqa: BLE001
            raise JobError(u'вход в контроллер не удался (%s): %s' % (result['target'], text_of(exc)))
        result['logged_in'] = True
        result['downloaded'] = download
        if job.get('start'):
            try:
                oa.start()
            except Exception as exc:    # noqa: BLE001
                warn(u'приложение не запустилось: %s' % text_of(exc))
        if job.get('stop'):
            try:
                oa.stop()
            except Exception as exc:    # noqa: BLE001
                warn(u'приложение не остановилось: %s' % text_of(exc))
        written = {}
        for expr, value in sorted((job.get('write') or {}).items()):
            try:
                oa.set_prepared_value(expr, text_of(value))
                written[expr] = {'ok': True, 'value': text_of(value)}
            except Exception as exc:    # noqa: BLE001
                written[expr] = {'ok': False, 'error': text_of(exc)}
        if written:
            try:
                oa.write_prepared_values()
            except Exception as exc:    # noqa: BLE001
                for expr in written:
                    if written[expr]['ok']:
                        written[expr] = {'ok': False, 'error': text_of(exc)}
            result['written'] = written
        if job.get('read'):
            try:
                g('system').delay(int(job.get('delay_ms') or 300))
            except Exception:           # noqa: BLE001
                pass
            values = {}
            for expr in job['read']:
                try:
                    values[expr] = {'ok': True, 'value': text_of(oa.read_value(expr))}
                except Exception as exc:    # noqa: BLE001
                    values[expr] = {'ok': False, 'error': text_of(exc)}
            result['values'] = values
        try:
            result['state'] = text_of(oa.application_state)
        except Exception:               # noqa: BLE001
            result['state'] = ''
    finally:
        try:
            oa.logout()
        except Exception:               # noqa: BLE001
            pass
        if simulation:
            try:
                dev.set_simulation_mode(False)
            except Exception:           # noqa: BLE001
                pass
    return result


JOBS = {'devices': job_devices, 'create': job_apply, 'update': job_apply, 'build': job_build, 'export': job_export,
        'scan': job_scan, 'online': job_online}


def main():
    job_file = os.environ.get('SYSAI_ME_JOB') or ''
    if not job_file or not os.path.isfile(job_file):
        print('SYSAI_ME_JOB is not set: this script is started by the tool machine-expert')
        return 2
    result_file = os.path.join(os.path.dirname(job_file), 'result.json')
    out = {'ok': False}
    try:
        with io.open(job_file, encoding='utf-8') as f:
            job = json.load(f)
        system = g('system')
        if system is None or g('projects') is None:
            raise JobError(u'скрипт запущен не в Machine Expert: нет объектов projects и system')
        handling = g('PromptHandling')
        if handling is not None:
            try:
                system.prompt_handling = getattr(handling, 'None')      # answer the prompts by default, never wait
            except Exception:           # noqa: BLE001
                pass
        action = job.get('action')
        if action not in JOBS:
            raise JobError(u'нет действия %s' % text_of(action))
        out = {'ok': True, 'action': action, 'result': JOBS[action](job)}
        try:
            out['version'] = text_of(getattr(system, 'executable_filename', '') or '')
        except Exception:               # noqa: BLE001
            pass
    except JobError as exc:
        out = {'ok': False, 'error': text_of(exc)}
    except Exception as exc:            # noqa: BLE001 — everything goes into the result: nobody watches the console
        out = {'ok': False, 'error': u'%s: %s' % (type(exc).__name__, text_of(exc)), 'trace': traceback.format_exc()[-3000:]}
    out['warnings'] = WARNINGS
    close_all()
    tmp = result_file + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8') as f:
        f.write(text_of(json.dumps(out, ensure_ascii=False)))
    if os.path.exists(result_file):
        os.remove(result_file)
    os.rename(tmp, result_file)
    return 0 if out.get('ok') else 1


if __name__ == '__main__' or g('projects') is not None:
    CODE = main()
    try:
        g('system').exit(CODE)          # close Machine Expert started for this job
    except AttributeError:
        pass
