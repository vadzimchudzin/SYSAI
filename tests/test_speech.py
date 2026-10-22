"""Sprint 5: clear speech — clean-up, hints, name correction, speaker naming, WER, natural style."""
import json

import pytest

from app import agent, asr_eval, audio, cases, llm, settings_store, speakers, speech_fix, transcribe, voice
from app.db import Employee, SessionLocal

VOCAB = ["ВентСнаб", "Вадим", "Чудин", "Ольга", "Кузнецова", "Bitrix24", "Игорь", "Соколов", "СтройМонтаж", "Анна",
         "Пётр", "Иванов"]


# ----------------------------------------------------------------------------- name and term correction
@pytest.mark.parametrize("heard, right", [
    ("акт для винсунаба", "акт для ВентСнаба"), ("передай Вадему", "передай Вадиму"),
    ("Игорю Саколову", "Игорю Соколову"), ("претензия к Строймантажу", "претензия к СтройМонтажу"),
    ("отчёт в битрикс", "отчёт в Bitrix24"), ("Чюдина", "Чудина"), ("Олге", "Ольге"),
])
def test_misheard_names_and_terms_are_corrected_with_their_endings(heard, right):
    assert speech_fix.fix_text(heard, VOCAB)[0] == right


@pytest.mark.parametrize("text", ["с Ольгой Кузнецовой", "Петру Иванову", "Ивановой", "вентиляция и винты",
                                  "давайте начнём", "анализ данных", "олигарх", "игорный бизнес", "Аннушка"])
def test_correct_forms_and_ordinary_words_are_left_alone(text):
    assert speech_fix.fix_text(text, VOCAB)[0] == text


def test_vocabulary_comes_from_the_directory_and_the_glossary():
    with SessionLocal() as s:
        s.add(Employee(name="Ольга Кузнецова", aliases="Оля", telegram_chat_id="@olga"))
        s.commit()
    settings_store.set_many({"glossary": "ВентСнаб, КС-2, Bitrix24, вентиляция"})
    vocab = speech_fix.vocabulary()
    assert {"Ольга", "Кузнецова", "ВентСнаб", "Bitrix24"} <= set(vocab) and "вентиляция" not in vocab
    assert "Ольга" in speech_fix.prompt_text()


def test_model_replacements_are_checked_by_code(monkeypatch):
    proposed = {"replacements": [{"from": "винсунаба", "to": "ВентСнаба"},       # sounds alike, in the vocabulary
                                 {"from": "договор", "to": "ВентСнаб"},          # does not sound alike
                                 {"from": "Петров", "to": "Сидоров"},            # not in the vocabulary
                                 {"from": "строй монтаж", "to": "СтройМонтаж"},  # two words
                                 {"from": "нетслова", "to": "Вадим"}]}           # not in the text
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(proposed, ensure_ascii=False))
    text = "договор с винсунаба подписал Петров, строй монтаж"
    assert speech_fix.model_replacements(text, VOCAB) == [("винсунаба", "ВентСнаба")]


def test_correction_can_be_switched_off():
    settings_store.set_many({"speech_fix": False, "glossary": "ВентСнаб"})
    segments = [{"text": "для винсунаба"}]
    assert speech_fix.fix_segments(segments)[0] == segments


# ----------------------------------------------------------------------------- recognition pipeline
def test_audio_cleanup_filter_is_applied_and_can_be_off(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        (tmp_path / "out.mp3").write_bytes(b"x")
    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    audio.extract("in.wav", tmp_path / "out.mp3", 0, 5)
    assert "-af" in calls[-1] and audio.CLEANUP in calls[-1]
    settings_store.set_many({"audio_cleanup": False})
    audio.extract("in.wav", tmp_path / "out.mp3", 0, 5)
    assert "-af" not in calls[-1] and "-ar" in calls[-1]


def test_stt_gets_the_vocabulary_and_the_transcript_is_corrected(monkeypatch, tmp_path):
    with SessionLocal() as s:
        s.add(Employee(name="Вадим Чудин", telegram_chat_id="@boss"))
        s.commit()
    settings_store.set_many({"glossary": "ВентСнаб"})
    seen = {}

    def fake_stt(data, model, fmt, language, timeout=60, prompt=""):
        seen["prompt"] = prompt
        return {"text": "Вадем подписал акт для винсунаба", "segments": [
            {"start": 0.0, "end": 3.0, "text": "Вадем подписал акт для винсунаба", "speaker": "A"}]}
    monkeypatch.setattr(transcribe.config, "llm_enabled", lambda: True)
    monkeypatch.setattr(llm, "transcribe_audio", fake_stt)
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps({"replacements": []}))
    monkeypatch.setattr(audio, "duration", lambda p: 3.0)
    monkeypatch.setattr(audio, "extract", lambda src, dst, start, sec, cleanup=None: open(dst, "wb").write(b"x"))
    src = tmp_path / "m.mp3"
    src.write_bytes(b"audio")
    _, segments = transcribe.transcribe(str(src), dict(settings_store.all_settings(), transcribe_model="openai/gpt-4o-transcribe"))
    assert "ВентСнаб" in seen["prompt"] and "Вадим" in seen["prompt"]
    assert segments[0]["text"] == "Вадим подписал акт для ВентСнаба"


