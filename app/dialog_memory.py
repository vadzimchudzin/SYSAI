"""The memory of a whole conversation with one person.

Every message of a chat is kept (conversation_messages). The model gets the fresh part of the conversation word for
word and a short retelling of everything older: the model writes it in the background, a batch of messages at a time,
and keeps agreements, promises, dates, numbers and documents. When a new message touches something said long ago («как
договаривались», «тот акт»), code finds the related old messages by their words and shows them next to the fresh ones
(from_earlier); the model may also ask to look through the whole history of the chat itself (recall) and answers with
what was found. Nothing here stops a reply: without a model there is no retelling, and the search needs no model.
"""
import collections
import concurrent.futures
import json
import logging
import math
import re
import threading

from sqlalchemy import func

from . import config, llm, memory as company_memory, reminders, voice
from .db import ConversationMessage, DialogMemory, SessionLocal

log = logging.getLogger("sysai.dialog")

FRESH = 24            # messages after the retelling that stay word for word once it is written
BATCH = 16            # a retelling is written when this many more have piled up: 24..40 messages are fresh
MAX_FRESH = FRESH + BATCH
FRESH_CHARS = 16000   # the fresh part in the prompt: at most this many characters, the newest messages first
MESSAGE_CHARS = 4000  # one fresh message (longer ones are cut at the end)
CHUNK = 60            # messages per retelling call
MAX_CHUNKS = 3        # calls per refresh: a long old chat catches up over several replies
MAX_SUMMARY = 2000    # the model is asked for 1500 characters
SCAN = 3000           # the newest old messages searched for a new message
SCAN_CHARS = 1500     # of each of them
EARLIER, EARLIER_CHARS, EARLIER_TEXT = 3, 2500, 600   # found by code for a new message: episodes, characters
RECALL, RECALL_CHARS, RECALL_TEXT = 5, 5000, 900      # found when the model asks
MIN_SCORE = 2.0       # code shows an old message on its own only when the shared words are rare enough
RARE = 3              # a single shared word is enough when no more than this many old messages have it
MAX_QUERY_KEYS = 24
ROLES = ("user", "assistant")
STATUSES = ("agent_pending", "agent_done")
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="dialog")
_busy, _busy_lock = set(), threading.Lock()


# ----------------------------------------------------------------------------- words
_WORD = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
_ENDINGS = tuple(sorted({
    "иями", "ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "ией", "иях", "ах", "ях", "ом", "ем", "ой", "ей",
    "ий", "ый", "ая", "яя", "ое", "ее", "ые", "ие", "ую", "юю", "ов", "ев", "ия", "ии", "ть", "ла", "ли", "ет", "ит",
    "ут", "ют", "ат", "ят", "а", "я", "ы", "и", "у", "ю", "е", "о", "ь", "й"}, key=len, reverse=True))
# Words that point at the past but say nothing about what: «как договаривались», «тот акт», «помнишь, ты писал».
_CUES = {w.replace("ё", "е") for w in """
    как помнишь помните помню напомни напомните тот та то те того той ту тем тех тому прошлый прошлая прошлое прошлого
    прошлой прошлом прошлую раз разе тогда давно недавно говорил говорила говорили сказал сказала сказали писал писала
    писали обсуждали обсуждал обещал обещала обещали который которая которое которые которого которой которую ну вот
    это эту этого этой этом там тут какой какая какое какие какую какого сколько куда зачем почему мой моя мое мои
    твой твоя ваш наш наша наши нам вам ему сейчас теперь опять снова привет здравствуй здравствуйте добрый доброе
    спасибо пожалуйста окей хорошо ладно понял поняла принято отлично супер нет""".split()}
_CUE_STARTS = ("договарива", "договорил", "договорим", "договорен")


def key(word: str) -> str:
    """A word without its ending, at most 6 letters: «акта», «акты», «актом» → «акт»; numbers stay as they are."""
    w = word.lower().replace("ё", "е")
    if w.isdigit():
        return w.lstrip("0") or "0"
    for ending in _ENDINGS:
        if w.endswith(ending) and len(w) - len(ending) >= 3:
            return w[:-len(ending)][:6]
    return w[:6]


