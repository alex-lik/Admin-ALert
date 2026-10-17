/**
 * Установка обработчиков push: входящее сообщение -> уведомление.
 *
 * Отдельный файл, потому что это единственное место в приложении, которое
 * обязано быть поднято при любом состоянии — и в том числе в фоне, когда
 * expo-router ещё не смонтировал ни одного экрана. Если повесить это на
 * useEffect в layout, уведомление о критичном алерте не покажется в свёрнутом
 * приложении — а это ровно тот случай, ради которого всё затевалось.
 *
 * Вызывается один раз из корневого layout при монтировании.
 */

import { Platform } from 'react-native';

import {
  ChannelId,
  cancelEscalation,
  displayAlertNotification,
  ensureChannels,
  type ChannelIdValue,
} from './channels';
import { parsePushData, subscribeToMessaging, type AlertPushData } from './fcm';

type Log = (line: string) => void;

const KNOWN_CHANNELS = new Set<string>(Object.values(ChannelId));

/**
 * Вызвать `displayAlertNotification` с нормализованными данными.
 *
 * Экспорт — чтобы диагностика могла показать уведомление таким же кодом, каким
 * пришёл бы настоящий push, а не второй копией этой логики.
 */
export async function showPushAlert(data: AlertPushData): Promise<void> {
  const channel: ChannelIdValue = KNOWN_CHANNELS.has(data.channel)
    ? (data.channel as ChannelIdValue)
    // Канал неизвестен серверу (например, добавили новый профиль эскалации, а
    // клиент старый). Молча уводить алерт в warning нельзя: лучше громкий
    // канал по умолчанию, чем дежурный, который не заметил предупреждение.
    : ChannelId.critical;

  await displayAlertNotification({
    alertId: data.alertId,
    channel,
    title: data.summary,
    body: data.detail || `${data.project} · ${data.host}`,
    occurrences: data.occurrences,
    resolved: data.type === 'resolved',
  });
}

/**
 * Поднять обработчики. Возвращает функцию отписки.
 *
 * Каналы создаются здесь же, а не на экране диагностики: первое уведомление
 * может прийти до того, как админ туда заглянет, а уведомление в несуществующий
 * канал Android просто выбросит.
 */
export function installPushHandlers(log: Log = () => {}): () => void {
  if (Platform.OS === 'web') {
    return () => {};
  }

  let unsubscribe: (() => void) | undefined;

  // Каналы создаются асинхронно, а подписка должна быть синхронной — иначе
  // сообщение, пришедшее в первые секунды после запуска, пропадёт. Поэтому
  // создание каналов не блокирует подписку: у Notifee запрос на создание
  // канала идёт сразу, а первое сообщение приходит заметно позже.
  void ensureChannels()
    .then((entries) => {
      const failed = entries.filter((e) => !e.ok);
      if (failed.length > 0) {
        log(`каналы не созданы: ${failed.map((e) => e.name).join(', ')}`);
      }
    })
    .catch((error: unknown) => {
      log(`каналы: ${error instanceof Error ? error.message : String(error)}`);
    });

  try {
    unsubscribe = subscribeToMessaging({
      onToken: () => {
        // Токен без Firebase-проекта не приходит вовсе; настоящая регистрация
        // идёт из экрана входа, где уже есть access token.
        log('push-токен обновлён');
      },
      onMessage: (data) => {
        if (!data.alertId) {
          log('push без alertId — вероятно, чужое сообщение в этот канал');
          return;
        }

        if (data.type === 'resolved') {
          // Локальная эскалация снимается: проблема решена, будить дальше нечего.
          void cancelEscalation(data.alertId).catch(() => {});
        }

        void showPushAlert(data).catch((error: unknown) => {
          log(`уведомление не показано: ${error instanceof Error ? error.message : String(error)}`);
        });
      },
      onError: (error) => log(`FCM: ${error}`),
    });
  } catch (error) {
    // Firebase без google-services.json не инициализируется. Приложение от
    // этого падать не должно — остальное работает.
    log(`FCM не инициализирован: ${error instanceof Error ? error.message : String(error)}`);
  }

  return () => unsubscribe?.();
}
