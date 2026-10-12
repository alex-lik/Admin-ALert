/**
 * Карточка алерта (задача 3.2): суть проблемы, история эскалации, действия.
 *
 * Главное на этом экране — не «описание», а две крупные кнопки: «Принял» и
 * «Передать». Ночью, в панике, одним большим пальцем. Всё остальное вторично.
 */

import { Stack, useLocalSearchParams } from 'expo-router';
import { useMemo, useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { EscalationTimeline } from '../../components/AlertCard';
import { AppText, Badge, Button, Card, Divider, FieldRow, Screen, SectionHeader, StateBadge } from '../../components/ui';
import { demoAlerts } from '../../api/demoData';
import { colorForState, palette, spacing } from '../../theme';

export default function AlertDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const alert = useMemo(() => demoAlerts.find((a) => a.id === id), [id]);
  const [acked, setAcked] = useState(false);
  const [snoozed, setSnoozed] = useState<number | null>(null);
  const [sending, setSending] = useState(false);

  if (!alert) {
    return (
      <Screen>
        <AppText variant="heading" color={palette.textSecondary}>
          Алерт не найден
        </AppText>
        <AppText variant="caption" color={palette.textMuted}>
          Возможно, он уже устранён и убран из списка.
        </AppText>
      </Screen>
    );
  }

  const accent = colorForState(alert.state, alert.severity, alert.project.isVip);
  const isClosed = alert.state === 'resolved' || acked;

  const handleAck = () => {
    setSending(true);
    // Заглушка реального вызова. Сейчас она специально синхронная и честная:
    // имитирует задержку сети, чтобы было видно состояние кнопки.
    setTimeout(() => {
      setAcked(true);
      setSending(false);
    }, 400);
  };

  const handleSnooze = (minutes: number) => {
    setSending(true);
    setTimeout(() => {
      setSnoozed(minutes);
      setSending(false);
    }, 400);
  };

  return (
    <>
      <Stack.Screen options={{ title: alert.project.name }} />
      <Screen>
        <View style={styles.badges}>
          <StateBadge state={acked ? 'acked' : alert.state} severity={alert.severity} isVip={alert.project.isVip} />
          {alert.occurrences > 1 ? (
            <Badge
              label={`повторов: ${alert.occurrences}`}
              fg={palette.warning}
              bg={palette.warningSurface}
              border={palette.warningBorder}
            />
          ) : null}
        </View>

        <AppText variant="title">{alert.summary}</AppText>

        {alert.detail ? (
          <AppText variant="body" color={palette.textSecondary}>
            {alert.detail}
          </AppText>
        ) : null}

        {/* Действия. Кнопка «Принял» — самая крупная точка экрана. */}
        {isClosed ? (
          <Card style={styles.closedCard}>
            <AppText variant="heading" color={palette.success}>
              Алерт подтверждён
            </AppText>
            <AppText variant="caption" color={palette.textSecondary}>
              Эскалация остановлена: повторные push, звонок и напоминания в Telegram
              не придут. Счётчик событий продолжает обновляться — если проблема
              вернётся, заведётся новый алерт.
            </AppText>
          </Card>
        ) : (
          <>
            <Button label="Принял — остановить эскалацию" variant="primary" onPress={handleAck} loading={sending} fullWidth />
            <View style={styles.secondaryActions}>
              <Button
                label="Передать"
                variant="outline"
                onPress={() => undefined}
                style={styles.secondaryButton}
                fullWidth
              />
              <Button
                label="Отложить 15 мин"
                variant="ghost"
                onPress={() => handleSnooze(15)}
                loading={sending}
                style={styles.secondaryButton}
                fullWidth
              />
            </View>
            <AppText variant="captionMuted" color={palette.textMuted} style={styles.snoozeHint}>
              Откладывание ограничено: не больше 2 раз в час на алерт. Откладывание не
              работает для критичных — иначе дежурный отложит всё подряд.
            </AppText>
          </>
        )}

        {snoozed ? (
          <Badge
            label={`Отложено на ${snoozed} минут`}
            fg={palette.info}
            bg={palette.infoSurface}
            border={palette.infoBorder}
          />
        ) : null}

        <SectionHeader title="Где случилось" />
        <Card>
          <FieldRow label="Проект" value={alert.project.name} />
          <FieldRow label="Хост" value={alert.host.name} mono />
          <FieldRow label="Проверка" value={alert.check.name} mono />
          <FieldRow label="Описание проверки" value={alert.check.description} />
          <Divider />
          <FieldRow label="Ключ дедупликации" value={alert.dedupKey} mono />
          <FieldRow label="Профиль эскалации" value={alert.profile} mono />
        </Card>

        <SectionHeader title="Время" />
        <Card>
          <FieldRow label="Первое срабатывание" value={formatTime(alert.firstSeenAt)} />
          <FieldRow label="Последнее" value={formatTime(alert.lastSeenAt)} />
          {alert.nextActionAt ? (
            <FieldRow
              label="Следующий шаг"
              value={formatTime(alert.nextActionAt)}
              valueColor={palette.warning}
            />
          ) : null}
          {alert.ackedAt ? (
            <FieldRow
              label="Подтверждён"
              value={`${formatTime(alert.ackedAt)}${alert.ackedBy ? ` · ${alert.ackedBy}` : ''}`}
              valueColor={palette.success}
            />
          ) : null}
        </Card>

        {alert.runbookUrl ? (
          <>
            <SectionHeader title="Runbook" />
            <Card>
              <AppText variant="mono" color={palette.info}>
                {alert.runbookUrl}
              </AppText>
              <AppText variant="captionMuted" color={palette.textMuted}>
                Что делать, когда откроешь ссылку, — написано там. Короткая версия:
                проверить, что именно переполнено, и не удалять логи до выяснения.
              </AppText>
            </Card>
          </>
        ) : null}

        <SectionHeader
          title="История эскалации"
          action={
            <AppText variant="captionMuted" color={palette.textMuted}>
              шагов: {alert.escalationHistory.length}
            </AppText>
          }
        />
        <Card>
          <EscalationTimeline steps={alert.escalationHistory} />
        </Card>

        <View style={styles.footer}>
          <AppText variant="captionMuted" color={palette.textMuted}>
            Цветная полоса слева — уровень алерта. Она же определяет, какой канал
            уведомлений сработает: warning, critical или critical-vip.
          </AppText>
        </View>
      </Screen>
    </>
  );
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

const styles = StyleSheet.create({
  badges: { flexDirection: 'row', gap: spacing.sm, flexWrap: 'wrap' },
  closedCard: { backgroundColor: palette.successSurface, borderColor: palette.successBorder, gap: spacing.sm },

  secondaryActions: { flexDirection: 'row', gap: spacing.sm },
  secondaryButton: { flex: 1 },
  snoozeHint: { lineHeight: 16 },

  footer: { paddingTop: spacing.lg, paddingBottom: spacing.md },
});