def keys(text: str) -> list:
    out = []
    for word in _WORD.findall(text or ""):
        low = word.lower().replace("ё", "е")
        if not low.isdigit() and (len(low) < 3 or low in company_memory._STOP or low in _CUES
                                  or low.startswith(_CUE_STARTS)):
            continue
        out.append(key(low))
    return out


def _index(text: str) -> tuple:
    exact = set(keys(text))
    grown = set(exact)
    for k in exact:
        if not k.isdigit():
            grown.update(k[:n] for n in range(4, len(k)))
    return exact, grown


def _hit(q: str, exact: set, grown: set) -> bool:
    """Does the message have the word: the same, or one is the beginning of the other (four letters at least)."""
    if q in exact:
        return True
    if q.isdigit() or len(q) < 4:
        return False
    return q in grown or any(q[:n] in exact for n in range(4, len(q)))


def _same_word(word_key: str, wanted) -> bool:
    return any(word_key == q or (min(len(q), len(word_key)) >= 4 and not q.isdigit()
                                 and (word_key.startswith(q) or q.startswith(word_key))) for q in wanted)


# ----------------------------------------------------------------------------- the messages
def _who(role: str) -> str:
    return "человек" if role == "user" else "ты"


def _when(moment):
    return reminders.local_text(moment)[:10] if moment is not None else None


def clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= MESSAGE_CHARS else text[:MESSAGE_CHARS] + "…"


