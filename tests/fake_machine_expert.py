"""A stand-in for EcoStruxure Machine Expert in tests: runs a script the way the program does — with the scripting
objects (`projects`, `system`, `online`, `device_repository` and the enumerations) as its globals.

It follows the documented scripting interface of CODESYS V3.5 (the engine of Machine Expert) only as far as the tool
machine-expert uses it; a project is kept as a JSON file at the path of the `.project`. The «compiler» knows two
rules: a line with `@@` is an error at that line, a line with `TODO` is a warning. It proves the logic of the job
script, not Machine Expert itself.

    python fake_machine_expert.py <script.py>
"""
import json
import os
import re
import sys

BUILD = "{97F48D64-A2A3-4856-B640-75C046E37EA9}"
KNOWN = {"address": "0301.A0C3", "node": "M241-Line1", "ip": "192.168.1.10"}


class Enum:
    def __init__(self, **items):
        for k, v in items.items():
            setattr(self, k, v)


Severity = Enum(FatalError=1, Error=2, Warning=4, Information=8, Text=16)
PouType = Enum(Program="program", FunctionBlock="function_block", Function="function")
KindOfTask = Enum(Cyclic="Cyclic", Freewheeling="Freewheeling")
OnlineChangeOption = Enum(Never=0, Try=1, Force=2, Keep=3)
PromptHandling = Enum()
setattr(PromptHandling, "None", 0)


class Doc:
    def __init__(self, obj, key):
        self.obj, self.key = obj, key

    @property
    def text(self):
        return self.obj.data.get(self.key, "")

    def replace(self, new_text=None, **kw):
        if kw or new_text is None:
            raise TypeError("only replace(new_text) is modelled")
        self.obj.data[self.key] = new_text
        self.obj.project.dirty = True


class Pous(list):
    def add(self, name, comment=None):
        self.append(name)

    def remove(self, index_or_name):
        if isinstance(index_or_name, int):
            del self[index_or_name]
        else:
            list.remove(self, index_or_name)


