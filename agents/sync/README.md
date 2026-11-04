# sysai-agent — синхронизатор агента SYSAI на ПК

ОПО-6.3 (docs/opo/TASKS.md). Ставится установщиком (`agents/installer/install.ps1`) как `uv tool`: SYSAI собирает
колесо из этой папки сам — `GET /agents/api/sync/sysai_agent-<версия>-py3-none-any.whl` (`app/net/installer.py`).
Версия — `sysai_agent/__init__.py`; поменяли код — поднимите версию, иначе ПК не узнают, что есть новая.

## Команды

| Команда | Что делает |
|---|---|
| `sysai-agent connect --server https://… --code КОД` | меняет одноразовый код на токен агента, запоминает ключ подписи доступов |
| `sysai-agent sync [--hermes-home ПАПКА] [--no-restart]` | синхронизировать сейчас: доступы, настройки Hermes, `SOUL.md`, хаб, предложения, пульс |
| `sysai-agent run` | в фоне: пульс раз в 5 минут, синхронизация, когда на сервере что-то сменилось (и раз в час) |
| `sysai-agentw run` | то же без окна — так его запускает Windows при входе |
| `sysai-agent status [--json]` | что на этом ПК: агент, сервер, ревизии, последняя синхронизация и ошибки |
| `sysai-agent stop` | остановить фоновую синхронизацию (до следующего входа в Windows) |
| `sysai-agent mcp ИМЯ` | MCP-сервер из хаба под охраной доступов — так его запускает Hermes |

## Где что лежит

`%LOCALAPPDATA%\SYSAI` (не Windows — `~/.sysai`, или `SYSAI_HOME`):

- `agent.json` — агент, сервер, открытый ключ подписи, профиль и папка Hermes; `state.json` — ревизии, что
  применено, отправленные предложения; `access.json` — подписанный документ доступов для MCP-обёрток;
- `hub\` — навыки SYSAI_Hub, `tools\` — инструменты (только для чтения; у инструмента с `requirements.txt` — своё
  окружение в `venvs\<имя>`), `proposals\` — навыки, которые агент предлагает в хаб;
- `logs\sync.log` — журнал; `run.lock` — фоновая синхронизация работает; файл `stop` — просьба остановиться.

Токен — в Windows Credential Manager («SYSAI agent»). Нет хранилища — файл `token`, читать его может только
пользователь. `SYSAI_AGENT_TOKEN` в окружении важнее обоих (для проверок).

## Что меняется в профиле Hermes

Только ключи SYSAI в `config.yaml`: `model`, `fallback_providers`, `delegation`, `approvals`, `memory`,
`mcp_servers`, `providers.sysai`, `auxiliary.vision` и `compression`, `skills.external_dirs` и `create_dir`. Остальное
— как у человека. Перед первой заменой — копии `config.before-sysai.yaml` и `SOUL.before-sysai.md`. Токен — в `.env`
профиля (`SYSAI_AGENT_TOKEN`), в `config.yaml` — только `${SYSAI_AGENT_TOKEN}`. Шлюз Hermes запущен
(`gateway.pid`) — `hermes -p <профиль> gateway restart`; разговор в терминале подхватит новое в новом сеансе.

## Проверки

`tests/test_net_sync.py`: синхронизатор против самого сервера SYSAI (FastAPI в тестах) — подключение, подпись,
слияние настроек Hermes, хаб (только чтение, sha256, удаление), предложения, MCP-обёртка, фоновый цикл, колесо и
установщик. Разработка: `pip install -e agents/sync`.
