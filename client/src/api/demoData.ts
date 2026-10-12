/**
 * Демо-данные.
 *
 * Нужны, чтобы интерфейс можно было разрабатывать и показывать до того, как
 * заработает сервер. Как только появится реальный API, модуль заменяется на
 * вызовы, а типы остаются те же — поменять придётся только источник данных.
 *
 * Явно помечены как демо, чтобы их нельзя было случайно показать как
 * production-данные: у всех проектов тут флаг `demo: true`.
 */

import type { Alert } from '../types/alert';

const now = Date.now();
const minutesAgo = (m: number) => new Date(now - m * 60_000).toISOString();
const minutesAhead = (m: number) => new Date(now + m * 60_000).toISOString();

export const demoAlerts: Alert[] = [
  {
    id: 'alt_001',
    project: { id: 'prj_flytaxi', name: 'flytaxi', isVip: true },
    host: { id: 'h_db01', name: 'db-master-01', projectId: 'prj_flytaxi' },
    check: { id: 'chk_disk', name: 'disk_space', description: 'Свободное место на разделе /' },
    severity: 'critical',
    state: 'notifying',
    summary: 'Диск заполнен на 95%',
    detail:
      'Раздел /data осталось 8.4 ГБ из 160 ГБ. Рост ~2 ГБ в сутки, при текущей скорости полное заполнение ожидается через 4 дня. База PostgreSQL пишет логи без ротации.',
    runbookUrl: 'https://wiki.internal/runbooks/db-disk-full',
    dedupKey: 'flytaxi:disk_space:db-master-01',
    profile: 'critical-vip',
    occurrences: 47,
    firstSeenAt: minutesAgo(312),
    lastSeenAt: minutesAgo(1),
    nextActionAt: minutesAhead(2),
    escalationHistory: [
      {
        id: 'st1',
        step: 1,
        channel: 'push',
        target: 'Дежурный (Иван П.)',
        at: minutesAgo(312),
        outcome: 'delivered',
      },
      {
        id: 'st2',
        step: 2,
        channel: 'telegram',
        target: 'Дежурный (Иван П.)',
        at: minutesAgo(288),
        outcome: 'delivered',
      },
      {
        id: 'st3',
        step: 3,
        channel: 'call',
        target: 'Дежурный (Иван П.)',
        at: minutesAgo(243),
        outcome: 'no_answer',
        note: '30 секунд без ответа, звонок переведён на запасного',
      },
      {
        id: 'st4',
        step: 4,
        channel: 'push',
        target: 'Запасной (Мария К.)',
        at: minutesAgo(4),
        outcome: 'delivered',
      },
    ],
  },
  {
    id: 'alt_002',
    project: { id: 'prj_etaxi', name: 'etaxi', isVip: false },
    host: { id: 'h_api03', name: 'api-03', projectId: 'prj_etaxi' },
    check: { id: 'chk_latency', name: 'api_latency', description: 'Время ответа p95 по /v2/orders' },
    severity: 'critical',
    state: 'triggered',
    summary: 'p95 latency 4200 мс при норме 800 мс',
    detail:
      'Рост начался в 03:12. Корреляция с ростом нагрузки на /v2/orders. Метрика p99 не затронута — похоже на деградацию одного из трёх бэкендов заOrderService.',
    runbookUrl: 'https://wiki.internal/runbooks/api-latency',
    dedupKey: 'etaxi:api_latency:api-03',
    profile: 'critical',
    occurrences: 12,
    firstSeenAt: minutesAgo(26),
    lastSeenAt: minutesAgo(2),
    nextActionAt: minutesAhead(1),
    escalationHistory: [
      {
        id: 'st5',
        step: 1,
        channel: 'push',
        target: 'Дежурный (Иван П.)',
        at: minutesAgo(26),
        outcome: 'delivered',
      },
    ],
  },
  {
    id: 'alt_003',
    project: { id: 'prj_keytrain', name: 'keytrain', isVip: false },
    host: { id: 'h_web01', name: 'web-01', projectId: 'prj_keytrain' },
    check: { id: 'chk_cert', name: 'ssl_certificate', description: 'Срок действия сертификата' },
    severity: 'warning',
    state: 'triggered',
    summary: 'Сертификат истекает через 12 дней',
    detail:
      'Сертификат для *.keytrain.com истекает 24 октября. Автопродление не сработало: ACME-челлендж не проходит проверку HTTP-01.',
    runbookUrl: 'https://wiki.internal/runbooks/cert-renew',
    dedupKey: 'keytrain:ssl_certificate:web-01',
    profile: 'warning',
    occurrences: 3,
    firstSeenAt: minutesAgo(140),
    lastSeenAt: minutesAgo(140),
    nextActionAt: null,
    escalationHistory: [
      {
        id: 'st6',
        step: 1,
        channel: 'push',
        target: 'Дежурный (Иван П.)',
        at: minutesAgo(140),
        outcome: 'delivered',
      },
    ],
  },
  {
    id: 'alt_004',
    project: { id: 'prj_flytaxi', name: 'flytaxi', isVip: true },
    host: { id: 'h_redis', name: 'redis-cache-01', projectId: 'prj_flytaxi' },
    check: { id: 'chk_memory', name: 'redis_memory', description: 'ДоляUsed memory в Redis' },
    severity: 'warning',
    state: 'acked',
    summary: 'Redis: память 88% от лимита maxmemory',
    detail: 'eviction policy: allkeys-lru. Рост ключей sessions без TTL.',
    dedupKey: 'flytaxi:redis_memory:redis-cache-01',
    profile: 'critical-vip',
    occurrences: 18,
    firstSeenAt: minutesAgo(95),
    lastSeenAt: minutesAgo(95),
    nextActionAt: null,
    ackedAt: minutesAgo(62),
    ackedBy: 'Мария К.',
    escalationHistory: [
      {
        id: 'st7',
        step: 1,
        channel: 'push',
        target: 'Дежурный (Иван П.)',
        at: minutesAgo(95),
        outcome: 'delivered',
      },
      {
        id: 'st8',
        step: 2,
        channel: 'push',
        target: 'Дежурный (Иван П.)',
        at: minutesAgo(62),
        outcome: 'acked',
        note: 'Подтверждено из приложения',
      },
    ],
  },
  {
    id: 'alt_005',
    project: { id: 'prj_address', name: 'address-resolution', isVip: false },
    host: { id: 'h_geo', name: 'geo-01', projectId: 'prj_address' },
    check: { id: 'chk_queue', name: 'queue_depth', description: 'Глубина очереди геокодирования' },
    severity: 'warning',
    state: 'resolved',
    summary: 'Очередь геокодирования: 4200 записей',
    detail: 'Очередь опустела после перезапуска воркера на geo-01.',
    dedupKey: 'address:queue_depth:geo-01',
    profile: 'warning',
    occurrences: 5,
    firstSeenAt: minutesAgo(420),
    lastSeenAt: minutesAgo(340),
    nextActionAt: null,
    escalationHistory: [
      {
        id: 'st9',
        step: 1,
        channel: 'push',
        target: 'Дежурный (Иван П.)',
        at: minutesAgo(420),
        outcome: 'delivered',
      },
      {
        id: 'st10',
        step: 2,
        channel: 'push',
        target: 'Дежурный (Иван П.)',
        at: minutesAgo(380),
        outcome: 'delivered',
      },
    ],
  },
];
