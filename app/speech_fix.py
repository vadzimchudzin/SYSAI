"""Correcting names and company terms after speech recognition.

Recognition hears «ВентСнаба» as «винсунаба» and «Вадиму» as «Вадему». The vocabulary (people and their nicknames from
the directory, the glossary, departments) is known, so a word that sounds like a vocabulary word is replaced by it,
keeping the Russian ending: «винсунаба» → «ВентСнаба». Sound-alike is decided on a phonetic key (unstressed vowels and
voiced/voiceless pairs merged, clusters simplified), never on meaning.

Two passes: a strict rule pass that needs no model, and an optional model pass that proposes replacements which code
accepts only when the target is in the vocabulary and the words still sound alike.
"""
import difflib
import json
import logging
import re

from . import config, llm, settings_store

log = logging.getLogger("sysai.speech_fix")

_WORD = re.compile(r"[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z0-9\-]*")
ENDINGS = ("ами", "ями", "ому", "ему", "ого", "его", "ой", "ей", "ою", "ею", "ом", "ем", "ам", "ям", "ах", "ях",
           "ов", "ев", "ы", "и", "а", "я", "у", "ю", "е", "о")
_PAIRS = str.maketrans({"б": "п", "в": "ф", "г": "к", "д": "т", "ж": "ш", "з": "с", "о": "а", "ё": "е", "э": "е",
                        "ы": "и", "я": "а", "ю": "у", "й": "и", "ъ": "", "ь": "", "щ": "ш"})
_LATIN = {"sh": "ш", "ch": "ч", "zh": "ж", "ts": "ц", "ya": "я", "yu": "ю", "a": "а", "b": "б", "c": "к", "d": "д",
          "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о",
          "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс", "y": "и", "z": "з"}
RULE_THRESHOLD = 0.86
MODEL_THRESHOLD = 0.6


def _translit(word: str) -> str:
    out, i, w = "", 0, word.lower()
    while i < len(w):
        if w[i:i + 2] in _LATIN:
            out, i = out + _LATIN[w[i:i + 2]], i + 2
        else:
            out, i = out + _LATIN.get(w[i], w[i]), i + 1
    return out


def phonetic(word: str) -> str:
    """A sound key: «ВентСнаб», «винсунаб», «Вэнтснап» end up close to each other."""
    w = _translit(word) if re.search(r"[a-z]", word.lower()) else word.lower()
    w = w.replace("-", "").translate(_PAIRS)
    w = re.sub(r"[еи]", "и", w)
    for cluster, simple in (("нтс", "нс"), ("тс", "ц"), ("тьс", "ц"), ("стн", "сн"), ("здн", "сн"), ("лнц", "нц")):
        w = w.replace(cluster, simple)
    return re.sub(r"(.)\1+", r"\1", w)


def split(word: str) -> tuple:
    """(stem, ending) of a Russian word form."""
    low = word.lower()
    for ending in ENDINGS:
        if low.endswith(ending) and len(low) - len(ending) >= 3:
            return word[:-len(ending)], word[-len(ending):]
    return word, ""


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, phonetic(a), phonetic(b)).ratio()


def vocabulary(settings: dict = None) -> list:
    """Words worth spelling right: people (names, surnames, nicknames), glossary terms, departments."""
    from .db import Department, Employee, SessionLocal
    words = set()
    try:
        with SessionLocal() as s:
            for e in s.query(Employee).filter(Employee.active.is_(True)).all():
                words.update(_WORD.findall(e.name or ""))
                for alias in (e.aliases or "").split(","):
                    words.update(_WORD.findall(alias))
            for d in s.query(Department).filter(Department.active.is_(True)).all():
                words.update(w for w in _WORD.findall(d.name or "") if w[:1].isupper())
    except Exception:  # noqa: BLE001  no database: the glossary alone
        pass
    glossary = str((settings.get("glossary") if settings is not None else settings_store.get("glossary")) or "")
    for term in re.split(r"[,;\n]", glossary):
        for w in _WORD.findall(term):
            if w[:1].isupper() or re.search(r"[A-Z]", w):
                words.add(w)
    return sorted(w for w in words if len(w) >= 4)


def prompt_text(settings: dict = None) -> str:
    """The recognition hint: names and terms, comma separated (the STT «prompt»)."""
    return ", ".join(vocabulary(settings))[:900]


def _forms(word: str):
    """Every way to read a word as stem + case ending (the whole word first)."""
    yield word, ""
    low = word.lower()
    for ending in ENDINGS:
        if low.endswith(ending) and len(low) - len(ending) >= 3:
            yield word[:-len(ending)], word[-len(ending):]


