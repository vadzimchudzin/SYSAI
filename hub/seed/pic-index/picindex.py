"""A symbol index for a folder of HMI equipment pictures: what each picture shows, in which state, which pictures
form one set. Built from file and folder names (plus manual labels), kept next to the pictures as pic-index.json,
updated incrementally and searched by meaning (Russian / Latin spelling, synonyms).

Standard library only. Nothing here looks inside a picture beyond its header (format, width, height)."""
import hashlib
import json
import os
import re
import struct
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache

INDEX_NAME = "pic-index.json"
INDEX_VERSION = 1
EXTENSIONS = {".png": "png", ".bmp": "bmp", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".svg": "svg"}
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 20000
HEAVY_BYTES = 1024 * 1024       # heavier pictures are reported by check(): a panel has little memory
HEAVY_PIXELS = 1024

STATES = ("off", "on", "alarm", "manual", "warning", "open", "closed", "opening", "closing", "unknown")
ORIENTATIONS = ("left", "right", "up", "down", "horizontal", "vertical")

# ----------------------------------------------------------------------------- dictionaries (extend here)
# Canonical Russian kind → the words that mean it in file names, folder names and queries. Any grammatical form and
# either spelling («нории», «noriya») is matched through key(), so one form per word is enough.
EQUIPMENT = {
    "нория": ("нория", "элеватор", "elevator", "noria"),
    "конвейер": ("конвейер", "транспортер", "conveyor", "conveyer", "transporter", "belt", "лента", "редлер", "redler"),
    "шнек": ("шнек", "screw", "auger"),
    "вентилятор": ("вентилятор", "fan", "vent", "ventilator", "blower"),
    "задвижка": ("задвижка", "gate", "slide", "шибер", "заслонка", "damper"),
    "клапан": ("клапан", "valve", "diverter"),
    "насос": ("насос", "pump"),
    "двигатель": ("двигатель", "мотор", "motor", "engine", "привод", "drive"),
    "силос": ("силос", "silo", "бункер", "bunker", "hopper", "bin", "емкость", "tank"),
    "сирена": ("сирена", "siren", "звонок", "bell", "horn", "buzzer"),
    "лампа": ("лампа", "лампочка", "lamp", "индикатор", "indicator", "led", "light"),
    "сепаратор": ("сепаратор", "separator"),
    "циклон": ("циклон", "cyclone"),
    "фильтр": ("фильтр", "filter"),
    "дробилка": ("дробилка", "crusher", "мельница", "mill"),
    "сушилка": ("сушилка", "dryer", "drier"),
    "весы": ("весы", "scale", "weigher"),
    "дозатор": ("дозатор", "doser", "feeder", "питатель"),
    "шлюз": ("шлюз", "airlock", "затвор"),
    "компрессор": ("компрессор", "compressor"),
    "смеситель": ("смеситель", "mixer"),
    "датчик": ("датчик", "sensor", "уровень", "level"),
    "труба": ("труба", "трубопровод", "самотек", "pipe", "tube"),
    "стрелка": ("стрелка", "arrow"),
    "кнопка": ("кнопка", "button", "btn"),
    "переключатель": ("переключатель", "тумблер", "switch"),
    "фон": ("фон", "подложка", "background", "bg"),
}
# State → the tokens that name it. Explicit words win over colors; the last one in the name wins.
STATE_WORDS = {
    "on": ("on", "run", "running", "work", "working", "active", "вкл", "включен", "работа", "работает", "пуск"),
    "off": ("off", "stop", "stopped", "idle", "выкл", "выключен", "стоп", "останов", "остановлен"),
    "alarm": ("alarm", "err", "error", "fault", "fail", "failure", "avar", "авар", "авария", "ошибка", "отказ"),
    "manual": ("man", "manual", "hand", "ручн", "ручной", "руч"),
    "warning": ("warn", "warning", "предупр", "предупреждение", "внимание"),
    "open": ("open", "opened", "откр", "открыт"),
    "closed": ("closed", "close", "закр", "закрыт"),
    "opening": ("opening", "открывается"),
    "closing": ("closing", "закрывается"),
}
COLOR_STATES = {
    "on": ("green", "зел", "зеленый"),
    "alarm": ("red", "красн"),
    "off": ("gray", "grey", "сер"),
    "warning": ("yellow", "желт"),
}
ORIENTATION_WORDS = {
    "left": ("left", "лев", "влево", "налево"),
    "right": ("right", "прав", "вправо", "направо"),
    "up": ("up", "верх", "вверх"),
    "down": ("down", "низ", "вниз"),
    "horizontal": ("horizontal", "horiz", "hor", "гор", "горизонт", "горизонтальный"),
    "vertical": ("vertical", "vert", "верт", "вертикальный"),
}
NUMBER_STATES = {"0": "off", "1": "on", "2": "alarm"}      # name_0 / name_1 / name_2 — a guess, see _finish()

