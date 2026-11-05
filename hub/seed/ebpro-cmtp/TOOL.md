---
name: ebpro-cmtp
kind: mcp
description: Читает и собирает проекты Weintek EasyBuilder Pro (.cmtp/.emtp) — сведения, тексты и имена окон, встроенные картинки, расшифровка и сборка, правка подписей по примеру.
version: 1.0.0
run: python server.py
platform: any
tools: cmtp_info, cmtp_labels, cmtp_extract_images, cmtp_unpack, cmtp_pack, cmtp_relabel
requires: python>=3.11
---
# EasyBuilder Pro: проекты .cmtp / .emtp

Формат разобран по EasyBuilder Pro 6.10.02.300 (подпись WEINTEK LABS, INC.). Контейнер:
`u32 revision` + версия (строка MFC) + второе слово revision; у новых версий (revision ≥ 240)
дальше `u32 длина` и тело в AES-128-ECB, у старых — открытым текстом. Пересборка байт-в-байт
проверена на всех 48 проектах из комплекта EasyBuilder.

## Когда применять

- Разобрать присланный пример проекта: из чего он, какие окна, какие картинки.
- Пополнить память примеров: вынуть картинки и тексты в хранилище.
- Собрать проект по образцу: взять пример как шаблон, поменять подписи и имена окон.

## Как запускать

Синхронизатор ставит папку в `%SYSAI_HOME%\tools\ebpro-cmtp` и запускает `python server.py` на своём Python
(нужна только cryptography — она у синхронизатора уже есть, ничего не скачивается). Доступ к папкам проверяет SYSAI.

Порядок разбора примера: `cmtp_info` → `cmtp_labels` → `cmtp_extract_images`.
Сборка по образцу: `cmtp_relabel` (замена подписи той же длины) или `cmtp_unpack` → правка → `cmtp_pack`
(revision и version — из `cmtp_info` исходника).

## Ошибки

- «payload not 16-byte aligned» / «truncated payload» — файл повреждён или это не проект EasyBuilder.
- `cmtp_relabel` требует новый текст той же длины, что старый (длины хранятся внутри — так файл остаётся целым).
- Новые виджеты с нуля пока не ставятся: внутренний формат объектов разбирается (следующий этап).
