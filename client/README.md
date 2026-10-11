# Admin-ALert Client (React Native)

Приложение дежурного админа: список алертов, карточка алерта с историей эскалации,
ack/snooze, экран диагностики разрешений.

**Статус: каркас.** Ручная инициализация RN-проекта ещё не выполнена, `package.json`
фиксирует нужные зависимости заранее.

## Инициализация

Expo Go **не подходит** — нужны нативные модули (Kotlin для `STREAM_ALARM`,
настройки каналов, `setAlarmClock`). Два варианта:

```bash
# Вариант A — чистый RN CLI (рекомендуется для кастомных нативных модулей)
npx @react-native-community/cli init AdminAlert --directory . --skip-install

# Вариант B — Expo с dev-client и prebuild
npx create-expo-app@latest . --template blank-typescript
npx expo install @notifee/react-native @react-native-firebase/app @react-native-firebase/messaging
npx expo prebuild
npm run android   # требуется dev-client, не Expo Go
```

Дальше:

```bash
npm install
```

## Что здесь будет (по плану, раздел 3)

| Раздел | Содержимое |
|---|---|
| Основа | логин, список алертов, карточка, настройки, диагностика; защищённое хранилище сессии; регистрация device token |
| Android | каналы `warning`/`critical`/`critical-vip`, `bypassDnd`, `USAGE_ALARM`, full-screen intent, локальная эскалация через `setAlarmClock()` (1/2/3 мин), Kotlin-модуль с `STREAM_ALARM` и foreground service |
| iOS | Push Notifications + Background Modes, `criticalAlert` (после entitlement), Notification Service Extension, действия Ack/Snooze |
| Общее | Ack/Snooze прямо в уведомлении, полноэкранный экран алерта, delivery receipt, RU/EN, тёмная тема |

## Критичные нюансы, которые нельзя упустить

1. **Каналы Android создаются один раз.** После создания пользователь сам меняет важность,
   звук и DND — изменить настройки канала из приложения нельзя. Продумать до релиза.
2. **`setBypassDnd(true)` работает только при importance не ниже High.**
3. **Data-only FCM-сообщение без high-priority в фоне игнорируется.**
4. **На iOS в фоне нельзя запустить таймер** — «усиление» делают серверные повторы push.
   На Android — локальный `setAlarmClock()`.
5. **Samsung / Xiaomi / Huawei** ограничивают фон и full-screen. Эмулятор и Pixel
   не показатель — проверять на реальных устройствах (задачи 3.12, 5.3).

## Полезные команды

```bash
npm run android            # debug-сборка
npm run test
npx react-native log-android
```

Права в `android/app/src/main/AndroidManifest.xml` и `ios/AdminAlert/Info.plist`
задаются по плану, раздел 3.9.
