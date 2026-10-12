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
  onMessage: (title: string, body: string) => void;
  onError: (error: string) => void;
}) {
  const messaging = getMessaging();

  const unsubscribeTokenRefresh = onTokenRefresh(messaging, (token) => handlers.onToken(token));

  const unsubscribeMessage = onMessage(messaging, async (remoteMessage) => {
    handlers.onMessage(
      String(remoteMessage.notification?.title ?? remoteMessage.data?.summary ?? 'Алерт'),
      String(remoteMessage.notification?.body ?? remoteMessage.data?.host ?? ''),
    );
  });

  // Фоновое сообщение: приложение было свёрнуто, JS-контекст надо поднять.
  // Именно поэтому сервер шлёт data-only с высоким приоритетом (задача 2.17).
  messaging.setBackgroundMessageHandler(async (remoteMessage) => {
    console.log('фоновое сообщение:', remoteMessage.messageId);
  });

  return () => {
    unsubscribeTokenRefresh();
    unsubscribeMessage();
  };
}

function describeError(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}
