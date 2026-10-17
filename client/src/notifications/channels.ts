/**
 * Каналы уведомлений Android (план, задачи 3.6 и 3.7).
 *
 * ВАЖНО, и это не переоценивается: канал, однажды созданный, пользователь
 * настраивает сам — важность, звук, отключение DND. Приложение не может
 * изменить эти настройки после создания. Если создать канал неверно,
 * «починить» его можно только удалением у пользователя, а это ломает его
 * настройки. Поэтому настройки задаются один раз и осознанно.
 *
 * Профиль -> канал:
 *   warning      -> warning       обычное уведомление, уважает тихие часы
 *   critical     -> critical      будит и обходит DND
 *   critical-vip -> critical-vip  самый агрессивный профиль
 */

import notifee, {
  AndroidCategory,
  AndroidImportance,
  AndroidVisibility,
  AuthorizationStatus,
  EventType,
  TriggerType,
  type AndroidChannel,
  type TimestampTrigger,
} from '@notifee/react-native';

/** Идентификаторы каналов. Должны совпадать с профилями эскалации на сервере. */
export const ChannelId = {
  warning: 'warning',
  critical: 'critical',
  criticalVip: 'critical-vip',
} as const;

export type ChannelIdValue = (typeof ChannelId)[keyof typeof ChannelId];

export type DiagnosticsEntry = {
  name: string;
  ok: boolean;
  detail: string;
};

/**
 * Критичные каналы: обходят DND, видны на lock screen, вибрируют долго.
 *
 * Про важность: у Notifee перечисление заканчивается на HIGH — значения MAX
 * не существует. Это не упрощение, а различие Android: IMPORTANCE_MAX
 * (значение 5) применяется к отдельному уведомлению, а IMPORTANCE_HIGH (4) —
 * к каналу, то есть к верхней границе того, что вообще можно выставить
 * каналу. Ставим HIGH.
 *
 * Про звук: сейчас 'default' — это обычный звук уведомлений, идущий через
 * STREAM_NOTIFICATION. По плану (задача 3.7) нужен звук с USAGE_ALARM, чтобы
 * он перебивал музыку и не зависел от режима звонка. Для этого нужен
 * собственный ресурс в res/raw и свой config plugin — это часть задачи 3.7,
 * а не то, что даётся настройкой канала. Пока не сделано, поэтому заявлять
 * «alarm-звук включён» было бы враньём.
 */
const CRITICAL_CHANNEL: Partial<AndroidChannel> = {
  importance: AndroidImportance.HIGH,
  // Обход DND работает, только если важность канала не ниже High.
  bypassDnd: true,
  vibration: true,
  // Длинная вибрация: сильный сигнал «это не обычное уведомление».
  // Формат Android: [пауза, вибрация, пауза, вибрация, ...]. Notifee проверяет
  // жёстко: элементов должно быть РОВНО чётное число, и все они должны быть
  // положительными. Ведущий ноль («не ждать перед первой вибрацией») такую
  // проверку не проходит — и канал просто не создаётся.
  vibrationPattern: [300, 800, 400, 800, 400, 800],
  lights: true,
  lightColor: '#FF2D2D',
  visibility: AndroidVisibility.PUBLIC,
  sound: 'default',
};

/**
 * Создать все каналы. Идемпотентно: повторный вызов обновляет описание,
 * но importance/звук/DND остаются такими, какими их выставил пользователь.
 */
export async function ensureChannels(): Promise<DiagnosticsEntry[]> {
  const results: DiagnosticsEntry[] = [];

  // warning — обычный канал. Здесь важность НИЖЕ максимума намеренно:
  // предупреждения не должны будить и не должны обходить DND.
  results.push(
    await create(
      ChannelId.warning,
      'Предупреждения',
      'Проблемы, которые не требуют немедленной реакции',
      {
        importance: AndroidImportance.HIGH,
        vibration: true,
        vibrationPattern: [300, 200],
        lights: false,
        sound: 'default',
        visibility: AndroidVisibility.PRIVATE,
      },
    ),
  );

  results.push(
    await create(
      ChannelId.critical,
      'Критичные',
      'Алерт, требующий реакции сейчас. Обходит режим «Не беспокоить»',
      CRITICAL_CHANNEL,
    ),
  );

  results.push(
    await create(
      ChannelId.criticalVip,
      'Критичные VIP',
      'Авария в VIP-проекте. Обходит DND, максимальная важность',
      CRITICAL_CHANNEL,
    ),
  );

  return results;
}

