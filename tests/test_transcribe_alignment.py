"""STT without timestamps: the audio model's speakers are laid over the exact STT words, then sentence by sentence,
and only when both fail the part stays one replica."""
import json

from app import llm, transcribe


def part(tmp_path):
    path = tmp_path / "part.mp3"
    path.write_bytes(b"audio")
    return path


def test_small_differences_in_the_model_wording_keep_the_split(monkeypatch, tmp_path):
    text = "Добрый день, коллеги. Начинаем планёрку. Анна, что с актом сверки? Акт подписан, отправила 15 марта."
    model = {"segments": [{"speaker": "Спикер 1", "start": 0.0, "text": "Добрый день коллеги начинаем планерку Анна что с актом сверки"},
                          {"speaker": "Спикер 2", "start": 6.5, "text": "Акт подписан отправила пятнадцатого марта"}]}
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(model, ensure_ascii=False))
    result = transcribe._align_plain_transcript(part(tmp_path), text, {}, 12.0, "")
    assert result["segments"] == [
        {"speaker": "Спикер 1", "start": 0.0, "text": "Добрый день, коллеги. Начинаем планёрку. Анна, что с актом сверки?"},
        {"speaker": "Спикер 2", "start": 6.5, "text": "Акт подписан, отправила 15 марта."}]


def test_the_exact_words_of_the_model_are_taken_as_they_are(monkeypatch, tmp_path):
    text = "Начинаем. Да, слушаю."
    model = {"segments": [{"speaker": "Спикер 1", "start": 0.0, "text": "Начинаем."},
                          {"speaker": "Спикер 2", "start": 1.2, "text": "Да, слушаю."}]}
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(model, ensure_ascii=False))
    assert transcribe._align_plain_transcript(part(tmp_path), text, {}, 3.0, "")["segments"] == model["segments"]


def test_a_rewritten_text_falls_back_to_the_speaker_of_each_sentence(monkeypatch, tmp_path):
    text = "Начинаем планёрку по объекту. Акт подписан вчера вечером. Да. Отправила его в бухгалтерию."
    answers = iter([json.dumps({"segments": [{"speaker": "Спикер 1", "start": 0, "text": "совсем другой пересказ разговора"}]}),
                    json.dumps({"speakers": [{"index": 0, "speaker": "Спикер 1"}, {"index": 1, "speaker": "Спикер 2"},
                                             {"index": 2, "speaker": "Спикер 1"}, {"index": 3, "speaker": "Спикер 2"}]})])
    asked = []

    def chat(model, messages, **kwargs):
        asked.append(messages[0]["content"][0]["text"])
        return next(answers)
    monkeypatch.setattr(llm, "chat", chat)
    result = transcribe._align_plain_transcript(part(tmp_path), text, {}, 20.0, "")
    # «Да.» is a sentence of its own: the model gives it the voice it hears, not the speaker before it
    assert [(s["speaker"], s["text"]) for s in result["segments"]] == [
        ("Спикер 1", "Начинаем планёрку по объекту."), ("Спикер 2", "Акт подписан вчера вечером."),
        ("Спикер 1", "Да."), ("Спикер 2", "Отправила его в бухгалтерию.")]
    assert 0 < result["segments"][1]["start"] < 20 and len(asked) == 2
    assert "одно слово" in asked[0] and "overlap" in asked[0] and "end" in asked[0]


def test_only_when_everything_fails_the_part_is_one_replica(monkeypatch, tmp_path):
    def chat(*args, **kwargs):
        raise llm.LLMError("нет связи")
    monkeypatch.setattr(llm, "chat", chat)
    text = "Начинаем планёрку. Акт подписан."
    assert transcribe._align_plain_transcript(part(tmp_path), text, {}, 5.0, "")["segments"] == [
        {"speaker": "Спикер 1", "start": 0.0, "text": text}]


def test_the_projection_needs_most_words_and_keeps_times_in_order():
    text = "раз два три четыре пять шесть семь восемь"
    assert transcribe._project(text, [{"speaker": "Спикер 1", "start": 0, "text": "раз два девять"}], 8.0) is None
    pieces = transcribe._project(text, [{"speaker": "Спикер 1", "start": 5, "text": "раз два три"},
                                        {"speaker": "Спикер 2", "start": 3, "text": "четыре пять"},
                                        {"speaker": "", "start": 99, "text": "шесть семь восемь"}], 8.0)
    assert [(p["speaker"], p["start"], p["text"]) for p in pieces] == [
        ("Спикер 1", 5.0, "раз два три"), ("Спикер 2", 5.0, "четыре пять"), ("Спикер 1", 8.0, "шесть семь восемь")]
