<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/darkdarin/yandex-quasar/main/custom_components/yandex_quasar/brand/dark_logo.png">
    <img alt="Yandex Quasar" src="https://raw.githubusercontent.com/darkdarin/yandex-quasar/main/custom_components/yandex_quasar/brand/logo.png" width="360">
  </picture>
</p>

# Yandex Quasar → Home Assistant

[![Release](https://img.shields.io/github/v/release/darkdarin/yandex-quasar?sort=semver)](https://github.com/darkdarin/yandex-quasar/releases)
[![Adapter](https://github.com/darkdarin/yandex-quasar/actions/workflows/adapter.yml/badge.svg)](https://github.com/darkdarin/yandex-quasar/actions/workflows/adapter.yml)
[![Validate](https://github.com/darkdarin/yandex-quasar/actions/workflows/validate.yml/badge.svg)](https://github.com/darkdarin/yandex-quasar/actions/workflows/validate.yml)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.10%2B-18BCF2?logo=homeassistant&logoColor=white)](https://www.home-assistant.io/)
![aarch64](https://img.shields.io/badge/aarch64-yes-green.svg)
![amd64](https://img.shields.io/badge/amd64-yes-green.svg)
[![License](https://img.shields.io/github/license/darkdarin/yandex-quasar)](LICENSE)

Превращает **рутованную Яндекс Станцию** в голосовой ассистент и колонку Home Assistant. Станция по-прежнему
слышит «Алиса» своими микрофонами и говорит своим динамиком, но понимает команды и отвечает ваш
**Assist** в HA. Облако Яндекса больше не нужно.

> Неофициальный проект, с Яндексом не связан. Нужен root-доступ к станции, всё делается на свой риск.

## Что получится

- **Голосовой ассистент.** «Алиса, включи свет» выполняет Home Assistant через выбранный Assist-пайплайн
  (облачный или локальный STT, любой агент, в том числе LLM). Можно продолжать диалог без повторного «Алиса».
- **Таймеры.** «Алиса, поставь таймер на 5 минут»: отсчёт идёт на часах станции, по окончании она звонит сама,
  «Алиса, стоп» выключает звонок.
- **Колонка.** Музыка из Music Assistant, громкость, пауза, объявления (`announce`) из автоматизаций.
- **Сущности в HA:** `assist_satellite`, `media_player`, `sensor` со статусом связи, `select` для выбора
  пайплайна и чувствительности конца фразы, переключатели обработки
  звука (см. «Настройки станции» в документации app).

## Что понадобится

- Яндекс Станция с root и доступом по SSH или ADB. Как получить root — [hackalice.me](https://hackalice.me/start/).
  Проверено на **Яндекс Станции Миди** (прошивка 0.369.1.50), другие модели не проверялись.
- Home Assistant с установленным [HACS](https://hacs.xyz/). На Home Assistant OS и Supervised адаптер ставится как
  app, а если Home Assistant работает в Docker — отдельным контейнером (шаг 1, вариант без Supervisor).
- Assist-пайплайн на русском: **Настройки → Голосовые ассистенты**. Подойдёт любой: облачный STT/TTS или Whisper/Piper.
- Станция и Home Assistant в одной локальной сети. Узнайте IP Home Assistant, он понадобится на шаге 3.

## Установка

### Шаг 1. Поставить app Quasar Adapter

Это сервер, к которому подключается станция вместо облака Яндекса.

[![Открыть app в Home Assistant](https://my.home-assistant.io/badges/supervisor_addon.svg)](https://my.home-assistant.io/redirect/supervisor_addon/?addon=7391c79f_quasar_adapter&repository_url=https%3A%2F%2Fgithub.com%2Fdarkdarin%2Fyandex-quasar)

1. Нажмите кнопку выше: Home Assistant предложит добавить репозиторий и откроет страницу app.
   Вручную: **Настройки → Приложения → Магазин приложений → ⋮ → Репозитории**, добавьте
   `https://github.com/darkdarin/yandex-quasar`, затем найдите в магазине **Quasar Adapter**.
2. Установите app и запустите его.
3. Откройте вкладку **Журнал** и скопируйте токен из строки `Generated new HA API token: …`. Он нужен на шаге 2.
   Если хотите задать свой токен, впишите его в опцию `api_token` на вкладке **Конфигурация** и перезапустите app.

#### Вариант без Supervisor (Home Assistant в Docker)

Если у вас нет раздела «Приложения» (Home Assistant установлен в Docker), запустите адаптер отдельным
контейнером из того же образа. Скачайте [docker-compose.yml](docker-compose.yml) на машину в той же сети, что
станция, и выполните:

```sh
docker compose up -d
cat data/api_token        # токен для шага 2
```

- Нужна сеть хоста (`network_mode: host` уже стоит в файле): станция подключается к адаптеру напрямую, а сам
  адаптер объявляет себя в сети для Home Assistant и Music Assistant.
- Настройки задаются переменными `QUASAR_*` в `docker-compose.yml`, их названия совпадают с опциями app
  (`QUASAR_MIC_GAIN_DB`, `QUASAR_API_TOKEN` и т. д.).
- Всё, что адаптер хранит, лежит в папке `data/` рядом с файлом: токен, список станций, файлы станции для полной
  отвязки (`data/seed/`, туда же их кладёт «Подключить станцию» на шаге 3).
- Если Home Assistant тоже работает в сети хоста, он найдёт адаптер сам. Иначе на шаге 2 добавьте интеграцию
  вручную: IP машины с адаптером и порт `8080`.
- На шаге 3 вместо IP Home Assistant везде указывайте IP машины, где запущен адаптер.
- Обновление: `docker compose pull && docker compose up -d`.

### Шаг 2. Поставить интеграцию

[![Открыть в HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=darkdarin&repository=yandex-quasar&category=integration)

1. Нажмите кнопку выше: HACS откроет страницу интеграции и предложит добавить репозиторий.
   Вручную: **HACS → ⋮ → Пользовательские репозитории**, добавьте `https://github.com/darkdarin/yandex-quasar`
   с типом **Integration** и найдите **Яндекс Станция**.
2. Нажмите **Скачать** и **перезапустите Home Assistant**.
3. **Настройки → Устройства и службы**: HA сам найдёт Quasar Adapter (блок «Обнаружено»). Нажмите
   **Настроить** и вставьте токен из шага 1. Если адаптер не нашёлся, нажмите кнопку ниже и укажите
   IP Home Assistant и порт `8080`.

   [![Добавить интеграцию](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=yandex_quasar)

Пока станция не подключена, устройств в интеграции не будет. Это нормально.

### Шаг 3. Перенаправить станцию на адаптер

**Проще всего — из Home Assistant.** **Настройки → Устройства и службы → Яндекс Станция → Quasar Adapter → Настроить →
Подключить станцию**. Укажите IP станции, способ подключения (SSH или ADB) и, если нужно, порт и пароль, оставьте
«Полностью отвязать от облака Яндекса». Адаптер сам сделает бэкапы на станции, заберёт нужные файлы, перенаправит
станцию на себя и дождётся её подключения. Если станция не подключится за 4 минуты, настройки на ней вернутся
обратно. Найденную в сети станцию Home Assistant предложит подключить сам. Пункт **Вернуть станцию к Яндексу**
там же откатывает всё обратно.

Проверено на Яндекс Станции Миди. Ниже — то же самое вручную.

Подключитесь к станции по SSH или ADB и выполните:

```sh
mount -o remount,rw /
cp /system/vendor/quasar/quasar.cfg /data/quasar.cfg.orig     # бэкап
vi /system/vendor/quasar/quasar.cfg                            # см. строку ниже
sync; mount -o remount,ro /
/system/vendor/quasar/q restart
```

В `quasar.cfg` найдите `uniProxyUrl` и замените значение на адрес вашего HA:

```json
"uniProxyUrl" : "ws://192.168.0.50:8080/uni.ws",
```

Через 1–2 минуты станция подключится. В HA появится устройство станции, сенсор «Статус» покажет «В сети».

Так голос идёт через HA, а настройки и обновления станция по-прежнему берёт из облака Яндекса. Чтобы
отключить облако совсем, сделайте шаг 3б. Его можно сделать сразу или позже.

### Шаг 3б (рекомендуется). Полностью отвязать станцию от облака Яндекса

Адаптер заменит все облачные сервисы, которые нужны станции: конфиг, авторизацию, проверку обновлений, метрики.
Станция перестанет обращаться к Яндексу, а обновления прошивки, которые могут отобрать root, к ней не придут.

> ⚠️ После отвязки станция пропадёт из приложения «Дом с Алисой», Яндекс Музыка на ней работать не будет.
> Делайте это на станции, которая уже активирована в облаке Яндекса: адаптер берёт её настройки из файлов ниже.

**1. Бэкап на станции.**

```sh
cp /system/vendor/quasar/quasar.cfg /data/quasar.cfg.uniproxy-only
cd /data/quasar && tar -cf /data/quasar-cloud-backup.tar account_storage.dat devices.json registered initialized data/user_config.dat
```

**2. Передать адаптеру настройки станции.** Скопируйте со станции на компьютер `/data/quasar/devices.json` и
`/data/quasar/data/user_config.dat`. Из `user_config.dat` достаньте JSON (перед ним в файле несколько байт
заголовка):

```sh
python3 -c "import json; d = open('user_config.dat', 'rb').read().decode('utf-8', 'replace'); \
cfg, _ = json.JSONDecoder().raw_decode(d[d.index('{'):]); json.dump(cfg, open('sync_info_base.json', 'w'), ensure_ascii=False)"
```

Положите `sync_info_base.json` и `devices.json` в папку конфигурации app на HA: `/addon_configs/7391c79f_quasar_adapter/`
(видна через app Terminal & SSH, Samba или Studio Code Server). Перезапустите app. Если адаптер запущен в Docker
без Supervisor, папка — `data/seed/` рядом с `docker-compose.yml`, а перезапуск — `docker compose restart`.

> 🔒 `devices.json` содержит ключи станции: никому его не отправляйте и не публикуйте.

**3. Переключить облачные адреса на адаптер.** Скопируйте `/system/vendor/quasar/quasar.cfg` на компьютер и
поменяйте в нём значения (`192.168.0.50` замените на IP вашего HA):

| Ключ в `quasar.cfg` | Новое значение |
|---|---|
| `aliced.uniProxyUrl` | `ws://192.168.0.50:8080/uni.ws` (уже сделано на шаге 3) |
| `common.backendUrl` | `http://192.168.0.50:8080/quasar` |
| `authd.passportUrl` | `http://192.168.0.50:8080/oauth` |
| `authd.codeUrl` | `http://192.168.0.50:8080/passport` |
| `authd.loginUrl` | `http://192.168.0.50:8080/login` |
| `arpc_client.server_url` | `http://192.168.0.50:8080/rpc` |
| `teleme3d.appMetricaStartupHost` | `http://192.168.0.50:8080/startup` |
| `teleme3d.appmetricaFallbackHosts` | `["http://192.168.0.50:8080/appmetrica"]` (это список) |
| `teleme3d.clickdaemon.endpointUri` | `http://192.168.0.50:8080/clck/quasar_metrics/write/batch` |
| `ntpd.timeOverHttpUrl` | `http://192.168.0.50:8080/scbh/generate_204` |
| `fluent-bitd.fluent-bit.enabled` | `false` (выключает отправку логов в Яндекс) |

Загрузите файл обратно и перезапустите станцию:

```sh
mount -o remount,rw /
cp /data/quasar.cfg.new /system/vendor/quasar/quasar.cfg      # ваш исправленный файл
sync; mount -o remount,ro /
/system/vendor/quasar/q restart
```

После перезапуска станция за ~20 секунд получит конфиг от адаптера и подключится, в журнале app появятся
запросы `get_sync_info`. Откат к шагу 3 (только голос через HA):
`cp /data/quasar.cfg.uniproxy-only /system/vendor/quasar/quasar.cfg` с теми же `mount`, `sync` и `q restart`.

Дополнительная защита от обновлений прошивки и подводные камни описаны в [docs/station-setup.md](docs/station-setup.md).

### Шаг 4. Выбрать пайплайн и проверить

1. Откройте устройство станции: **Настройки → Устройства и службы → Яндекс Станция**.
2. В списке **Ассистент** выберите свой русский Assist-пайплайн.
3. Скажите «Алиса, который час?». Станция должна ответить голосом из вашего TTS.

## Настройка

### Слово активации

На странице устройства станции откройте сущность голосового ассистента (`assist_satellite`) → ⚙ →
**Слово активации** и выберите «Алиса» или «Яндекс». Других слов станция не умеет.

### Music Assistant

Станция появляется в Music Assistant как плеер **Sendspin** сама, через mDNS. При первом подключении
Music Assistant спросит код сопряжения. Код придёт уведомлением в Home Assistant, введите его в Music Assistant.

Чтобы голосовые «Алиса, пауза» / «продолжи» управляли музыкой, станция и плеер Music Assistant должны
находиться в одной зоне (area) Home Assistant.

### Опции app

Меняются на вкладке **Конфигурация** app Quasar Adapter. Обычно трогать не нужно.

| Опция | Зачем |
|---|---|
| `port` | порт адаптера (по умолчанию `8080`); при смене поменяйте его и в `quasar.cfg` станции |
| `api_token` | свой токен для интеграции вместо сгенерированного |
| `mic_gain_db` | усиление микрофона станции: увеличьте, если STT плохо слышит. Для отдельной станции его можно выключить переключателем «Предусиление микрофона» на её устройстве |
| `speech_threshold_db` | уровень, с которого звук считается речью |
| `spotter_tail_ms` | сколько отрезать от хвоста «Алиса» в начале команды, если STT дописывает лишнее слово («Так,», «Всё,»). Для отдельной станции обрезка выключается переключателем «Отрезать конец слова активации» |
| `log_level` | подробность журнала (`debug` для поиска проблем) |

## Если что-то не работает

- **Станция не появляется в HA.** Проверьте журнал app: при подключении в нём появится строка
  `Station … connected`. Если её нет, проверьте IP и порт в `uniProxyUrl` и что станция видит HA по сети.
  После перезапуска app станция переподключается в течение 1–3 минут.
- **Станция слышит «Алиса», но не отвечает.** Проверьте, что выбран пайплайн (шаг 4), и посмотрите отладку
  пайплайна: **Настройки → Голосовые ассистенты → ⋮ → Отладка**.
- **Долгие ответы LLM.** Если ответ не пришёл за 55 секунд, станция проиграет короткий звук ошибки.
- **Вернуть станцию к Яндексу:** `cp /data/quasar.cfg.orig /system/vendor/quasar/quasar.cfg`
  (с `mount -o remount,rw /`, `sync`, `mount -o remount,ro /` и `q restart`, как в шаге 3).

## Как это устроено

```
Яндекс Станция ──ws──> Quasar Adapter (app в HA OS) ──ws──> интеграция yandex_quasar ──> Assist / Music Assistant
     (root)             эмулирует облако Яндекса            assist_satellite, media_player, …
```

Адаптер (`adapter/`, упакован как app в `ha-addon/`) говорит со станцией на её родном протоколе и подменяет
облачные сервисы Яндекса. Интеграция (`custom_components/yandex_quasar`) подключается к адаптеру и создаёт сущности.

## Разработка

- Адаптер: `cd adapter && tools/gen_protos.sh && uv run pytest`.
- Локальная сборка app, релизы и правила для агентов: [AGENTS.md](AGENTS.md).
- Архитектура, протоколы и исследования ведутся в Notion владельца.

Протокол станции изучен по открытым проектам [quasard](https://github.com/amirilivice/quasard),
[quasar-server](https://github.com/v3rt3p/quasar-server), [quasar-protos](https://github.com/v3rt3p/quasar-protos)
и вики [hackalice.me](https://hackalice.me). Их код в репозиторий не копировался. `.proto`-файлы (© Яндекс) в
репозитории не хранятся: они компилируются при сборке образа app.

## Лицензия

[MIT](LICENSE)
