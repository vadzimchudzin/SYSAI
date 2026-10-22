"""Measuring recognition quality: word error rate (WER) and how many names and terms were heard right.

Both sides are normalised the same way before comparing: lower case, «ё» → «е», no punctuation, numbers spoken in words
become digits («двенадцать тысяч» = «12000»), digit groups are joined («12 000» = «12000»). WER = (substitutions +
deletions + insertions) / words in the reference.
"""
import re

_UNITS = {"ноль": 0, "один": 1, "одна": 1, "одно": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
          "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
          "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
          "девятнадцать": 19}
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70,
         "восемьдесят": 80, "девяносто": 90}
_HUNDREDS = {"сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500, "шестьсот": 600,
             "семьсот": 700, "восемьсот": 800, "девятьсот": 900}
_THOUSAND = {"тысяча", "тысячи", "тысяч"}


def _numbers(words: list) -> list:
    out, value, current, active = [], 0, 0, False
    for w in words + [""]:
        if w in _UNITS or w in _TENS or w in _HUNDREDS:
            current += _UNITS.get(w, 0) + _TENS.get(w, 0) + _HUNDREDS.get(w, 0)
            active = True
        elif w in _THOUSAND and (active or True):
            value += (current or 1) * 1000
            current, active = 0, True
        else:
            if active:
                out.append(str(value + current))
            value, current, active = 0, 0, False
            if w:
                out.append(w)
    return out


def normalize(text: str) -> list:
    t = str(text or "").lower().replace("ё", "е")
    t = re.sub(r"(?<=\d)[\s\u00a0\u202f](?=\d{3}\b)", "", t)  # 12 000 → 12000
    t = re.sub(r"(?<=\d)[.,](?=\d)", "", t)
    t = re.sub(r"[^\w\s-]", " ", t).replace("-", " ")
    return _numbers(t.split())


def distance(ref: list, hyp: list) -> int:
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, start=1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1]


def wer(reference: str, hypothesis: str) -> float:
    ref, hyp = normalize(reference), normalize(hypothesis)
    return distance(ref, hyp) / max(1, len(ref))


def terms_heard(reference: str, hypothesis: str, terms: list) -> tuple:
    """(heard right, said) for vocabulary words (any case form, matched on the first 5 letters)."""
    from .speech_fix import _base
    said = heard = 0
    hyp = " ".join(normalize(hypothesis))
    for term in terms:
        stem = _base(term).lower().replace("ё", "е")[:6]
        n_ref = len(re.findall(rf"\b{re.escape(stem)}", " ".join(normalize(reference))))
        n_hyp = len(re.findall(rf"\b{re.escape(stem)}", hyp))
        said += n_ref
        heard += min(n_ref, n_hyp)
    return heard, said
