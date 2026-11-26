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

Бэкапы, которые создаются на самой станции по шагам README или автонастройкой из HA:

| Файл | Что в нём | Как вернуть |
|---|---|---|
| `/data/quasar.cfg.orig` | заводской `quasar.cfg` (до шага 3) | станция снова полностью работает с Яндексом |
| `/data/quasar.cfg.uniproxy-only` | `quasar.cfg` после шага 3 (только при ручной настройке) | голос через HA, остальное в облаке |
| `/data/quasar.cfg.prev` | `quasar.cfg` до последней автонастройки из HA | предыдущее состояние станции |
| `/data/quasar-cloud-backup.tar` | авторизация и облачный конфиг станции до полной отвязки | см. «Полный возврат к Яндексу» |
| `/data/quasar-cloud-backup.restored.tar` | тот же бэкап после возврата к Яндексу: следующая отвязка снимет свежий | — |
| `/data/quasar-adapter-state.tar` | авторизация и конфиг станции на момент возврата к Яндексу | — |

Возврат любого `quasar.cfg`:

```sh
mount -o remount,rw /
cp /data/quasar.cfg.orig /system/vendor/quasar/quasar.cfg
sync; mount -o remount,ro /
/system/vendor/quasar/q restart
```

### Полный возврат к Яндексу

Так же делает пункт **Вернуть станцию к Яндексу** в настройках интеграции. После полной отвязки станция хранит
в `/data/quasar` авторизацию и конфиг, полученные от адаптера, поэтому кроме `quasar.cfg` возвращается и
облачный бэкап. Пока quasar работает, он перезаписывает эти файлы, поэтому его нужно сначала остановить:

```sh
/system/vendor/quasar/q stop
mount -o remount,rw /
cp /data/quasar.cfg.orig /system/vendor/quasar/quasar.cfg
sync; mount -o remount,ro /
cd /data/quasar
tar -cf /data/quasar-adapter-state.tar account_storage.dat devices.json data/user_config.dat
tar -xf /data/quasar-cloud-backup.tar -C /data/quasar
mv /data/quasar-cloud-backup.tar /data/quasar-cloud-backup.restored.tar
sync
/system/vendor/quasar/q start
```

Если станция давно не была в облаке, токены в бэкапе могут устареть. Тогда станцию придётся настроить заново в
приложении «Дом с Алисой».

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