# ----------------------------------------------------------------------------- words
_TRANSLIT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "i",
             "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
             "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "iu", "я": "ia"}
# Russian endings as they look after _fold(), longest first
_ENDINGS = ("iami", "ami", "ogo", "ego", "omu", "emu", "imi", "aia", "uiu", "iah", "ah", "ov", "ev", "om", "em", "am",
            "oe", "ee", "ie", "oi", "ei", "ia", "iu", "a", "o", "e", "i", "u")
_TOKEN_RE = re.compile(r"[A-ZА-ЯЁ]+(?![a-zа-яё])|[A-ZА-ЯЁ]?[a-zа-яё]+|\d+")


class PicError(Exception):
    pass


def tokenize(text: str) -> list:
    """«Нория_большая-ON2.v1» → ["Нория", "большая", "ON", "2", "v", "1"]: separators, camelCase, letter/digit borders."""
    return _TOKEN_RE.findall(text or "")


def _fold(word: str) -> str:
    """One Latin spelling for both alphabets: «нория», «noria» and «noriya» all become "noria"."""
    low = word.lower().replace("shch", "sch").replace("kh", "h")
    out = "".join(_TRANSLIT.get(ch, ch) for ch in low)
    out = out.replace("y", "i").replace("j", "i").replace("w", "v")
    return re.sub(r"i{2,}", "i", out)


@lru_cache(maxsize=20000)
def key(word: str) -> str:
    """The search key of a word: folded spelling without the plural «s» and one Russian ending (at least 3 letters stay)."""
    w = _fold(word)
    if len(w) > 3 and w.endswith("s") and w[-2] not in "aouis":
        w = w[:-1]
    for end in _ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= 3:
            return w[:-len(end)]
    return w


def _by_key(table: dict) -> dict:
    """key(word) → name; when two words give one key the earlier entry keeps it."""
    out = {}
    for name, words in table.items():
        for word in words:
            out.setdefault(key(word), name)
    return out


EQUIPMENT_KEYS = _by_key(EQUIPMENT)
EQUIPMENT_SYNONYMS = {name: {key(w) for w in words} for name, words in EQUIPMENT.items()}
STATE_KEYS = _by_key(STATE_WORDS)
COLOR_KEYS = _by_key(COLOR_STATES)
ORIENTATION_KEYS = _by_key(ORIENTATION_WORDS)


def _clean(text) -> str:
    return str(text or "").strip().lower().replace("ё", "е")


def equipment_of(text: str) -> str:
    """The canonical kind named by a word or phrase ('' — none): «Elevators» → «нория»."""
    for token in tokenize(text):
        kind = EQUIPMENT_KEYS.get(key(token))
        if kind:
            return kind
    return ""


# ----------------------------------------------------------------------------- picture headers
def _jpeg_size(data: bytes):
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0x00, 0x01) or 0xD0 <= marker <= 0xD9:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        i += 2 + max(length, 2)
    return None, None


def _svg_number(text):
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*(px)?\s*", text or "")
    return float(m.group(1)) if m else None


