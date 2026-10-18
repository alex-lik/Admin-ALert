/**
 * Клиент API (план: подключение приложения к серверу).
 *
 * Раньше интерфейс работал на `demoData`. Теперь источник данных — сервер,
 * а демо-данные остались только для тех мест, где сервер ещё ничего не
 * отдал (пустой список показывается честно пустым, а не выдуманным).
 *
 * Что здесь и зачем:
 * - один базовый адрес и один заголовок авторизации, а не размазанные по
 *   экранам `fetch` — иначе смена адреса или заголовка требует правок везде;
 * - `ApiError` с кодом и текстом от сервера: экрану нужно знать «нет сети» и
 *   «пароль неверный» — это разные сообщения, разбирать их по тексту
 *   невозможно;
 * - токен в памяти модуля. Это НЕ постоянное хранилище: перезапуск приложения
 *   его потеряет. Хранение в защищённом хранилище — отдельная задача плана,
 *   и пока её нет, вход приходится повторять после перезапуска.
 */

const DEFAULT_API_URL = 'http://10.0.2.2:8080/api/v1';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }

  /** Сеть недоступна — отличаем от ответа сервера с кодом. */
  get isNetwork() {
    return this.status === 0;
  }
}

let accessToken: string | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

function apiBase(): string {
  return process.env.EXPO_PUBLIC_API_URL ?? DEFAULT_API_URL;
}

async function request<T>(
  path: string,
  init: { method?: string; body?: unknown; auth?: boolean } = {},
): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (init.auth !== false && accessToken) {
    headers.Authorization = `Bearer ${accessToken}`;
  }

  let response: Response;
  try {
    response = await fetch(`${apiBase()}${path}`, {
      method: init.method ?? 'GET',
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
    });
  } catch (error) {
    // Сеть, DNS, сервер не поднят — всё это для пользователя одно и то же
    // сообщение, и различать их незачем: во всех трёх случаях ничего не
    // работает и повторять надо позже.
    throw new ApiError(0, `Сервер недоступен: ${error instanceof Error ? error.message : String(error)}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      // Ответ без JSON (прокси, 502 от балансировщика). Текст показываем
      // как есть, но обрезанным: HTML-страница ошибки в UI неуместна.
      throw new ApiError(response.status, `Ответ сервера не распознан: ${text.slice(0, 200)}`);
    }
  }

  if (!response.ok) {
    throw new ApiError(response.status, extractMessage(payload, response.status));
  }

  return payload as T;
}

/** Достать человеческий текст ошибки из ответа FastAPI. */
function extractMessage(payload: unknown, status: number): string {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === 'string') {
      return detail;
    }
    // Валидация отдаёт detail списком объектов — берём первую же проблему,
    // она обычно единственная и самая понятная.
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: string };
      if (first?.msg) {
        return first.msg.replace(/^Value error, /, '');
      }
    }
  }
  if (status === 401) {
    return 'Нет доступа или пароль неверный';
  }
  return `Ошибка сервера: ${status}`;
}

// ───────────────────────────── Аутентификация ─────────────────────────────

type TokenPair = {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
};

/**
 * Вход. Токен сохраняется в памяти модуля и уходит в заголовок следующих
 * запросов автоматически.
 */
export async function login(email: string, password: string): Promise<TokenPair> {
  const tokens = await request<TokenPair>('/auth/login', {
    method: 'POST',
    body: { email, password },
    auth: false,
  });
  setAccessToken(tokens.access_token);
  return tokens;
}

// ───────────────────────────── Telegram ─────────────────────────────

export type TelegramStatus = {
  bot_configured: boolean;
  chat_id: string | null;
  chat_reachable: boolean | null;
  detail: string | null;
};

export async function telegramStatus(): Promise<TelegramStatus> {
  return request<TelegramStatus>('/telegram/status');
}

/**
 * Привязать чат. Пустая строка снимает привязку.
 *
 * chat_id передаётся query-параметром, а не телом: значение приходит
 * руками из сообщения бота, и это короткая цифра без пробелов. Тело для
 * одного числа было бы лишним слоем, а так ссылку можно скопировать в
 * закладки.
 */
export async function telegramLink(chatId: string): Promise<void> {
  await request(`/telegram/link?chat_id=${encodeURIComponent(chatId)}`, { method: 'POST' });
}

// ───────────────────────────── Алерты ─────────────────────────────

type AlertListResponse = {
  items: unknown[];
  total: number;
  active: number;
  critical: number;
};

export async function fetchAlerts(params: {
  onlyActive?: boolean;
  state?: string;
  project?: string;
} = {}): Promise<AlertListResponse> {
  const query = new URLSearchParams();
  if (params.onlyActive) {
    query.set('only_active', 'true');
  }
  if (params.state) {
    query.set('state', params.state);
  }
  if (params.project) {
    query.set('project', params.project);
  }
  const suffix = query.toString();
  return request<AlertListResponse>(`/alerts${suffix ? `?${suffix}` : ''}`);
}

/** Собирательный объект для вызовов из компонентов. */
export const api = {
  login,
  telegramStatus,
  telegramLink,
  fetchAlerts,
};
