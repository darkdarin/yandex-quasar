# Станция: дополнительно

Пошаговая установка, включая полную отвязку от облака Яндекса, описана в [README](../README.md#установка).
Здесь — то, что пригодится после неё.

## Бэкапы и откат

Перед первыми правками стоит снять копии файлов на компьютер:

```sh
adb pull /system/vendor/quasar/quasar.cfg
adb pull /system/vendor/quasar/launch.json
adb pull /data/quasar/devices.json   # секрет станции: никому не отправляйте и не публикуйте
```

Бэкапы, которые создаются на самой станции по шагам README:

| Файл | Что в нём | Как вернуть |
|---|---|---|
| `/data/quasar.cfg.orig` | заводской `quasar.cfg` (до шага 3) | станция снова полностью работает с Яндексом |
| `/data/quasar.cfg.uniproxy-only` | `quasar.cfg` после шага 3 | голос через HA, остальное в облаке |
| `/data/quasar-cloud-backup.tar` | авторизация и облачный конфиг станции | `cd /data/quasar && tar -xf /data/quasar-cloud-backup.tar` |

Возврат любого `quasar.cfg`:

```sh
mount -o remount,rw /
cp /data/quasar.cfg.orig /system/vendor/quasar/quasar.cfg
sync; mount -o remount,ro /
/system/vendor/quasar/q restart
```

## Защита от обновлений прошивки

- После полной отвязки адаптер на запросы обновлений всегда отвечает «обновлений нет» и выключает OTA в конфиге
  станции.
- Дополнительно на устройстве: `yandex-noupdate.sh` из вики hackalice.me (подмена `ota_script.pub`, заглушка
  `device_ota_update.sh`, удаление `--updatesd` из `launch.json`). Заблокируйте `updater.mobile.yandex.net`.

## Подводные камни

- Без `sync` изменения на UBIFS теряются.
- Сброс к заводским настройкам стирает `/data`: маркеры ADB, dropbear и бэкапы.
- Если станция хоть раз синхронизируется с настоящим облаком, Яндекс может выключить ADB
  (`Deactivate adb by quasmodrom config`) и перезаписать unbound.
- После полной отвязки проверку сети (`generate_204`) станции отвечает адаптер. Если он долго недоступен, станция
  решает, что сети нет, и может начать переподключать Wi-Fi.
- Проверка сети идёт на хост из `common.robotBackendHost`. Если станцию отвязывали до версии 0.2.26 (вручную или
  автонастройкой из HA), этот ключ ещё указывает на `scbh.yandex.net`, и без доступа в интернет проверка не
  проходит. Повторите автонастройку или поменяйте ключ вручную на `<IP HA>:8080` (без `http://`), как в
  [README](../README.md#установка), и перезапустите quasar.
