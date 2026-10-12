# Admin-ALert Client

Приложение дежурного админа: список алертов, карточка алерта с историей эскалации,
ack/snooze, экран диагностики разрешений.

Стек: **Expo SDK 57 + React Native 0.86**, сборка через **development build**
(`expo-dev-client`).

## Почему dev-client, а не Expo Go

Expo Go содержит только встроенные модули. Проекту нужны нативные вещи, которых
в Expo Go нет:

- каналы уведомлений с `bypassDnd` и alarm-потоком;
- точные будильники `setAlarmClock` для локальной эскалации;
- Kotlin-модуль на `AudioManager` / `STREAM_ALARM` (задача 3.11);
- FCM через `@react-native-firebase/messaging`.

Expo Go здесь не подойдёт принципиально. Development build — это обычная
нативная сборка с dev-сервером Metro внутри.

## Запуск

```bash
npm install
npx expo run:android          # собрать и поставить на эмулятор/устройство
npx expo start                # только Metro (приложение уже установлено)
```

Полезно:

```bash
npx tsc --noEmit              # проверка типов
npx expo-doctor               # диагностика зависимостей и конфига
npx expo prebuild --clean     # перегенерировать android/ и ios/ из app.json
```

## Что уже сделано

| Файл | Задачи плана | Что внутри |
|---|---|---|
| `src/notifications/channels.ts` | 3.6, 3.7, 3.8, 3.10 | каналы `warning`/`critical`/`critical-vip`, обход DND, full-screen, категории alarm/call, эскалация через `setAlarmClock` с шагами 1/2/3 мин, отмена при ack |
| `src/notifications/fcm.ts` | 3.4, 1.7 | получение FCM-токена, регистрация устройства на сервере, `onTokenRefresh`, фоновый обработчик |
| `App.tsx` | 3.13 | экран диагностики: разрешения, состояние каналов, токен, кнопки тестовых алертов |

### Важная деталь про важность канала

В Notifee перечисление `AndroidImportance` заканчивается на `HIGH` — значения
`MAX` не существует, и его использование не скомпилируется. Это не упрощение:
в Android `IMPORTANCE_MAX` относится к отдельному уведомлению, а
`IMPORTANCE_HIGH` — к каналу, то есть к верхней границе для канала. Ставим `HIGH`.

### Каналы создаются один раз

После создания канала пользователь сам меняет важность, звук и DND — из
приложения это изменить нельзя. Если создать канал неверно, «починить» его
можно только удалением у пользователя, а это сносит его настройки. Поэтому
настройки каналов заданы один раз и осознанно в `channels.ts`.

### Что ещё не сделано в каналах

`bypassDnd`, категории `alarm`/`call`, `fullScreenAction` и `autoCancel: false`
работают. **Не сделано:** звук с `USAGE_ALARM`.

Сейчас в канале стоит `sound: 'default'` — это обычный звук уведомлений через
`STREAM_NOTIFICATION`. По плану (3.7) нужен собственный ресурс в `res/raw` и
свой config plugin: поток звука настройкой канала не выбирается. Это часть
задачи 3.7 вместе с Kotlin-модулем (3.11), который поднимает громкость
принудительно.

## Firebase

Сейчас лежит **placeholder** `google-services.json` — файла нет, потому что
Firebase-проект ещё не создан (задача 1.7). Он нужен только чтобы Gradle
собрался; push через него работать не будет, и экран диагностики честно
покажет это в строке FCM-токен.

Когда появится реальный проект: положить настоящий `google-services.json`
рядом с `app.json`. В репозиторий он не попадёт (см. `.gitignore`), хотя
секретов в нём и нет — только публичные идентификаторы.

## Native folders

`android/` и `ios/` генерируются из `app.json` (Continuous Native Generation)
и **не коммитятся**. Всё нативное поведение задаётся в `app.json`:

- разрешения Android — `expo.android.permissions`;
- `USE_FULL_SCREEN_INTENT`, `SCHEDULE_EXACT_ALARM`,
  `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` — ручной флоу на устройстве (задача 3.9);
- плагины — `expo-dev-client`, `@react-native-firebase/app`.

Если править настройки вручную в `android/`, они потеряются при следующем
`expo prebuild`.

### Config plugin `withNotifeeMavenRepository`