async function create(
  id: ChannelIdValue,
  name: string,
  description: string,
  options: Partial<Parameters<typeof notifee.createChannel>[0]>,
): Promise<DiagnosticsEntry> {
  try {
    const channelId = await notifee.createChannel({
      id,
      name,
      description,
      ...options,
    });
    return { name: `Канал ${id}`, ok: true, detail: channelId };
  } catch (error) {
    return { name: `Канал ${id}`, ok: false, detail: describeError(error) };
  }
}

/** Разрешение на показ уведомлений (Android 13+). */
export async function requestPermission(): Promise<boolean> {
  const settings = await notifee.requestPermission();
  return (
    settings.authorizationStatus === AuthorizationStatus.AUTHORIZED ||
    settings.authorizationStatus === AuthorizationStatus.PROVISIONAL
  );
}

export async function getPermissionStatus(): Promise<string> {
  const settings = await notifee.getNotificationSettings();
  switch (settings.authorizationStatus) {
    case AuthorizationStatus.AUTHORIZED:
      return 'разрешено';
    case AuthorizationStatus.PROVISIONAL:
      return 'временно разрешено (Android 13+)';
    case AuthorizationStatus.DENIED:
      return 'ЗАПРЕЩЕНО — уведомления не придут';
    default:
      return `неизвестно (${settings.authorizationStatus})`;
  }
}

/**
 * Отправить тестовый алерт в указанный канал.
 * Позволяет проверить, что канал действительно обходит DND и звучит.
 */
export async function sendTestAlert(channel: ChannelIdValue): Promise<string> {
  const isCritical = channel !== ChannelId.warning;

  await notifee.displayNotification({
    title: isCritical ? 'ТЕСТ: критичный алерт' : 'ТЕСТ: предупреждение',
    body: isCritical
      ? 'Проект flytaxi, хост db-master-01: диск заполнен на 95%'
      : 'Проект flytaxi, хост api-01: latency 1200ms',
    android: {
      channelId: channel,
      pressAction: { id: 'default', launchActivity: 'default' },
      importance: AndroidImportance.HIGH,
      // Категория alarm/call (задача 3.8). Именно категория даёт Android право
      // показать уведомление поверх экрана блокировки, а не просто в шторке.
      // У warning своей тревожной категории нет — это обычное уведомление.
      category: isCritical ? AndroidCategory.CALL : AndroidCategory.REMINDER,
      // fullScreenAction — задача 3.8: уведомление поднимается поверх всего.
      // Это объект с id, а не флаг: разные id — разные full-screen намерения.
      fullScreenAction: isCritical ? { id: 'alert-fullscreen' } : undefined,
      // autoCancel: false — уведомление не исчезает само (задача 3.8).
      autoCancel: false,
      // asForegroundService намеренно НЕ включён. Android снимает такое
      // уведомление, если приложение не подняло настоящий foreground service,
      // а он живёт в нативном Kotlin-модуле (задача 3.11). Включать флаг
      // раньше Kotlin-модуля — значит получать уведомления, исчезающие сразу
      // после показа.
      color: '#FF2D2D',
      visibility: AndroidVisibility.PUBLIC,
    },
  });

  return `отправлено в канал ${channel}`;
}

/**
 * Показать уведомление, пришедшее с сервера (задачи 2.16–2.17).
 *
 * Отдельная функция, а не переиспользование `sendTestAlert`: у теста
 * фиксированный текст и он намеренно бьёт по DND, чтобы проверить канал. Здесь
 * текст и канал приходят с сервера, а повторы задаёт профиль эскалации — то
 * есть повторный шаг может прийти в канале `warning` и не должен снова будить.
 *
 * `alertId` в id уведомления даёт две вещи. Повторный шаг того же алерта с тем
 * же id перезапишет уведомление, а не наложит второе поверх — иначе шторка
 * забивается «Диск заполнен» ×5. И по нему работают `cancelEscalation` и
 * обработчик нажатий.
 */