class Obj:
    def __init__(self, project, kind, name, parent=None, data=None):
        self.project, self.kind, self.name, self.parent = project, kind, name, parent
        self.data = data or {}
        self.kids = []
        if parent is not None:
            parent.kids.append(self)
        if kind == "task":
            self.pous = Pous(self.data.get("pous", []))
            self.kind_of_task = self.data.get("kind_of_task", "Cyclic")
            self.interval = self.data.get("interval", "")
            self.interval_unit = self.data.get("interval_unit", "ms")
            self.priority = self.data.get("priority", "")

    is_device = property(lambda s: s.kind == "device")
    is_application = property(lambda s: s.kind == "application")
    is_task_configuration = property(lambda s: s.kind == "taskconfig")
    is_task = property(lambda s: s.kind == "task")
    is_symbol_config = property(lambda s: s.kind == "symbolconfig")
    has_textual_declaration = property(lambda s: s.kind in ("pou", "gvl", "dut"))
    has_textual_implementation = property(lambda s: s.kind == "pou" and not s.data.get("graphic"))
    textual_declaration = property(lambda s: Doc(s, "decl"))
    textual_implementation = property(lambda s: Doc(s, "impl"))

    def get_name(self, resolve_localized_display_name=False):
        return self.name

    def get_children(self, recursive=False):
        out = []
        for k in self.kids:
            out.append(k)
            if recursive:
                out += k.get_children(True)
        return out

    def _new(self, kind, name, data):
        if any(k.name.lower() == name.lower() for k in self.get_children(True)):
            raise Exception("An object named '%s' already exists" % name)
        if kind in ("pou", "gvl", "dut") and not re.match(r"^[A-Za-z_]\w*$", name):
            raise Exception("'%s' is not an IEC identifier" % name)
        self.project.dirty = True
        return Obj(self.project, kind, name, self, data)

    def create_pou(self, name, type=None, language=None, return_type=None):
        word = {"program": "PROGRAM", "function_block": "FUNCTION_BLOCK", "function": "FUNCTION"}[type]
        return self._new("pou", name, {"decl": "%s %s\nVAR\nEND_VAR" % (word, name), "impl": ""})

    def create_gvl(self, name):
        return self._new("gvl", name, {"decl": "VAR_GLOBAL\nEND_VAR"})

    def create_dut(self, name, type=None, baseType=None):
        return self._new("dut", name, {"decl": "TYPE %s :\nSTRUCT\nEND_STRUCT\nEND_TYPE" % name})

    def create_task_configuration(self):
        return self._new("taskconfig", "Task Configuration", {})

    def create_task(self, name):
        return Obj(self.project, "task", name, self, {})

    def create_symbol_config(self, export_comments_to_xml, support_opc_ua, client_side_layout_calculator):
        return self._new("symbolconfig", "Symbol Configuration", {"opcua": bool(support_opc_ua)})

    # the application
    def _compile(self):
        system.store[BUILD] = []
        for o in self.get_children(True):
            for key, part in (("decl", "Decl"), ("impl", "Impl")):
                for n, line in enumerate((o.data.get(key) or "").split("\n"), 1):
                    if "@@" in line:
                        system.store[BUILD].append(Message(2, "C0046", "Identifier '%s' not defined" % line.strip(" \t;"),
                                                           o, "Line %d, Column 1 (%s)" % (n, part)))
                    elif "TODO" in line:
                        system.store[BUILD].append(Message(4, "C0196", "Implicit conversion", o, "Line %d (%s)" % (n, part)))

    build = generate_code = _compile

    def export_xml(self, reporter=None, path=None, recursive=False, export_folder_structure=False, declarations_as_plaintext=False):
        names = [o.name for o in self.get_children(True) if o.has_textual_declaration]
        with open(path, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0"?><project xmlns="http://www.plcopen.org/xml/tc6_0200"><!-- %s --></project>' % ",".join(names))

    # the device
    def set_simulation_mode(self, simulation):
        self.data["simulation"] = bool(simulation)

    def set_gateway_and_address(self, gateway, address):
        self.data["target"] = ("address", address)

    def set_gateway_and_device_name(self, gateway, device_name):
        self.data["target"] = ("node", device_name)

    def set_gateway_and_ip_address(self, gateway, ip_address, port=11740):
        self.data["target"] = ("ip", str(ip_address))

    def dump(self):
        d = dict(self.data)
        if self.kind == "task":
            d.update(pous=list(self.pous), kind_of_task=self.kind_of_task, interval=self.interval,
                     interval_unit=self.interval_unit, priority=self.priority)
        d.pop("simulation", None)
        return {"kind": self.kind, "name": self.name, "data": d, "kids": [k.dump() for k in self.kids]}


class Message:
    def __init__(self, severity, code, text, obj, position_text):
        self.severity, self.prefix, self.number, self.text, self.object, self.position_text = \
            severity, code[:1], code[1:], text, obj, position_text


class Project:
    def __init__(self, path):
        self.path, self.kids, self.dirty, self._active = path, [], False, None

    def get_children(self, recursive=False):
        out = []
        for k in self.kids:
            out.append(k)
            if recursive:
                out += k.get_children(True)
        return out

    def add(self, name, type, id, version, module=None):
        dev = Obj(self, "device", name, None, {"id": id, "version": version})
        self.kids.append(dev)
        logic = Obj(self, "plclogic", "Plc Logic", dev)
        Obj(self, "application", "Application", logic)
        self.dirty = True

    @property
    def active_application(self):
        return self._active or next((o for o in self.get_children(True) if o.is_application), None)

    @active_application.setter
    def active_application(self, obj):
        self._active = obj

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"fake-machine-expert": 1, "kids": [k.dump() for k in self.kids]}, f, ensure_ascii=False)
        self.dirty = False

    def save_as(self, path, password=None):
        self.path = path
        self.save()

    def close(self):
        if projects.primary is self:
            projects.primary = None

    @staticmethod
    def load(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if "fake-machine-expert" not in data:
            raise Exception("The file is not a project")
        p = Project(path)

        def grow(d, parent):
            o = Obj(p, d["kind"], d["name"], parent, d["data"])
            if parent is None:
                p.kids.append(o)
            for k in d["kids"]:
                grow(k, o)
        for d in data["kids"]:
            grow(d, None)
        return p


class Projects:
    primary = None

    def create(self, path, primary=True):
        self.primary = Project(path)
        self.primary.save()
        return self.primary

    def open(self, path, password=None, primary=True):
        self.primary = Project.load(path)
        return self.primary


class System:
    store = {}
    executable_filename = "fake Machine Expert"
    prompt_handling = None

    def get_message_categories(self, bActive=True):
        return list(self.store)

    def get_message_objects(self, category=None, severities=0xFFFFFFFF):
        return list(self.store.get(category, []))

    def clear_messages(self, category):
        self.store[category] = []

    def delay(self, ms):
        pass

    def exit(self, code=0):
        raise SystemExit(code)


class Info:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Repository:
    devices = [("TM241CE40R", "1.2.3.4"), ("TM241CE40R", "5.0.8.2"), ("TM241CE24T/U", "5.0.8.2"), ("TM251MESE", "5.0.4.1")]

    def get_all_devices(self, name=None, source=None):
        return [Info(device_info=Info(name=n, vendor="Schneider Electric", order_number=n),
                     device_id=Info(type=4096, id="101a 0711", version=v))
                for n, v in self.devices if not name or name.lower() in n.lower()]


class Gateway:
    name = "Gateway-1"

    def perform_network_scan(self):
        return [Info(device_name=KNOWN["node"], type_name="TM241CE40R", vendor_name="Schneider Electric", address=KNOWN["address"])]


class OnlineApplication:
    def __init__(self, app):
        self.app, self.logged, self.application_state, self.prepared, self.values = app, False, "stop", {}, {}

    def _device(self):
        o = self.app
        while o is not None and not o.is_device:
            o = o.parent
        return o

    def login(self, change_option, delete_foreign_apps):
        dev = self._device()
        target = dev.data.get("target")
        if not dev.data.get("simulation") and (not target or KNOWN.get(target[0]) != target[1]):
            raise Exception("No device is responding to the scan request")
        self.logged = True
        self.values = {}
        for o in self.app.get_children(True):
            if o.kind == "gvl":
                for m in re.finditer(r"^\s*(\w+)\s*(?:AT\s+\S+\s*)?:\s*(\w+)(?:\s*:=\s*([^;]+))?;", o.data.get("decl", ""), re.M):
                    self.values["%s.%s" % (o.name, m.group(1))] = (m.group(3) or ("FALSE" if m.group(2) == "BOOL" else "0")).strip()

    def logout(self):
        self.logged = False

    def start(self):
        self.application_state = "run"

    def stop(self):
        self.application_state = "stop"

    def read_value(self, expression):
        if expression not in self.values:
            raise Exception("'%s' is not a valid expression" % expression)
        return self.values[expression]

    def set_prepared_value(self, expression, value):
        if expression not in self.values:
            raise Exception("'%s' is not a valid expression" % expression)
        self.prepared[expression] = value

    def write_prepared_values(self):
        self.values.update(self.prepared)
        self.prepared = {}


class Online:
    gateways = [Gateway()]

    def create_online_application(self, application=None):
        return OnlineApplication(application)

    def set_default_credentials(self, username, password=None):
        pass


projects, system, online, device_repository = Projects(), System(), Online(), Repository()

if __name__ == "__main__":
    script = sys.argv[1]
    scope = {"__name__": "__main__", "__file__": script, "projects": projects, "system": system, "online": online,
             "device_repository": device_repository, "Severity": Severity, "PouType": PouType, "KindOfTask": KindOfTask,
             "OnlineChangeOption": OnlineChangeOption, "PromptHandling": PromptHandling}
    with open(script, encoding="utf-8") as f:
        code = compile(f.read(), script, "exec")
    if os.environ.get("FAKE_ME_REFUSE"):          # an installation that does not take scripts this way
        sys.exit(3)
    exec(code, scope)       # noqa: S102 — this is what the program does with a script