def _snippet(text: str, wanted, limit: int) -> str:
    """The message, or the part of a long one around the first word that was looked for."""
    text = text or ""
    if len(text) <= limit:
        return text
    at = next((m.start() for m in _WORD.finditer(text) if _same_word(key(m.group(0)), wanted)), 0)
    start = max(0, at - limit // 3)
    end = start + limit
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def _talk(s, chat: str):
    return s.query(ConversationMessage).filter(ConversationMessage.chat_id == chat,
                                               ConversationMessage.role.in_(ROLES),
                                               ConversationMessage.status.in_(STATUSES))


def _rows(s, chat: str, before_id=None, after_id: int = 0, limit: int = SCAN) -> list:
    """(id, role, text, created_at) of the chat in time order: the newest `limit` between the two ids."""
    query = (s.query(ConversationMessage.id, ConversationMessage.role,
                     func.substr(ConversationMessage.text, 1, SCAN_CHARS).label("text"), ConversationMessage.created_at)
             .filter(ConversationMessage.chat_id == chat, ConversationMessage.role.in_(ROLES),
                     ConversationMessage.status.in_(STATUSES)))
    if before_id is not None:
        query = query.filter(ConversationMessage.id < before_id)
    if after_id:
        query = query.filter(ConversationMessage.id > after_id)
    rows = query.order_by(ConversationMessage.id.desc()).limit(limit).all()
    rows.reverse()
    return rows


def window(s, chat: str, before_id: int) -> dict:
    """What the model sees of the conversation before message `before_id`: the retelling of the older part, the fresh
    messages word for word (oldest first, at most FRESH_CHARS) and how many messages there were in all."""
    stored = s.get(DialogMemory, chat)
    summary = (stored.summary or "").strip() if stored else ""
    upto = (stored.upto_id or 0) if summary else 0
    total = _talk(s, chat).filter(ConversationMessage.id < before_id).count()
    recent = (_talk(s, chat).filter(ConversationMessage.id < before_id, ConversationMessage.id > upto)
              .order_by(ConversationMessage.id.desc()).limit(MAX_FRESH).all())
    fresh, used = [], 0
    for row in recent:
        size = len(clip(row.text))
        if fresh and used + size > FRESH_CHARS:
            break
        fresh.append(row)
        used += size
    fresh.reverse()
    return {"fresh": fresh, "total": total, "summary": summary}


# ----------------------------------------------------------------------------- finding old messages
def _pick(query: str, rows: list, *, strict: bool, k: int) -> tuple:
    """Indexes of the rows that speak about what the query does (best first) and the words looked for.
    strict: code looks on its own, so only clearly related messages (rare shared words, most of the query)."""
    wanted = list(dict.fromkeys(keys(query)))[:MAX_QUERY_KEYS]
    if not wanted or not rows:
        return [], wanted
    hits, df = [], collections.Counter()
    probes = {q if len(q) < 4 or q.isdigit() else q[:4] for q in wanted}  # a match needs one of them in the text
    for i, row in enumerate(rows):
        low = (row.text or "").lower().replace("ё", "е")
        if not any(p in low for p in probes):
            continue
        exact, grown = _index(low)
        matched = {q for q in wanted if _hit(q, exact, grown)}
        if matched:
            hits.append((i, matched))
            df.update(matched)
    n = len(rows)
    idf = {q: math.log((n + 1) / (df[q] + 0.5)) for q in df}
    total = sum(idf.values())
    scored = []
    for i, matched in hits:
        score = sum(idf[q] for q in matched)
        if strict and (score < MIN_SCORE or score < 0.6 * total
                       or (len(matched) < 2 and df[next(iter(matched))] > RARE)):
            continue
        scored.append((score, i))
    scored.sort(key=lambda x: (-x[0], -x[1]))  # the best, and of equal ones the newest
    return [i for _, i in scored[:k]], wanted


def _episodes(rows: list, picked: list, wanted, chars: int, text_limit: int) -> list:
    """Each found message with the other side's message next to it (the question and the answer), in time order."""
    spans, used = [], 0
    for i in picked:
        lo = hi = i
        if rows[i].role == "user" and i + 1 < len(rows) and rows[i + 1].role == "assistant":
            hi = i + 1
        elif rows[i].role == "assistant" and i > 0 and rows[i - 1].role == "user":
            lo = i - 1
        cost = sum(min(len(rows[j].text or ""), text_limit) for j in range(lo, hi + 1))
        if spans and used + cost > chars:
            break
        spans.append((lo, hi))
        used += cost
    merged = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return [{"when": _when(rows[lo].created_at),
             "messages": [{"from": _who(rows[j].role), "text": _snippet(rows[j].text, wanted, text_limit)}
                          for j in range(lo, hi + 1)]} for lo, hi in merged]


def search(query: str, rows: list, *, strict: bool = False) -> list:
    """Episodes of `rows` (objects with id, role, text, created_at in time order) about the query."""
    k, chars, text_limit = (EARLIER, EARLIER_CHARS, EARLIER_TEXT) if strict else (RECALL, RECALL_CHARS, RECALL_TEXT)
    picked, wanted = _pick(query, rows, strict=strict, k=k)
    return _episodes(rows, picked, wanted, chars, text_limit) if picked else []


def related(s, chat: str, text: str, before_id: int) -> list:
    """Old messages (older than the fresh part, which starts at `before_id`) the new message seems to speak about."""
    if not keys(text):
        return []
    return search(text, _rows(s, chat, before_id), strict=True)


def recall(chat: str, query: str, before_id=None) -> list:
    """The model asked to remember: the whole history of the chat before `before_id` is searched."""
    with SessionLocal() as s:
        rows = _rows(s, chat, before_id)
    return search(query, rows)


# ----------------------------------------------------------------------------- the retelling of the older part
SUMMARY_PROMPT = (
    "Ты ведёшь краткую память разговора ИИ-сотрудника компании с одним человеком в рабочем мессенджере. Тебе дают "
    "прежнюю память (может быть пустой) и следующие сообщения разговора по порядку, с датами. Верни обновлённую "
    "память: сжатый пересказ всего разговора до 1500 символов, по-русски, без вступлений. Сохрани всё, что может "
    "понадобиться потом: договорённости и решения, обещания и поручения (кто, что, к какому сроку), даты, суммы, "
    "номера, названия документов, объектов и клиентов, о чём человек просил и что было сделано, открытые вопросы. "
    "Указывай даты, когда это было сказано. Приветствия, благодарности и мелочи опусти; старое сжимай сильнее нового, "
    "но не теряй договорённостей. Только то, что есть в сообщениях, ничего не додумывай. Не давай оценок людям и не "
    'сохраняй сведения о здоровье, религии, политике и подобном. Ответь JSON: {"summary": "..."}')


def _summarize(previous: str, rows: list) -> str:
    lines = [{"when": _when(r.created_at), "from": "человек" if r.role == "user" else "ИИ-сотрудник",
              "text": (r.text or "")[:1500]} for r in rows]
    raw = llm.chat(voice.model(), [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": "<data>" + json.dumps({"memory": previous or "", "messages": lines},
                                                          ensure_ascii=False) + "</data>"}],
        json_mode=True, temperature=0.2, max_tokens=1200, timeout=90)
    data = llm.parse_json(raw)
    summary = str(data.get("summary") or "").strip() if isinstance(data, dict) else ""
    if not summary:
        raise llm.LLMError("модель не вернула пересказ")
    return summary[:MAX_SUMMARY]