def _svg_size(data: bytes):
    head = data[:65536].decode("utf-8", "replace")
    tag = re.search(r"<svg\b[^>]*>", head, re.S | re.I)
    if not tag:
        return None, None
    attrs = dict((k.lower(), v) for k, v in re.findall(r"([A-Za-z:]+)\s*=\s*[\"']([^\"']*)[\"']", tag.group(0)))
    width, height = _svg_number(attrs.get("width")), _svg_number(attrs.get("height"))
    if width is None or height is None:
        box = re.split(r"[\s,]+", (attrs.get("viewbox") or "").strip())
        try:
            width, height = float(box[2]), float(box[3])
        except (IndexError, ValueError):
            return None, None
    return int(round(width)), int(round(height))


def picture_info(data: bytes, name: str = "") -> tuple:
    """(format, width, height) by the header; the format falls back to the extension, an unknown size is None."""
    fmt = EXTENSIONS.get(os.path.splitext(name)[1].lower(), "")
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            if data[12:16] == b"IHDR":
                return ("png",) + struct.unpack(">II", data[16:24])
            return "png", None, None
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return ("gif",) + struct.unpack("<HH", data[6:10])
        if data[:2] == b"BM" and len(data) >= 26:
            if struct.unpack("<I", data[14:18])[0] == 12:           # the old OS/2 header: 16-bit sizes
                return ("bmp",) + struct.unpack("<HH", data[18:22])
            width, height = struct.unpack("<ii", data[18:26])
            return "bmp", abs(width), abs(height)
        if data[:2] == b"\xff\xd8":
            return ("jpeg",) + _jpeg_size(data)
        if fmt == "svg" or b"<svg" in data[:2048]:
            return ("svg",) + _svg_size(data)
    except struct.error:
        pass
    return fmt, None, None


# ----------------------------------------------------------------------------- what a name says
def derive(rel: str) -> dict:
    """Equipment, state, orientation, base and search words of a picture by its relative path. `number` is the last
    number in the name: _finish() decides whether it is a state (name_0 / name_1) or a variant (noria1 / noria2)."""
    folder, _, fname = rel.rpartition("/")
    tokens = tokenize(os.path.splitext(fname)[0])
    keys = [key(t) for t in tokens]
    explicit = [i for i, k in enumerate(keys) if k in STATE_KEYS]
    colors = [i for i, k in enumerate(keys) if k in COLOR_KEYS]
    orients = [i for i, k in enumerate(keys) if k in ORIENTATION_KEYS]
    numbers = [i for i, t in enumerate(tokens) if t.isdigit()]
    state, drop = "unknown", set(orients) | set(numbers)
    if explicit:
        state = STATE_KEYS[keys[explicit[-1]]]
        drop |= set(explicit)
    elif colors:
        state = COLOR_KEYS[keys[colors[-1]]]
        drop |= set(colors)
    rest = [tokens[i].lower().replace("ё", "е") for i in range(len(tokens)) if i not in drop]
    folder_tokens = [t for part in reversed(folder.split("/")) for t in tokenize(part)]      # the nearest folder first
    equipment = ""
    for k in [key(t) for t in rest] + [key(t) for t in folder_tokens]:
        if k in EQUIPMENT_KEYS:
            equipment = EQUIPMENT_KEYS[k]
            break
    base = "_".join(rest) or "_".join(t.lower().replace("ё", "е") for t in tokenize(folder.rpartition("/")[2]) if not t.isdigit())
    words = []
    for k in keys + [key(t) for t in folder_tokens]:
        if k not in words and not k.isdigit():
            words.append(k)
    return {"equipment": equipment, "state": state, "orientation": ORIENTATION_KEYS[keys[orients[-1]]] if orients else "",
            "base": base, "words": words, "number": tokens[numbers[-1]] if numbers else ""}