def _base(term: str) -> str:
    """The part of a vocabulary word (a dictionary form) that endings attach to: «Ольга» → «Ольг», «Игорь» → «Игор»,
    «Соколов» stays «Соколов» (→ «Соколову»)."""
    if term[-1:].lower() in ("а", "я", "ь", "й", "о", "е") and len(term) > 4:
        return term[:-1]
    return term


def _same(a: str, b: str) -> bool:
    return a.lower().replace("ё", "е") == b.lower().replace("ё", "е")


def _best(word: str, vocab: list):
    """(replacement or None, score). None with 1.0 means the word is already a correct form of a vocabulary word."""
    best, score = None, 0.0
    for term in vocab:
        base = _base(term)
        for stem, ending in _forms(word):
            if _same(stem + ending, term) or (ending and _same(stem, base)):
                return None, 1.0
            target = term if not ending else base
            value = similarity(stem, target)
            if value > score + 1e-9 or (abs(value - score) < 1e-9 and best and len(ending) > len(best[1])):
                fixed = term if not ending else base + ending
                best, score = (fixed, ending), value
    return best, score


def _apply_form(word: str, best) -> str:
    fixed, _ = best
    return fixed


def fix_text(text: str, vocab: list, threshold: float = RULE_THRESHOLD) -> tuple:
    """(text, replacements) after the rule pass."""
    if not vocab:
        return text, []
    known = {v.lower() for v in vocab}
    changes = []

    def repl(match):
        word = match.group(0)
        if len(word) < 4 or word.lower() in known:
            return word
        best, score = _best(word, vocab)
        if best is None or score < threshold or phonetic(word[:1]) != phonetic(best[0][:1]):
            return word
        fixed = _apply_form(word, best)
        if fixed != word:
            changes.append((word, fixed))
        return fixed
    return _WORD.sub(repl, text), changes


def model_replacements(text: str, vocab: list) -> list:
    """The model proposes [from, to] pairs; only sound-alike pairs onto vocabulary words are kept."""
    if not vocab or not config.llm_enabled() and not config.TESTING:
        return []
    from . import voice
    try:
        raw = llm.chat(voice.model(), [
            {"role": "system", "content": "Это расшифровка речи. Найди слова, которые распознаны неверно и на самом деле "
                                          "являются именами или терминами из словаря (по звучанию). Ничего другого не "
                                          "исправляй. JSON: {\"replacements\": [{\"from\": \"слово как в тексте\", "
                                          "\"to\": \"правильное слово в том же падеже\"}]}"},
            {"role": "user", "content": "Словарь: " + ", ".join(vocab) + "\n<data>" + text[:20000] + "</data>"}],
            json_mode=True, temperature=0.0, max_tokens=1200, timeout=90)
        proposed = (llm.parse_json(raw) or {}).get("replacements") or []
    except Exception as exc:  # noqa: BLE001  correction is a bonus, never a failure
        log.info("model correction unavailable: %s", exc)
        return []
    stems = {split(v)[0].lower(): v for v in vocab} | {v.lower(): v for v in vocab}
    kept = []
    for item in proposed[:60]:
        if not isinstance(item, dict):
            continue
        source, target = str(item.get("from") or "").strip(), str(item.get("to") or "").strip()
        if not source or not target or source == target or " " in source or source not in text:
            continue
        tstem = split(target)[0].lower()
        if target.lower() not in stems and tstem not in stems:
            continue  # only words of the vocabulary may be written in
        if similarity(source, target) >= MODEL_THRESHOLD:
            kept.append((source, target))
    return kept


def fix_segments(segments: list, settings: dict = None, use_model: bool = True) -> tuple:
    """Correct names and terms in a transcript (in place on copies). Returns (segments, replacements)."""
    enabled = (settings or {}).get("speech_fix") if settings and "speech_fix" in settings else settings_store.get("speech_fix")
    if enabled is False:
        return segments, []
    vocab = vocabulary(settings)
    if not vocab:
        return segments, []
    out, changes = [], []
    for seg in segments:
        text, found = fix_text(str(seg.get("text") or ""), vocab)
        changes += found
        out.append(dict(seg, text=text))
    if use_model:
        joined = "\n".join(s["text"] for s in out)
        pairs = model_replacements(joined, vocab)
        for source, target in pairs:
            pattern = re.compile(rf"(?<![А-Яа-яЁёA-Za-z]){re.escape(source)}(?![А-Яа-яЁёA-Za-z])")
            for seg in out:
                if pattern.search(seg["text"]):
                    seg["text"] = pattern.sub(target, seg["text"])
                    changes.append((source, target))
    if changes:
        log.info("speech fix: %s", json.dumps(changes[:20], ensure_ascii=False))
    return out, changes