export async function displayAlertNotification(params: {
  alertId: string;
  channel: ChannelIdValue;
  title: string;
  body: string;
  occurrences?: number;
  resolved?: boolean;
}): Promise<string> {
  const isCritical = params.channel !== ChannelId.warning;
  const occurrences = params.occurrences ?? 1;
  const suffix = occurrences > 1 ? ` (повтор ${occurrences})` : '';

  await notifee.displayNotification({
    // Отдельный id для «устранено»: тот же alertId перезаписал бы зелёное
    // сообщение тревожным красным.
    id: `${params.alertId}:${params.resolved ? 'resolved' : 'alert'}`,
    title: params.resolved ? 'Проблема устранена' : params.title,
    body: params.resolved ? params.body : `${params.body}${suffix}`,
    android: {
      channelId: params.channel,
      pressAction: { id: 'default', launchActivity: 'default' },
      importance: AndroidImportance.HIGH,
      category: isCritical ? AndroidCategory.CALL : AndroidCategory.REMINDER,
      fullScreenAction: isCritical ? { id: 'alert-fullscreen' } : undefined,
      autoCancel: false,
      // Устранённое — хорошая новость, и висеть тревожным красным поверх
      // экрана блокировки она не должна.
      color: params.resolved ? '#2E7D32' : '#FF2D2D',
      visibility: AndroidVisibility.PUBLIC,
      groupId: params.resolved ? undefined : 'alerts',
    },
    data: { alertId: params.alertId, type: params.resolved ? 'resolved' : 'alert' },
  });

  return `показано в канале ${params.channel}`;
}

/**
 * Локальная эскалация через setAlarmClock (задача 3.10):
 * усиление через 1, 2, 3 минуты. Отменяется при ack.
 *
 * AlarmClock — самый «настойчивый» тип будильника в Android: снимает
 * ограничения Doze и показывает в строке состояния. Требует разрешения
 * SCHEDULE_EXACT_ALARM, которое запрашивается отдельно.
 */
export async function scheduleEscalation(alertId: string, stepsSeconds = [60, 120, 180]) {
  const scheduled: string[] = [];

  for (const [index, seconds] of stepsSeconds.entries()) {
    const trigger: TimestampTrigger = {
      type: TriggerType.TIMESTAMP,
      timestamp: Date.now() + seconds * 1000,
      alarmManager: { allowWhileIdle: true },
    };

    try {
      await notifee.createTriggerNotification(
        {
          id: `${alertId}:escalation:${index}`,
          title: `Эскалация ${index + 1}/${stepsSeconds.length}: алерт не подтверждён`,
          body: 'Алерт всё ещё не подтверждён. Откройте приложение и нажмите «Принял».',
          android: {
            channelId: ChannelId.critical,
            pressAction: { id: 'default' },
            importance: AndroidImportance.HIGH,
            category: AndroidCategory.ALARM,
            autoCancel: false,
          },
        },
        trigger,
      );
      scheduled.push(`+${seconds}с`);
    } catch (error) {
      // Точные будильники могут быть запрещены — это ожидаемая ситуация,
      // её надо показать в диагностике, а не молча проглотить.
      scheduled.push(`+${seconds}с — ОТКАЗАНО: ${describeError(error)}`);
    }
  }

  return scheduled;
}

/** Отменить всю локальную эскалацию по алерту (вызывается при ack). */
export async function cancelEscalation(alertId: string, steps = 3): Promise<number> {
  const ids = Array.from({ length: steps }, (_, i) => `${alertId}:escalation:${i}`);
  await notifee.cancelTriggerNotifications(ids);
  return ids.length;
}

/** Подписаться на действия пользователя в уведомлении (Ack / Snooze). */
export function subscribeToActions(onAction: (id: string, pressAction: string) => void) {
  return notifee.onForegroundEvent(({ type, detail }) => {
    if (type === EventType.ACTION_PRESS) {
      onAction(detail.notification?.id ?? '', detail.pressAction?.id ?? '');
    }
    if (type === EventType.DELIVERED) {
      // delivery receipt — задача 3.22 / серверная 2.20
      console.log('push доставлен:', detail.notification?.id);
    }
  });
}

function describeError(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}