Android-ядро Notifee (`app.notifee:core`) не лежит ни в Google Maven, ни в
Maven Central — оно вкомпоновано в пакет, в
`node_modules/@notifee/react-native/android/libs`. Notifee объявляет этот
репозиторий сам, но при Continuous Native Generation объявление не доезжает
до `:app`, и сборка падает с `Could not find any matches for app.notifee:core:+`.

Поэтому `plugins/withNotifeeMavenRepository.js` добавляет репозиторий во время
`expo prebuild`. Править `android/build.gradle` руками бессмысленно — он
перезаписывается. Плагин идемпотентен и падает с понятной ошибкой, если
структура шаблона изменится.

Взят локальный репозиторий из `node_modules`, а не сетевой
`https://maven.notifee.com` — сборка не зависит от доступности внешнего Maven.

### Первый запуск требует `ANDROID_HOME`

Если Gradle ругается на `SDK location not found`, нужен путь к SDK — в
переменной окружения или в `client/android/local.properties`
(машинно-зависимый файл, в git не коммитится):

```properties
sdk.dir=C\:\\Users\\<user>\\AppData\\Local\\Android\\Sdk
```

## Что проверено на устройстве

Проверено на эмуляторе Android 17 (SDK 37, x86_64), сборка debug. Выводы — из
`adb shell dumpsys notification`, а не из предположений.

| Что | Результат |
|---|---|
| Каналы создаются | ✅ `warning`, `critical`, `critical-vip` |
| Важность канала | ✅ `mImportance=4` (HIGH) — максимум для канала в Android |
| Уведомление показывается | ✅ `channel=critical-vip importance=4 category=call vis=PUBLIC` |
| Показ в режиме «Не беспокоить» | ✅ уведомление видно над секцией Silent |
| Запрос разрешения на уведомления | ✅ работает, статус меняется на «разрешено» |
| FCM-токен | ❌ ожидаемо: нет реального Firebase-проекта (задача 1.7) |

### Три находки, которые сэкономят время дальше

**1. `vibrationPattern` должен содержать чётное число положительных значений.**
Массив нечётной длины Notifee отклоняет, и канал не создаётся вообще. Ведущий
ноль («не ждать перед первой вибрацией») проверку тоже не проходит: требуется
`expected an array containing an even number of positive values`. Рабочий вид:
`[300, 800, 400, 800, 400, 800]`.

**2. `asForegroundService: true` без настоящего foreground service приводит к
тому, что уведомление исчезает сразу после показа.** Android снимает его,
потому что сервис не поднят, а объявлять уведомление от несуществующего сервиса
нельзя. Флаг имеет смысл только вместе с нативным модулем из задачи 3.11.

**3. `bypassDnd` на эмуляторе не проверить.** Даже у каналов самого Google
Dialer (`phone_incoming_call`, importance 4) на этом образе `mBypassDnd=false`,
хотя `ACCESS_NOTIFICATION_POLICY` выдан. Это особенность образа эмулятора,
а не настройка проекта. Обход DND, full-screen и фоновые ограничения нужно
проверять на реальных устройствах — Pixel, Samsung, Xiaomi (задачи 3.12 и 5.3).
Эмулятор годится только для проверки, что каналы создаются и уведомления
доходят.

## Локальные адреса сервера

С `extra.apiUrl` из `app.json` эмулятор Android не достучится до `localhost`
хоста — для него `localhost` это сам эмулятор. Отсюда
`http://10.0.2.2:8080/api/v1`. На физическом устройстве этот адрес не подходит:
нужен IP машины в сети или туннель.

## Что дальше по плану

- [ ] Логин и список алертов, карточка алерта с историей эскалации (3.2)
- [ ] Защищённое хранение сессии через `react-native-keychain` (3.3)
- [ ] Kotlin-модуль: `STREAM_ALARM`, поднятие громкости, foreground service (3.11)
- [ ] Онбординг разрешений по шагам: уведомления → DND → full-screen → батарея → будильники (3.9)
- [ ] Кнопки Ack/Snooze в уведомлении, полноэкранный экран алерта (3.18, 3.20, 3.21)

## Известная оговорка

`expo-doctor` ругается на `@notifee/react-native`: в реестре React Native
Directory он помечен как Unmaintained. Осознанное решение — оставить его:
альтернативы не дают полного контроля над каналами, `bypassDnd` и
alarm-потоком, а это ровно то, ради чего задача 3.7 существует. Пометка
исключена в `package.json`, чтобы не мешала.
