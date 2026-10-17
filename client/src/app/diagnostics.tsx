/**
 * Диагностика дежурного канала (задача 3.13).
 *
 * Экран существует не для разработчика, а для админа. По плану критичные
 * нюансы Android ломаются по-разному на Pixel, Samsung, Xiaomi и Huawei, и
 * заранее проверить это нельзя — только на устройстве. Здесь админ за десять
 * секунд видит, что у него сломано, до того как реальный алерт не дошёл.
 *
 * Первая версия этого экрана окупилась сразу же: он поймал неверный
 * vibrationPattern, из-за которого каналы critical и critical-vip вообще не
 * создавались.
 */

import { Stack } from 'expo-router';
import { useCallback, useEffect, useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { AppText, Badge, Button, Card, Screen, SectionHeader } from '../components/ui';
import {
  ChannelId,
  cancelEscalation,
  ensureChannels,
  getPermissionStatus,
  requestPermission,
  scheduleEscalation,
  sendTestAlert,
  type DiagnosticsEntry,
} from '../notifications/channels';
import { fetchToken, parsePushData, subscribeToMessaging } from '../notifications/fcm';
import { showPushAlert } from '../notifications/install';
import { palette, spacing, stateColors } from '../theme';

type Line = { label: string; value: string; ok: boolean | null };

export default function DiagnosticsScreen() {
  const [lines, setLines] = useState<Line[]>([]);
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState<string[]>([]);

  const append = useCallback((text: string) => {
    setLog((prev) => [text, ...prev].slice(0, 12));
  }, []);

  const runDiagnostics = useCallback(async () => {
    setBusy(true);
    const next: Line[] = [];

    try {
      const permission = await getPermissionStatus();
      next.push({
        label: 'Разрешение на уведомления',
        value: permission,
        ok: !permission.includes('ЗАПРЕЩЕНО'),
      });

      const channels: DiagnosticsEntry[] = await ensureChannels();
      for (const entry of channels) {
        next.push({ label: entry.name, value: entry.detail, ok: entry.ok });
      }
    } catch (error) {
      next.push({ label: 'Ошибка каналов', value: describe(error), ok: false });
    }

    try {
      const { token, error } = await fetchToken();
      next.push({
        label: 'FCM-токен',
        value: token ? `${token.slice(0, 24)}…` : (error ?? 'нет'),
        ok: Boolean(token),
      });
    } catch (error) {
      next.push({ label: 'FCM-токен', value: describe(error), ok: false });
    }

    setLines(next);
    setBusy(false);
  }, []);

  useEffect(() => {
    runDiagnostics();

    let unsubscribeMessaging: (() => void) | undefined;
    try {
      unsubscribeMessaging = subscribeToMessaging({
        onToken: (token) => append(`токен обновлён: ${token.slice(0, 20)}…`),
        onMessage: (data, remote) =>
          append(
            `push${remote ? ' (фон)' : ''}: [${data.channel}] ${data.project} · ${data.host} — ` +
              `${data.summary}${data.occurrences > 1 ? ` (повтор ${data.occurrences})` : ''}`,
          ),
        onError: (error) => append(`FCM ошибка: ${error}`),
      });
    } catch {
      // Firebase без настроек не инициализируется — экран не должно этим ронять.
    }

    return () => unsubscribeMessaging?.();
  }, [append, runDiagnostics]);

  const askPermission = useCallback(async () => {
    setBusy(true);
    const granted = await requestPermission();
    append(granted ? 'разрешение выдано' : 'разрешение НЕ выдано');
    await runDiagnostics();
  }, [append, runDiagnostics]);

  const testAlert = useCallback(
    async (channel: (typeof ChannelId)[keyof typeof ChannelId]) => {
      setBusy(true);
      try {
        append(await sendTestAlert(channel));
      } catch (error) {
        append(`ошибка: ${describe(error)}`);
      } finally {
        setBusy(false);
      }
    },
    [append],
  );

  /**
   * Прогнать уведомление тем же кодом, каким пришёл бы настоящий push.
   *
   * Отдельный пункт диагностики, потому что проверить «приходит ли push» и
   * «правильно ли приложение его рисует» — разные вещи. Если этот пункт зелёный,
   * а реальный push не приходит, дело в Firebase-проекте, а не в приложении.
   */
  const testServerPayload = useCallback(async () => {
    setBusy(true);
    try {
      const raw = parsePushData({
        type: 'alert',
        alert_id: 'test-alert-id',
        channel: 'critical',
        project: 'flytaxi',
        host: 'db-master-01',
        check: 'disk_space',
        severity: 'critical',
        summary: 'Диск заполнен на 95%',
        detail: 'flytaxi · db-master-01',
        occurrences: '3',
        sent_at: new Date().toISOString(),
      });
      await showPushAlert(raw);
      append(`показано как пришло бы с сервера: [${raw.channel}] ${raw.summary} (повтор ${raw.occurrences})`);
    } catch (error) {
      append(`ошибка: ${describe(error)}`);
    } finally {
      setBusy(false);
    }
  }, [append]);

  const testEscalation = useCallback(async () => {
    setBusy(true);
    const alertId = `test-${Date.now()}`;
    const result = await scheduleEscalation(alertId);
    append(`эскалация ${alertId}: ${result.join(', ')}`);
    setBusy(false);
  }, [append]);

  return (
    <>
      <Stack.Screen options={{ title: 'Диагностика' }} />
      <Screen>
        <AppText variant="title">Диагностика канала</AppText>
        <AppText variant="caption" color={palette.textSecondary}>
          Проверьте раз в неделю. Если что-то красное — дежурный не услышит алерт,
          и узнает об этом только тогда, когда станет слишком поздно.
        </AppText>

        <SectionHeader title="Состояние" />
        <Card>
          {lines.length === 0 ? (
            <AppText variant="caption" color={palette.textMuted}>
              проверка…
            </AppText>
          ) : (
            lines.map((line, index) => (
              <View
                key={line.label}
                style={[styles.line, index < lines.length - 1 && styles.lineDivided]}
              >
                <View style={styles.lineHeader}>
                  <AppText variant="caption" color={palette.textMuted}>
                    {line.label}
                  </AppText>
                  <Badge
                    label={line.ok === null ? '—' : line.ok ? 'ок' : 'ошибка'}
                    fg={line.ok ? stateColors.acked.fg : palette.critical}
                    bg={line.ok ? stateColors.acked.bg : palette.criticalSurface}
                    border={line.ok ? stateColors.acked.border : palette.criticalBorder}
                  />
                </View>
                <AppText
                  variant="mono"
                  color={line.ok === false ? palette.critical : palette.text}
                  style={styles.lineValue}
                >
                  {line.value}
                </AppText>
              </View>
            ))
          )}
        </Card>

        <SectionHeader title="Проверки" />
        <View style={styles.actions}>
          <Button label="Повторить проверку" variant="outline" onPress={runDiagnostics} disabled={busy} fullWidth />
          <Button label="Запросить разрешения" variant="outline" onPress={askPermission} disabled={busy} fullWidth />
        </View>

        <SectionHeader title="Тестовые алерты" />
        <Card>
          <AppText variant="caption" color={palette.textSecondary} style={styles.actionsHint}>
            Тест критичного алерта должен пройти сквозь режим «Не беспокоить».
            Если не прошёл — проблема в разрешениях, а не в канале.
          </AppText>
          <View style={styles.actions}>
            <Button
              label="Предупреждение"
              variant="ghost"
              onPress={() => testAlert(ChannelId.warning)}
              disabled={busy}
              fullWidth
            />
            <Button
              label="Критичный"
              variant="outline"
              onPress={() => testAlert(ChannelId.critical)}
              disabled={busy}
              fullWidth
            />
            <Button
              label="Критичный VIP"
              variant="outline"
              onPress={() => testAlert(ChannelId.criticalVip)}
              disabled={busy}
              fullWidth
            />
          </View>
          <Button
            label="Эскалация 1 / 2 / 3 мин"
            variant="outline"
            onPress={testEscalation}
            disabled={busy}
            fullWidth
          />
          <Button
            label="Payload с сервера"
            variant="ghost"
            onPress={testServerPayload}
            disabled={busy}
            fullWidth
          />
        </Card>

        {log.length > 0 ? (
          <>
            <SectionHeader title="Журнал" />
            <Card>
              {log.map((entry, index) => (
                <AppText key={`${entry}-${index}`} variant="mono" color={palette.textSecondary} style={styles.logLine}>
                  {entry}
                </AppText>
              ))}
            </Card>
          </>
        ) : null}

        <AppText variant="captionMuted" color={palette.textMuted} style={styles.footer}>
          Значения берутся из реальных вызовов Notifee и FCM, а не из предположений.
          Поэтому красная строка — это факт, а не предупреждение.
        </AppText>
      </Screen>
    </>
  );
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

const styles = StyleSheet.create({
  line: { paddingVertical: spacing.sm },
  lineDivided: { borderBottomWidth: 1, borderBottomColor: palette.border },
  lineHeader: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: spacing.sm },
  lineValue: { marginTop: 4 },

  actions: { gap: spacing.sm },
  actionsHint: { marginBottom: spacing.md },

  logLine: { marginBottom: spacing.xs },

  footer: { paddingTop: spacing.lg, lineHeight: 16 },
});