def test_any_gemini_model_hears_words_and_voices_in_one_pass():
    assert transcribe.chat_model("google/gemini-2.5-pro") and transcribe.chat_model("google/gemini-2.5-flash")
    assert not transcribe.chat_model("openai/gpt-4o-transcribe")


def test_telegram_voice_goes_through_the_same_improvements(monkeypatch):
    with SessionLocal() as s:
        s.add(Employee(name="Игорь Соколов", telegram_chat_id="@igor"))
        s.commit()
    settings_store.set_many({"glossary": "СтройМонтаж", "transcribe_model": "openai/gpt-4o-transcribe"})
    seen = {}
    monkeypatch.setattr(transcribe.config, "llm_enabled", lambda: True)
    monkeypatch.setattr(audio, "duration", lambda p: 4.0)
    monkeypatch.setattr(audio, "extract", lambda src, dst, start, sec, cleanup=None: open(dst, "wb").write(b"x"))

    def fake_stt(data, model, fmt, language, timeout=60, prompt=""):
        seen["prompt"] = prompt
        return {"text": "Передай Игарю что Строймантаж заплатил"}
    monkeypatch.setattr(llm, "transcribe_audio", fake_stt)
    assert transcribe.voice_text(b"ogg", "audio/ogg", 4) == "Передай Игорю что СтройМонтаж заплатил"
    assert "СтройМонтаж" in seen["prompt"]


# ----------------------------------------------------------------------------- speakers
MEETINGS = [
    [("Вадим Чудин", "Доброе утро, коллеги. Это Вадим, начинаем планёрку. Анна, что у нас с актом сверки?"),
     ("Анна Смирнова", "Акт сверки я отправила в пятницу."),
     ("Вадим Чудин", "Хорошо. Ольга, когда придут вентиляторы?"),
     ("Ольга Кузнецова", "ВентСнаб обещал отгрузить в среду."),
     ("Вадим Чудин", "Игорь, монтаж успеваете закончить?"),
     ("Игорь Соколов", "Успеваем, бригада уже на объекте.")],
    [("Пётр Иванов", "Меня зовут Пётр, я по претензии к СтройМонтажу. Вадим, можно коротко?"),
     ("Вадим Чудин", "Да, Пётр, слушаю тебя."),
     ("Пётр Иванов", "Нужна ваша подпись под претензией."),
     ("Вадим Чудин", "Подпишу сегодня. Анна, проверь сумму долга."),
     ("Анна Смирнова", "Проверю и пришлю Петру до обеда.")],
    [("Ольга Кузнецова", "Это Ольга. Игорь, тебе на объект нужны фильтры?"),
     ("Игорь Соколов", "Да, Ольга, нужно двадцать фильтров."),
     ("Ольга Кузнецова", "Закажу у ВентСнаба сегодня.")],
    [("Игорь Соколов", "Игорь на связи, я с объекта. Пётр, заказчик спрашивает про допсоглашение."),
     ("Пётр Иванов", "Игорь, допсоглашение будет готово в понедельник."),
     ("Игорь Соколов", "Понял, тогда монтаж начинаем со вторника.")],
]


@pytest.fixture
def staff():
    with SessionLocal() as s:
        for name, alias in (("Вадим Чудин", "Вадим"), ("Анна Смирнова", "Аня"), ("Ольга Кузнецова", "Оля"),
                            ("Игорь Соколов", ""), ("Пётр Иванов", "Петя")):
            s.add(Employee(name=name, aliases=alias, telegram_chat_id=""))
        s.commit()
        return s.query(Employee).all()


def _anonymous(turns):
    labels = {}
    return [{"speaker": labels.setdefault(p, f"Спикер {len(labels) + 1}"), "text": t} for p, t in turns], labels


def test_speakers_are_named_from_introductions_and_answers(staff):
    named = total = 0
    for turns in MEETINGS:
        segments, labels = _anonymous(turns)
        _, mapping = speakers.name_speakers(segments, staff, use_model=False)
        for person, label in labels.items():
            total += 1
            named += mapping.get(label) == person
    assert named == total  # every speaker here has introduced themselves or answered a call by name


def test_nobody_is_named_without_evidence(staff):
    segments = [{"speaker": "Спикер 1", "text": "Коллеги, коротко по объекту."},
                {"speaker": "Спикер 2", "text": "Исполнительную сдадим в конце месяца."}]
    assert speakers.name_speakers(segments, staff, use_model=False)[1] == {}