def _finish(pictures: list, labels: dict) -> None:
    """Fill the fields derived from names, then what only the neighbours show (state numbers, numbered variants), with
    the manual labels on top. Runs over every record on each update: it is cheap, and a new dictionary takes effect."""
    groups = defaultdict(list)
    for rec in pictures:
        for name in ("state_guess", "labeled", "note"):
            rec.pop(name, None)
        rec.update(derive(rec["file"]))
        label = labels.get(rec["sha1"]) or {}
        for name in ("equipment", "state", "orientation", "base"):
            if label.get(name):
                rec[name] = label[name]
        if label.get("note"):
            rec["note"] = label["note"]
        if label:
            rec["labeled"] = True
            for token in tokenize(" ".join(str(label.get(n) or "") for n in ("equipment", "base"))):
                if key(token) not in rec["words"] and not token.isdigit():
                    rec["words"].append(key(token))
        groups[(rec["file"].rpartition("/")[0], rec["base"])].append((rec, label))
    for members in groups.values():
        guess = [rec for rec, label in members if rec["state"] == "unknown" and rec["number"] in NUMBER_STATES and not label.get("state")]
        if len({rec["number"] for rec in guess}) >= 2:      # name_0 + name_1 (+ name_2): the number is the state
            for rec in guess:
                rec["state"], rec["state_guess"], rec["number"] = NUMBER_STATES[rec["number"]], True, ""
        numbers = {rec["number"] for rec, _ in members}
        pairs = Counter((rec["state"], rec["orientation"]) for rec, _ in members)
        if "" not in numbers and len(numbers) > 1 and max(pairs.values()) > 1:      # noria1_on + noria2_on: two sets
            for rec, label in members:
                if not label.get("base"):
                    rec["base"] = f"{rec['base']}_{rec['number']}" if rec["base"] else rec["number"]
    for rec in pictures:
        rec.pop("number", None)


# ----------------------------------------------------------------------------- the index file
def _root(folder) -> str:
    root = os.path.abspath(os.path.expanduser(str(folder or "")))
    if not folder or not os.path.isdir(root):
        raise PicError(f"нет папки {folder}")
    return root


def _load(root: str) -> dict:
    try:
        with open(os.path.join(root, INDEX_NAME), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict) or not isinstance(doc.get("pictures"), list) or not isinstance(doc.get("labels", {}), dict):
        return {}
    doc["pictures"] = [r for r in doc["pictures"] if isinstance(r, dict) and isinstance(r.get("file"), str) and r.get("sha1")]
    doc["labels"] = {k: v for k, v in (doc.get("labels") or {}).items() if isinstance(v, dict)}
    return doc


