/**
 * Типы предметной области клиента. Должны совпадать со схемой сервера
 * (server/app/schemas/events.py и модель Alert).
 */

import type { AlertState, Severity } from '../theme';

export type { AlertState, Severity };

/** Профиль эскалации — на сервере это EscalationProfile. */
export type EscalationProfile = 'warning' | 'critical' | 'critical-vip' | 'local-disk';

export type Project = {
  id: string;
  name: string;
  isVip: boolean;
};

export type Host = {
  id: string;
  name: string;
  projectId: string;
};

export type Check = {
  id: string;
  name: string;
  description: string;
};

/** Шаг уже выполненной эскалации — то, что показывается в истории. */
export type EscalationStep = {
  id: string;
  /** Номер шага, начиная с 1. */
  step: number;
  /** Что было сделано: push / telegram / call. */
  channel: 'push' | 'telegram' | 'call' | 'snooze';
  /** Кому адресован шаг. */
  target: string;
  at: string;
  /** Итог шага: доставлено, принято, без ответа, занято. */
  outcome: 'delivered' | 'acked' | 'no_answer' | 'busy' | 'failed';
  note?: string;
};

export type Alert = {
  id: string;
  project: Project;
  host: Host;
  check: Check;
  severity: Severity;
  state: AlertState;
  summary: string;
  detail?: string;
  runbookUrl?: string;
  dedupKey: string;
  profile: EscalationProfile;
  /** Счётчик повторов по dedup_key. */
  occurrences: number;
  firstSeenAt: string;
  lastSeenAt: string;
  /** Когда ждём следующего шага эскалации; null — если алерт не активен. */
  nextActionAt: string | null;
  escalationHistory: EscalationStep[];
  ackedAt?: string;
  ackedBy?: string;
};

export type Admin = {
  id: string;
  name: string;
  email: string;
  role: 'admin' | 'viewer';
};

/** Готовый к отправке ответ админа. */
export type AckRequest = {
  alertId: string;
  comment?: string;
};

export type SnoozeRequest = {
  alertId: string;
  minutes: number;
};