def _save(chat: str, **values) -> None:
    with SessionLocal() as s:
        stored = s.get(DialogMemory, chat) or DialogMemory(chat_id=chat, summary="", upto_id=0, messages=0)
        for name, value in values.items():
            setattr(stored, name, value)
        s.merge(stored)
        s.commit()


def refresh(chat: str) -> bool:
    """Folds the oldest messages outside the fresh part into the retelling once MAX_FRESH have piled up after it (FRESH
    stay word for word). True when the retelling changed. A failure is noted and tried again after the next reply."""
    if not config.llm_enabled():
        return False
    with _busy_lock:
        if chat in _busy:
            return False
        _busy.add(chat)
    changed = False
    try:
        for _ in range(MAX_CHUNKS):
            with SessionLocal() as s:
                stored = s.get(DialogMemory, chat)
                upto, summary, covered = ((stored.upto_id or 0, stored.summary or "", stored.messages or 0) if stored
                                          else (0, "", 0))
                waiting = _talk(s, chat).filter(ConversationMessage.id > upto).count()
                if waiting < MAX_FRESH:
                    break
                rows = (s.query(ConversationMessage.id, ConversationMessage.role, ConversationMessage.text,
                                ConversationMessage.created_at)
                        .filter(ConversationMessage.chat_id == chat, ConversationMessage.role.in_(ROLES),
                                ConversationMessage.status.in_(STATUSES), ConversationMessage.id > upto)
                        .order_by(ConversationMessage.id).limit(min(CHUNK, waiting - FRESH)).all())
            try:
                summary = _summarize(summary, rows)
            except Exception as exc:  # noqa: BLE001  the retelling waits; the conversation goes on without it
                log.warning("conversation %s: no retelling yet: %s", chat, exc)
                _save(chat, error=str(exc)[:300])
                break
            _save(chat, summary=summary, upto_id=rows[-1].id, messages=covered + len(rows), error="")
            changed = True
    finally:
        with _busy_lock:
            _busy.discard(chat)
    return changed


def _refresh_quietly(chat: str) -> None:
    try:
        refresh(chat)
    except Exception:  # noqa: BLE001
        log.exception("conversation %s: retelling failed", chat)


def schedule(chat: str) -> None:
    """After a reply: the retelling is brought up to date in the background (at once in tests)."""
    if config.TESTING:
        _refresh_quietly(chat)
    else:
        POOL.submit(_refresh_quietly, chat)


# ----------------------------------------------------------------------------- the panel
def view(s, chats) -> list:
    out = []
    for chat in sorted({c for c in chats if c}):
        stored = s.get(DialogMemory, chat)
        if stored is not None and (stored.summary or "").strip():
            out.append({"summary": stored.summary, "messages": stored.messages or 0, "updated_at": stored.updated_at})
    return out


def forget(s, chats) -> None:
    """The retelling is erased; the messages it covered are not retold again (only newer ones will be)."""
    for chat in {c for c in chats if c}:
        stored = s.get(DialogMemory, chat)
        if stored is not None:
            stored.summary = ""
