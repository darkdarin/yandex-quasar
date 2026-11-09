# AGENTS.md

Интеграция рутованной Яндекс Станции с Home Assistant: станция работает как Assist-сателлит и медиаплеер,
облако Яндекса заменено нашим сервером.

## Компоненты

- `adapter/` — **Quasar Adapter** (Python, asyncio/aiohttp). Эмулирует `quasar.yandex.net` и uniproxy для станции,
  отдаёт HA свой WebSocket API.
- `custom_components/yandex_quasar/` — **HA custom integration** (`assist_satellite`, `media_player`, `sensor`,
  `select`; позже `event`, `light`, `switch`).
- `station-agent/` — опциональный агент на станции (Go): кнопки, LED, мьют.
- `ha-addon/` — упаковка адаптера как HA OS add-on (app). `README.md` и `DOCS.md` там — для пользователей
  (страница app в магазине и вкладка «Документация»); сборка и релиз описаны ниже.
- `docker-compose.yml` — тот же образ app без Supervisor (HA в Docker): `run.sh` без `SUPERVISOR_TOKEN` берёт
  настройки из `QUASAR_*`, данные и seed-файлы — в томе `/data` (`/data/seed`).

## Документация (читать перед работой)

Документация разработки ведётся в **Notion** (workspace `darkdarin`, страница
[Yandex Quasar](https://app.notion.com/p/40de74ee720f805ab86ae8e7c52488f4)), читать и править её через Notion MCP. В репозитории, в `docs/`, только
документация для пользователя ([docs/README.md](docs/README.md), [docs/station-setup.md](docs/station-setup.md)).

- [Архитектура](https://app.notion.com/p/40de74ee720f81238003dda9c1c7b20d) — архитектурный план и ключевые решения
- [Протокол станции](https://app.notion.com/p/40de74ee720f81c2ae54c8a00a0abc54) — HTTP-бэкенд, uniproxy JSON/AAPI, директивы, glagol
- [Протокол Adapter ↔ HA](https://app.notion.com/p/40de74ee720f81478799c6df712ce45f)
- [Дизайн HA-интеграции](https://app.notion.com/p/40de74ee720f81e3b826cba549bea65d)
- [Заметки об устройствах](https://app.notion.com/p/40de74ee720f81a0b48bd8f68f731b56) — что подтверждено на реальных станциях
- [Станция: исследование и внутренности](https://app.notion.com/p/40de74ee720f81b6b1d0fca6026c86c6) — инвентаризация, DNS + CA, автозапуск агента
- [Roadmap](https://app.notion.com/p/40de74ee720f8148bcc4f0d15da8b248) — этапы, риски, открытые вопросы
- [Источники](https://app.notion.com/p/40de74ee720f81b1bf72d97348ac83d8)

## Репозиторий и релизы

- GitHub: `git@github.com:darkdarin/yandex-quasar.git` (push по SSH-ключу этого Mac, аккаунт `darkdarin`).
  В `gh` два аккаунта: глобально активен рабочий, а `.envrc` в корне (direnv, gitignored) задаёт `GH_TOKEN`
  аккаунта `darkdarin`. В неинтерактивном shell хук direnv не срабатывает: вызывать `direnv exec . gh …`
  (релизы, CI: `direnv exec . gh run list`). `gh auth switch` не трогать.
- Один репозиторий — два канала: HACS (`custom_components/`, `hacs.json`) и магазин HA apps (`repository.yaml` +
  `ha-addon/`). Опубликованный app — готовый образ `ghcr.io/darkdarin/quasar-adapter:<version>` (`image:` в
  `ha-addon/config.yaml`), его собирает и публикует `release.yml`.
- **Каждая новая версия выкатывается GitHub-релизом с changelog.** Порядок
  ([чек-лист ниже](#чек-лист-релиза)): поднять версию в 4 местах, `uv lock` в `adapter/`,
  дописать раздел `## <version>` в [ha-addon/CHANGELOG.md](ha-addon/CHANGELOG.md), коммит, тег `v<version>`, push
  ветки и тега. `.github/workflows/release.yml` собирает образ (amd64 и arm64), публикует его и создаёт релиз с
  текстом этого раздела; без раздела или при несовпадении версий он падает. Changelog пишется для пользователей, по-русски: что изменилось в работе, а не
  какие файлы тронуты. Его же HA показывает на вкладке «Журнал изменений» у app.
- Раздел версии в changelog состоит из `### Приложение` и `### Интеграция`. Если компонент не менялся, в его
  разделе пишется «Без изменений, обновлять не обязательно.» Этот же текст HACS показывает в обновлении интеграции,
  а HA — в обновлении app: пользователь видит два обновления с одной версией и должен понимать, какое из них
  настоящее. Интеграция называется «Яндекс Станция», app — «Quasar Adapter».
- Не пушить в `main` поднятую версию без тега и релиза в том же push: магазин увидит новую версию app и попробует
  скачать образ `:<version>`, которого ещё нет, — обновление упадёт. Dev-сборки на живой HA (локальный app) тоже требуют
  новой версии; их изменения копятся в разделе changelog этой версии до релиза.
- CI: `.github/workflows/adapter.yml` (pytest, пробная сборка образа amd64 и его запуск без Supervisor), `validate.yml` (hassfest, HACS, app
  lint), `release.yml` (образ и релиз по тегу).

### Сборка app

- `ha-addon/build.sh` собирает в `ha-addon/.build/quasar_adapter` контекст сборки: файлы app и исходники адаптера.
  Из него собирает образ CI, и он же — локальный app: в его `config.yaml` нет `image:`, поэтому Supervisor
  собирает образ сам.
- Dockerfile ставит зависимости строго по `uv.lock` и только с PyPI (базовый образ HA подмешивает свой индекс
  колёс), компилирует quasar-protos внутрь образа и проверяет импорт.

Установка и обновление локального app на живом HA:

```sh
STAGE=$(ha-addon/build.sh)
COPYFILE_DISABLE=1 tar -C "$(dirname "$STAGE")" -cf - quasar_adapter | ssh ha 'rm -rf /local_apps/quasar_adapter && tar -C /local_apps -xf -'
ssh ha 'ha store reload && ha apps install local_quasar_adapter'   # первый раз
ssh ha 'ha store reload && ha apps update local_quasar_adapter'    # после изменений (нужна новая версия в config.yaml)
ssh ha 'ha apps start local_quasar_adapter && ha apps logs local_quasar_adapter'
```

`COPYFILE_DISABLE=1` не пускает в архив macOS-файлы `._*`. Если `ha store reload` упал по таймауту, повторить:
Supervisor может быть занят предыдущей перезагрузкой. Если в рабочем дереве есть чужие незакоммиченные правки,
собирать из `git archive HEAD` во временную папку. Исходные файлы бэкенда для локального app лежат в
`/addon_configs/local_quasar_adapter/` на хосте. Локальный app и app из магазина делят порт 8080: перед установкой
  локального остановить app из магазина и перенести токен (`api_token`) и seed-файлы, потом вернуть обратно.

Проверка сборки под arm64 (как RPi 5):

```sh
cd "$(ha-addon/build.sh)" && docker build --platform linux/arm64 \
  --build-arg BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.14-alpine3.22 -t local/quasar_adapter:test .
```

### Чек-лист релиза

1. Поднять версию в `ha-addon/config.yaml`, `adapter/pyproject.toml`, `adapter/quasar_adapter/__init__.py`,
   `custom_components/yandex_quasar/manifest.json`, затем `cd adapter && uv lock`.
2. Дописать раздел `## <version>` в `ha-addon/CHANGELOG.md`.
3. Коммит, тег `v<version>`, push ветки и тега. Проверить, что `release.yml` прошёл:
   `direnv exec . gh run list --workflow release.yml`, и что образ скачивается без авторизации (пакет в ghcr
   должен быть публичным).

## Рабочее окружение

Разработка ведётся на **живом HA** владельца, отдельного тестового HA нет.

| Что | Где / как |
|---|---|
| Станция (Midi) | `ssh alice` (192.168.1.25:2222, root). `uniProxyUrl` → `ws://192.168.0.50:8080/uni.ws`, бэкап — `/data/quasar.cfg.orig` |
| Home Assistant OS (RPi 5) | `ssh ha` (192.168.0.50, Terminal & SSH add-on). API-токен — `main-ha.env` в корне (gitignored, в чат не выводить) |
| Адаптер | app из магазина `7391c79f_quasar_adapter`, seed-файлы — `/addon_configs/7391c79f_quasar_adapter/` (в SSH-аддоне `/app_configs/…`). Dev-сборка — локальный app (см. [«Сборка app»](#сборка-app)) |
| Интеграция | `tar -C custom_components -cf - yandex_quasar \| ssh ha 'tar -C /config/custom_components -xf -'`, затем `ssh ha 'ha core check && ha core restart'` |
| Отладка Assist | WS `assist_pipeline/pipeline_debug/list|get` на основном HA; пайплайн «OpenAI» (облачный STT + агент) |
| Тесты адаптера | `cd adapter && uv run pytest` |

Ограничения: HA на Raspberry Pi 5, поэтому тяжёлые локальные модели (Whisper turbo и т. п.) не предлагать.
Перезапуск HA и правки на станции касаются живого дома: делать осознанно и сообщать.

**Релизы и обновления — только с согласия владельца.** Перед каждым релизом (поднятие версии, тег, push тега)
спросить и дождаться «да». App и интеграцию на HA владелец обновляет сам: не запускать `ha apps update`, не копировать
интеграцию в `/config/custom_components`, не перезапускать HA ради деплоя. Чтение логов и состояния — можно.

## Правила

- Документация на русском, идентификаторы, код и комментарии в коде — на английском. Пользовательская
  документация (`README.md`, `docs/`, `ha-addon/DOCS.md`) описывает только установку и использование.
- Меняешь протокол (станция или Adapter ↔ HA) — обнови соответствующую страницу в Notion в том же изменении.
- Находки с реального устройства записывай в Notion, «Заметки об устройствах» (модель, прошивка, что подтверждено).
  Пометки **[проверить]** в доках снимай, только когда факт подтверждён.
- Не коммить и не вставлять в Notion секреты станции: `devices.json`, glagol-сертификаты, токены, записи трафика с ними.
- Интеграция не тянет внешних зависимостей (`requirements: []`). Общие схемы протокола копируются из адаптера.
- Код из quasard и quasar-server не копируем без проверки лицензии: протоколы описываем, код пишем свой.