def test_calling_somebody_is_not_proof_of_being_them(staff, monkeypatch):
    segments = [{"speaker": "Спикер 1", "text": "Пётр, договор с заказчиком подписан?"},
                {"speaker": "Спикер 1", "text": "Подписан вчера."}]  # the voices were merged by recognition
    petr = next(e for e in staff if e.name == "Пётр Иванов")
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(
        {"speakers": [{"label": "Спикер 1", "employee_id": petr.id, "quote": "Пётр, договор с заказчиком подписан?"}]},
        ensure_ascii=False))
    assert speakers.name_speakers(segments, staff)[1] == {}


def test_model_proposal_with_a_real_introduction_is_accepted(staff, monkeypatch):
    segments = [{"speaker": "Спикер 1", "text": "Добрый день, это Анна из бухгалтерии."},
                {"speaker": "Спикер 2", "text": "Слушаю."}]
    anna = next(e for e in staff if e.name == "Анна Смирнова")
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(
        {"speakers": [{"label": "Спикер 1", "employee_id": anna.id, "quote": "это Анна из бухгалтерии"}]}))
    assert speakers.name_speakers(segments, staff)[1] == {"Спикер 1": "Анна Смирнова"}


def test_names_from_voice_profiles_are_kept_and_not_given_twice(staff):
    segments = [{"speaker": "Вадим Чудин", "text": "Анна, что с актом?"},
                {"speaker": "Спикер 2", "text": "Отправила."},
                {"speaker": "Спикер 3", "text": "Это Вадим, я тоже здесь."}]
    named, mapping = speakers.name_speakers(segments, staff, use_model=False)
    assert mapping == {"Спикер 2": "Анна Смирнова"}  # Вадим is already the voice-profile label
    assert named[0]["speaker"] == "Вадим Чудин"


def test_the_meeting_pipeline_names_speakers(staff, monkeypatch):
    from app import pipeline
    segments, _ = _anonymous(MEETINGS[2])
    assert {s["speaker"] for s in pipeline._name_speakers(segments)} == {"Ольга Кузнецова", "Игорь Соколов"}
    settings_store.set_many({"speaker_naming": False})
    assert {s["speaker"] for s in pipeline._name_speakers(segments)} == {"Спикер 1", "Спикер 2"}


# ----------------------------------------------------------------------------- WER
def test_wer_normalises_case_punctuation_and_numbers():
    assert asr_eval.normalize("Задолженность — 12 000 рублей!") == ["задолженность", "12000", "рублей"]
    assert asr_eval.normalize("двенадцать тысяч рублей") == ["12000", "рублей"]
    assert asr_eval.normalize("девятьсот восемьдесят") == ["980"]
    assert asr_eval.wer("Анна отправила акт в пятницу", "анна отправила акт в пятницу.") == 0
    assert asr_eval.wer("Анна отправила акт в пятницу", "Анна отправила в пятницу") == pytest.approx(0.2)
    assert asr_eval.terms_heard("акт для ВентСнаба", "акт для винсунаба", ["ВентСнаб"]) == (0, 1)


# ----------------------------------------------------------------------------- natural style
@pytest.mark.parametrize("text, stiff", [("Доводим до вашего сведения, что данный акт подписан.", True),
                                         ("В целях ускорения просим вас прислать документ.", True),
                                         ("Акт подписан, пришлите, пожалуйста, оригинал до пятницы.", False),
                                         ("Привет! Посмотрел договор — всё ок.", False)])
def test_officialese_is_detected(text, stiff):
    assert bool(voice.officialese(text)) is stiff


def test_a_stiff_message_is_rewritten_once(monkeypatch):
    answers = iter([{"text": "Доводим до вашего сведения, что в рамках проекта акт подписан.", "covered_task_ids": []},
                    {"text": "Акт подписан, всё готово.", "covered_task_ids": []}])
    calls = []

    def fake(model, messages, **kw):
        calls.append(messages[1]["content"])
        return json.dumps(next(answers), ensure_ascii=False)
    monkeypatch.setattr(llm, "chat", fake)
    assert voice._ask("сис", {})["text"] == "Акт подписан, всё готово."
    assert "без канцелярита" in calls[1]


def test_a_stiff_message_still_goes_after_one_rewrite(monkeypatch):
    stiff = {"text": "Уведомляем, что акт подписан.", "covered_task_ids": []}
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(stiff, ensure_ascii=False))
    assert voice._ask("сис", {})["text"] == stiff["text"]  # style never blocks delivery


def test_case_messages_are_rewritten_without_officialese_and_know_the_history(monkeypatch):
    seen = []

    def fake(model, messages, **kw):
        seen.append(messages[1]["content"])
        return json.dumps({"text": "Просим вас в кратчайшие сроки прислать акт." if len(seen) == 1 else
                           "Пришлите, пожалуйста, акт сегодня."}, ensure_ascii=False)
    monkeypatch.setattr(llm, "chat", fake)
    assert cases._write("request", {"task": {"title": "акт"}}) == "Пришлите, пожалуйста, акт сегодня."
    assert "Прошлый вариант" in seen[-1]


def test_style_guide_is_in_every_prompt():
    prompt = voice.system_prompt("задача")
    assert "Без канцелярита" in prompt and "Сначала суть" in prompt
