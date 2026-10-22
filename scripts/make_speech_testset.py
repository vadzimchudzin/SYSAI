"""Builds a small synthetic test set of Russian «meetings» for the speech benchmark (needs `pip install gTTS`).

    python3 scripts/make_speech_testset.py /tmp/speech_set

Each meeting is several speakers (one synthetic voice, pitch-shifted per person), pauses between turns, room noise
and a little echo, so recognition and speaker naming have something real to work on. The reference text and who
said what are saved next to the audio. Real recordings of the company are the better benchmark: put an audio file and a
`<name>.json` with {"turns": [{"speaker": "...", "text": "..."}]} into the same folder.
"""
import json
import pathlib
import subprocess
import sys
import tempfile

VOICES = {"Вадим Чудин": 0.70, "Анна Смирнова": 1.05, "Ольга Кузнецова": 1.2, "Игорь Соколов": 0.8, "Пётр Иванов": 0.92}

MEETINGS = [
    [("Вадим Чудин", "Доброе утро, коллеги. Это Вадим, начинаем планёрку. Анна, что у нас с актом сверки с ВентСнабом?"),
     ("Анна Смирнова", "Акт сверки я отправила в пятницу. По нему наша задолженность двенадцать тысяч рублей."),
     ("Вадим Чудин", "Хорошо. Ольга, когда придут вентиляторы на Галерею Борисов?"),
     ("Ольга Кузнецова", "ВентСнаб обещал отгрузить в среду, я проверю накладные и напишу в Битрикс."),
     ("Вадим Чудин", "Игорь, монтаж успеваете закончить к пятнадцатому декабря?"),
     ("Игорь Соколов", "Успеваем, если вентиляторы придут в среду. Бригада уже на объекте.")],
    [("Пётр Иванов", "Меня зовут Пётр, я по претензии к СтройМонтажу. Вадим, можно коротко?"),
     ("Вадим Чудин", "Да, Пётр, слушаю тебя."),
     ("Пётр Иванов", "СтройМонтаж просрочил оплату на сорок дней. Я подготовил претензию, нужна ваша подпись."),
     ("Вадим Чудин", "Подпишу сегодня. Анна, проверь, пожалуйста, сумму долга по СтройМонтажу."),
     ("Анна Смирнова", "Проверю и пришлю Петру до обеда.")],
    [("Ольга Кузнецова", "Это Ольга. Игорь, тебе на объект нужны фильтры?"),
     ("Игорь Соколов", "Да, Ольга, нужно двадцать фильтров и два вентилятора."),
     ("Ольга Кузнецова", "Закажу у ВентСнаба, заявку оформлю в Битриксе сегодня."),
     ("Игорь Соколов", "Спасибо. И акт КС-2 по второму этапу я передам Анне в четверг."),
     ("Ольга Кузнецова", "Анна просила присылать акты только в PDF, не забудь.")],
    [("Вадим Чудин", "Коллеги, коротко по Галерее Борисов. Пётр, договор с заказчиком подписан?"),
     ("Пётр Иванов", "Подписан вчера, оригинал отправили курьером, будет в пятницу."),
     ("Вадим Чудин", "Отлично. Игорь, а что с исполнительной документацией?"),
     ("Игорь Соколов", "Исполнительную сдадим вместе с актом КС-2 в конце месяца."),
     ("Вадим Чудин", "Хорошо, держи меня в курсе.")],
    [("Анна Смирнова", "Добрый день, это Анна из бухгалтерии. Ольга, счёт от ВентСнаба на какую сумму?"),
     ("Ольга Кузнецова", "Счёт на девятьсот восемьдесят рублей, оплатить нужно до четверга."),
     ("Анна Смирнова", "Платежи свыше пяти тысяч согласует Вадим, а этот я оплачу сама."),
     ("Ольга Кузнецова", "Спасибо, Анна. Тогда я сообщу ВентСнабу, что оплата будет завтра.")],
    [("Игорь Соколов", "Игорь на связи, я с объекта. Пётр, заказчик спрашивает про допсоглашение."),
     ("Пётр Иванов", "Игорь, допсоглашение будет готово в понедельник, я пришлю его Вадиму на подпись."),
     ("Игорь Соколов", "Понял. Тогда монтаж второго этажа начинаем со вторника."),
     ("Пётр Иванов", "Да, и попроси заказчика подписать акт скрытых работ.")],
]


def tts(text: str, path: pathlib.Path):
    from gtts import gTTS
    gTTS(text, lang="ru").save(str(path))


def main(out: str):
    out_dir = pathlib.Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix="speech_set_"))
    for number, turns in enumerate(MEETINGS, start=1):
        parts = []
        for i, (speaker, text) in enumerate(turns):
            raw, voiced = work / f"m{number}_{i}.mp3", work / f"m{number}_{i}.wav"
            tts(text, raw)
            factor = VOICES[speaker]
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-af",
                            f"asetrate=24000*{factor},aresample=24000,atempo={1 / factor:.4f},apad=pad_dur=0.5",
                            "-ar", "24000", "-ac", "1", str(voiced)], check=True)
            parts.append(voiced)
        listing = work / f"m{number}.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts))
        clean = work / f"m{number}_clean.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing), str(clean)],
                       check=True)
        target = out_dir / f"meeting_{number}.mp3"
        # room: pink noise about 18 dB below speech, a little echo, a low hum
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(clean), "-f", "lavfi", "-i",
                        "anoisesrc=color=pink:amplitude=0.06:sample_rate=24000", "-f", "lavfi", "-i",
                        "sine=frequency=50:sample_rate=24000", "-filter_complex",
                        "[0]aecho=0.8:0.5:40:0.25,volume=0.7[s];[2]volume=0.03[h];[s][1][h]amix=inputs=3:duration=first:normalize=0",
                        "-ac", "1", "-b:a", "64k", str(target)], check=True)
        (out_dir / f"meeting_{number}.json").write_text(json.dumps(
            {"turns": [{"speaker": s, "text": t} for s, t in turns]}, ensure_ascii=False, indent=1))
        print("ready", target)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/speech_set")