def _save(root: str, index: dict) -> None:
    """Atomically: a temporary file next to the index, then os.replace."""
    path = os.path.join(root, INDEX_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _scan(root: str) -> tuple:
    """([(relative path, bytes, mtime)], skipped large files, cut at MAX_FILES?) — pictures only, no dotfiles."""
    found, large = [], []
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or name == INDEX_NAME or os.path.splitext(name)[1].lower() not in EXTENSIONS:
                continue
            full = os.path.join(here, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if st.st_size > MAX_FILE_BYTES:
                large.append(rel)
                continue
            if len(found) >= MAX_FILES:
                return found, large, True
            found.append((rel, st.st_size, round(st.st_mtime, 3)))
    return found, large, False


def refresh(folder, full: bool = False, write: bool = True) -> tuple:
    """(index, stats): bring the index in line with the folder. An unchanged file (same size and mtime) keeps its
    hash and size; `full` reads every file again. The file is written only when something changed; a folder that
    cannot be written still gives a working index in memory (stats["saved"] is False)."""
    root = _root(folder)
    old = _load(root)
    labels = old.get("labels") or {}
    before = {r["file"]: r for r in old.get("pictures") or []}
    scanned, large, cut = _scan(root)
    pictures, added, changed, unreadable = [], [], 0, []
    for rel, size, mtime in scanned:
        prev = before.get(rel)
        if prev and not full and prev.get("bytes") == size and prev.get("mtime") == mtime:
            rec = {k: prev.get(k) for k in ("file", "sha1", "bytes", "mtime", "format", "width", "height")}
        else:
            try:
                with open(os.path.join(root, rel), "rb") as f:
                    data = f.read(MAX_FILE_BYTES + 1)
            except OSError:
                unreadable.append(rel)
                continue
            fmt, width, height = picture_info(data, rel)
            rec = {"file": rel, "sha1": hashlib.sha1(data).hexdigest(), "bytes": len(data), "mtime": mtime, "format": fmt,
                   "width": width, "height": height}
            if not prev:
                added.append(rec)
            elif prev["sha1"] != rec["sha1"] or not full:
                changed += 1
        pictures.append(rec)
    now = {r["file"] for r in pictures}
    gone = [r for r in before.values() if r["file"] not in now]
    gone_hashes = Counter(r["sha1"] for r in gone)
    moved = 0
    for rec in added:                       # the same content under a new name: moved, its labels stay (they go by sha1)
        if gone_hashes[rec["sha1"]] > 0:
            gone_hashes[rec["sha1"]] -= 1
            moved += 1
    _finish(pictures, labels)
    index = {"version": INDEX_VERSION, "updated": old.get("updated") or "", "pictures": pictures, "labels": labels}
    saved = True
    if not old or old.get("pictures") != pictures or old.get("version") != INDEX_VERSION:
        index["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        saved = False
        if write:
            try:
                _save(root, index)
                saved = True
            except OSError:
                pass
    unknown = [r["file"] for r in pictures if not r["equipment"]]
    stats = {"dir": root, "index": os.path.join(root, INDEX_NAME), "saved": saved, "total": len(pictures),
             "added": len(added) - moved, "changed": changed, "moved": moved, "removed": len(gone) - moved,
             "sets": len(sets_of(pictures)), "unrecognized": len(unknown), "unrecognized_files": unknown[:50]}
    if large:
        stats["skipped_large"] = large[:50]
    if unreadable:
        stats["unreadable"] = unreadable[:50]
    if cut:
        stats["truncated"] = f"в индекс вошли первые {MAX_FILES} картинок"
    return index, stats


def _ready(folder) -> tuple:
    """(root, index) for the reading tools: the index is rebuilt silently when it is missing or behind the folder."""
    index, stats = refresh(folder)
    if not index["pictures"]:
        raise PicError(f"в папке нет картинок: {stats['dir']} (ищутся {', '.join(sorted(EXTENSIONS))})")
    return stats["dir"], index


# ----------------------------------------------------------------------------- sets
def _size(members: list) -> str:
    sizes = {(r.get("width"), r.get("height")) for r in members}
    if len(sizes) == 1:
        width, height = sizes.pop()
        if width and height:
            return f"{width}x{height}"
    return ""


def sets_of(pictures: list) -> list:
    """Pictures sharing folder + base are one set. `states` maps a state to its file (pictures without an orientation
    first); when the set has several orientations `by_orientation` gives the same map for each of them."""
    groups = defaultdict(list)
    for rec in pictures:
        groups[(rec["file"].rpartition("/")[0], rec["base"])].append(rec)
    out = []
    for (folder, base), members in sorted(groups.items()):
        members.sort(key=lambda r: (r["orientation"] != "", r["file"]))
        kinds = Counter(r["equipment"] for r in members if r["equipment"])
        states, by_orientation = {}, {}
        for rec in members:
            states.setdefault(rec["state"], rec["file"])
            if rec["orientation"]:
                by_orientation.setdefault(rec["orientation"], {}).setdefault(rec["state"], rec["file"])
        notes = []
        for rec in members:
            if rec.get("note") and rec["note"] not in notes:
                notes.append(rec["note"])
        one = {"equipment": kinds.most_common(1)[0][0] if kinds else "", "base": base, "folder": folder, "size": _size(members),
               "states": {s: states[s] for s in STATES if s in states}, "files": sorted(r["file"] for r in members),
               "note": "; ".join(notes)}
        if by_orientation:
            one["by_orientation"] = by_orientation
        if any(r.get("state_guess") for r in members):
            one["state_guess"] = True
        out.append(one)
    return out


def _set_name(one: dict) -> str:
    return (one["folder"] + "/" if one["folder"] else "") + (one["base"] or "?")


def _near(word: str, keys: set) -> bool:
    return len(word) >= 3 and any(k.startswith(word) or (len(k) >= 4 and word.startswith(k)) for k in keys)


def _states_arg(state) -> list:
    wanted = [s for s in re.split(r"[\s,;]+", _clean(state)) if s]
    for s in wanted:
        if s not in STATES:
            raise PicError(f"нет состояния {s}: бывают {', '.join(STATES)}")
    return wanted


def find(folder, query: str, state: str = "", limit: int = 10) -> dict:
    """Sets by meaning, best first. Equipment match > word of the name > word of the folder or the note; a state or
    an orientation named in the query lifts the sets that have it; `state` keeps only the sets that have them all."""
    root, index = _ready(folder)
    terms, soft = [], []
    for token in tokenize(str(query or "")):
        k = key(token)
        if token.isdigit():
            continue
        if k in STATE_KEYS or k in COLOR_KEYS:
            soft.append(("state", STATE_KEYS.get(k) or COLOR_KEYS[k], k))
        elif k in ORIENTATION_KEYS:
            soft.append(("orientation", ORIENTATION_KEYS[k], k))
        elif k not in terms:
            terms.append(k)
    if not terms and not soft:
        raise PicError("пустой запрос: назови оборудование, например «нория» или «conveyor»")
    wanted = _states_arg(state)
    words = {r["file"]: r["words"] for r in index["pictures"]}
    found = []
    for one in sets_of(index["pictures"]):
        if any(s not in one["states"] for s in wanted):
            continue
        folder_keys = {key(t) for t in tokenize(one["folder"])}
        name_keys = {k for f in one["files"] for k in words[f]} - folder_keys | {key(t) for t in tokenize(one["base"])}
        note_keys = {key(t) for t in tokenize(one["note"])}
        kind_keys = {key(t) for t in tokenize(one["equipment"])}
        score = matched = 0
        for q in terms:
            kind = EQUIPMENT_KEYS.get(q)
            same = EQUIPMENT_SYNONYMS.get(kind, set()) | {q}
            if (kind and kind == one["equipment"]) or q in kind_keys:
                points = 10
            elif same & name_keys:
                points = 5
            elif _near(q, name_keys):
                points = 3
            elif same & (folder_keys | note_keys):
                points = 2
            elif _near(q, folder_keys | note_keys):
                points = 1
            else:
                continue
            score, matched = score + points, matched + 1
        for what, value, k in soft:
            has = value in one["states"] if what == "state" else value in one.get("by_orientation", {})
            score += (2 if has else 0) + (3 if k in name_keys else 0)
        if (matched if terms else score) > 0:
            found.append((-matched, -score, -len(one["states"]), _set_name(one), one))
    found.sort(key=lambda x: x[:4])
    limit = max(1, min(int(limit or 10), 100))
    result = {"dir": root, "query": query, "found": len(found), "sets": [x[4] for x in found[:limit]]}
    if not found:
        result["hint"] = "ничего не нашлось: посмотри pics_list (какое оборудование есть) или подпиши картинки pics_label_update"
    return result


def overview(folder, equipment: str = "") -> dict:
    """Equipment kinds with the number of sets and pictures, and the sets themselves (optionally of one kind)."""
    root, index = _ready(folder)
    sets = sets_of(index["pictures"])
    kinds = {}
    for one in sets:
        row = kinds.setdefault(one["equipment"], {"equipment": one["equipment"], "sets": 0, "pictures": 0})
        row["sets"] += 1
        row["pictures"] += len(one["files"])
    want = _clean(equipment)
    if want:
        kind, want_keys = equipment_of(want) or want, {key(t) for t in tokenize(want)}
        sets = [s for s in sets if s["equipment"] == kind or want_keys & {key(t) for t in tokenize(s["equipment"])}]
    rows = [{"equipment": s["equipment"], "name": s["base"], "folder": s["folder"], "states": list(s["states"]), "size": s["size"],
             "pictures": len(s["files"])} for s in sets]
    return {"dir": root, "pictures": len(index["pictures"]), "sets": len(rows),
            "unlabeled": sum(1 for r in index["pictures"] if not r["equipment"]),
            "equipment": sorted((k for k in kinds.values() if k["equipment"]), key=lambda k: (-k["sets"], k["equipment"])),
            "list": rows[:1000]}


def label(folder, file: str, equipment=None, state=None, orientation=None, base=None, note=None) -> dict:
    """A manual label of one picture, kept under labels[sha1]: None leaves a field, '' clears it. Returns the record."""
    root, index = _ready(folder)
    name = str(file or "")
    if os.path.isabs(name):
        name = os.path.relpath(name, root)
    name = os.path.normpath(name).replace(os.sep, "/")
    rec = next((r for r in index["pictures"] if r["file"] == name), None) \
        or next((r for r in index["pictures"] if r["file"].lower() == name.lower()), None)
    if rec is None:
        raise PicError(f"нет картинки {file} в индексе папки {root}")
    new = dict(index["labels"].get(rec["sha1"]) or {})
    fields = {"equipment": equipment, "state": state, "orientation": orientation, "base": base, "note": note}
    for field, value in fields.items():
        if value is None:
            continue
        value = str(value).strip() if field == "note" else _clean(value)
        if field == "state" and value and value not in STATES:
            raise PicError(f"нет состояния {value}: бывают {', '.join(STATES)}")
        if field == "orientation" and value and value not in ORIENTATIONS:
            raise PicError(f"нет положения {value}: бывают {', '.join(ORIENTATIONS)}")
        if field == "equipment" and value:
            value = equipment_of(value) or value
        if field == "base" and value:
            value = "_".join(tokenize(value)).lower()
        if value:
            new[field] = value
        else:
            new.pop(field, None)
    if new:
        index["labels"][rec["sha1"]] = new
    else:
        index["labels"].pop(rec["sha1"], None)
    _finish(index["pictures"], index["labels"])
    index["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save(root, index)
    return next(r for r in index["pictures"] if r["file"] == rec["file"])


def check(folder) -> dict:
    """Problems of the library as [{level, file | set, text}]: incomplete sets, sizes, duplicates, heavy pictures."""
    root, index = _ready(folder)
    pictures, issues = index["pictures"], []
    by_file = {r["file"]: r for r in pictures}
    for one in sets_of(pictures):
        name, states = _set_name(one), one["states"]
        for have, need, level in (("on", "off", "warning"), ("open", "closed", "warning"), ("closed", "open", "warning"),
                                  ("alarm", "off", "warning"), ("on", "alarm", "info")):
            if have in states and need not in states:
                issues.append({"level": level, "set": name, "text": f"в наборе есть состояние {have}, но нет {need}"})
        sizes = sorted({f"{by_file[f]['width']}x{by_file[f]['height']}" for f in one["files"] if by_file[f]["width"]})
        if len(sizes) > 1:
            issues.append({"level": "warning", "set": name,
                           "text": "картинки набора разного размера: " + ", ".join(sizes) + " — на экране состояние будет «прыгать»"})
        if one.get("state_guess"):
            issues.append({"level": "info", "set": name,
                           "text": "состояния угаданы по номерам в именах (0 — off, 1 — on, 2 — alarm): посмотри картинки и "
                                   "подпиши pics_label_update, если это не так"})
    same = defaultdict(list)
    for rec in pictures:
        same[rec["sha1"]].append(rec["file"])
    for files in same.values():
        if len(files) > 1:
            issues.append({"level": "info", "file": files[0], "text": "одинаковые картинки: " + ", ".join(files)})
    for rec in pictures:
        if not rec["equipment"]:
            issues.append({"level": "info", "file": rec["file"],
                           "text": "оборудование по имени не распознано: посмотри картинку и подпиши pics_label_update"})
        if rec["width"] is None:
            issues.append({"level": "warning", "file": rec["file"], "text": "размер не прочитан: файл повреждён или не картинка"})
        heavy = []
        if rec["bytes"] > HEAVY_BYTES:
            heavy.append(f"{rec['bytes'] / 1048576:.1f} МБ")
        if max(rec["width"] or 0, rec["height"] or 0) > HEAVY_PIXELS:
            heavy.append(f"{rec['width']}x{rec['height']}")
        if heavy:
            issues.append({"level": "warning", "file": rec["file"], "text": "тяжёлая картинка для панели: " + ", ".join(heavy)})
    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: order[i["level"]])
    return {"dir": root, "summary": {k: sum(1 for i in issues if i["level"] == k) for k in order}, "issues": issues[:2000]}
