/**
 * Регистрация FCM-токена на сервере (задачи 3.4 и 1.7).
 *
 * Токен нужно отправлять при старте приложения и при каждом обновлении:
 * onTokenRefresh срабатывает при переустановке, сбросе данных приложения
 * и смене аккаунта. Если этого не делать, сервер шлёт в никуда — push
 * «доставлен», а человек ничего не видит.
 */

import { getApp } from '@react-native-firebase/app';
import { getMessaging, getToken, onMessage, onTokenRefresh } from '@react-native-firebase/messaging';

import { ChannelId } from './channels';

const DEFAULT_API_URL = 'http://10.0.2.2:8080/api/v1';

function apiBase(): string {
  return process.env.EXPO_PUBLIC_API_URL ?? DEFAULT_API_URL;
}

export type TokenState = {
  token: string | null;
  error: string | null;
};

/** Получить текущий FCM-токен. */
export async function fetchToken(): Promise<TokenState> {
  try {
    if (!getApp().options.projectId) {
      return {
        token: null,
        error: 'projectId не настроен — нужен реальный google-services.json (задача 1.7)',
      };
    }

    const token = await getToken(getMessaging());
    return { token, error: null };
  } catch (error) {
    return { token: null, error: describeError(error) };
  }
}

/** Отправить токен серверу. Не бросает — ошибка не должна ронять приложение. */
export async function registerToken(token: string, accessToken: string): Promise<string> {
  const response = await fetch(`${apiBase()}/devices`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify({
      token,
      platform: 'android',
    }),
  });

  if (!response.ok) {
    throw new Error(`регистрация устройства: HTTP ${response.status}`);
  }
  return 'устройство зарегистрировано';
}

/**
 * Подписки на жизненный цикл токена и входящие push.
 * Возвращает функцию отписки.
 */
export function subscribeToMessaging(handlers: {
  onToken: (token: string) => void;
  onMessage: (data: AlertPushData, remote: boolean) => void;
  onError: (error: string) => void;
}) {
  const messaging = getMessaging();

  const unsubscribeTokenRefresh = onTokenRefresh(messaging, (token) => handlers.onToken(token));

  // Приложение открыто. Уведомление всё равно строим через Notifee, а не
  // показываем тост: канал и категория (а значит обход DND и full-screen)
  // задаются именно в уведомлении. Тост их обойти не может.
  const unsubscribeMessage = onMessage(messaging, async (remoteMessage) => {
    handlers.onMessage(parsePushData(remoteMessage.data), false);
  });

  // Фоновое сообщение: приложение было свёрнуто, JS-контекст надо поднять.
  // Именно поэтому сервер шлёт data-only с высоким приоритетом (задача 2.17):
  // data-only без high в фоне Android игнорирует как низкоприоритетное.
  messaging.setBackgroundMessageHandler(async (remoteMessage) => {
    handlers.onMessage(parsePushData(remoteMessage.data), true);
  });

  return () => {
    unsubscribeTokenRefresh();
    unsubscribeMessage();
  };
}

/** Данные алерта, которые сервер кладёт в data-сообщение. */
export type AlertPushData = {
  type: 'alert' | 'resolved';
  alertId: string;
  channel: string;
  project: string;
  host: string;
  check: string;
  severity: string;
  summary: string;
  detail: string;
  occurrences: number;
  sentAt: string;
};

/**
 * Разобрать data-сообщение сервера.
 *
 * Значения приходят строками — FCM требует этого для data. Поэтому
 * occurrences разбираем в число сами: в уведомлении это «повтор N»,
 * и показывать «occurrences: 1» нельзя.
 *
 * Неизвестные поля игнорируем, отсутствующие подставляем: уведомление лучше
 * показать с неполным текстом, чем не показать вовсе.
 */
export function parsePushData(raw: Record<string, unknown> | undefined): AlertPushData {
  // Типы react-native-firebase объявляют data как `string | object`, но FCM по
  // контракту присылает только строки. Приводим явно, а не полагаемся на
  // объявленный тип: иначе в уведомление попадёт «[object Object]».
  const str = (key: string, fallback: string): string => {
    const value = raw?.[key];
    return typeof value === 'string' && value.length > 0 ? value : fallback;
  };

  const parsed = Number.parseInt(str('occurrences', '1'), 10);

  return {
    type: str('type', 'alert') === 'resolved' ? 'resolved' : 'alert',
    alertId: str('alert_id', ''),
    channel: str('channel', ChannelId.critical),
    project: str('project', ''),
    host: str('host', ''),
    check: str('check', ''),
    severity: str('severity', 'critical'),
    summary: str('summary', 'Алерт'),
    detail: str('detail', ''),
    occurrences: Number.isFinite(parsed) ? parsed : 1,
    sentAt: str('sent_at', ''),
  };
}

function describeError(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}
