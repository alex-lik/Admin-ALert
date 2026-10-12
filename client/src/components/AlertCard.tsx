/**
 * Карточка алерта в списке и таймлайн истории эскалации.
 */

import { StyleSheet, View } from 'react-native';

import { AppText, Badge, Card, Chip, StateBadge } from './ui';
import { colorForState, palette, radius, spacing, stateColors } from '../theme';
import type { Alert, EscalationStep } from '../types/alert';

const CHANNEL_LABELS: Record<EscalationStep['channel'], string> = {
  push: 'Push',
  telegram: 'Telegram',
  call: 'Звонок',
  snooze: 'Snooze',
  reassign: 'Передача',
};

const OUTCOME_LABELS: Record<EscalationStep['outcome'], { label: string; color: string }> = {
  delivered: { label: 'доставлено', color: palette.textSecondary },
  acked: { label: 'подтверждено', color: stateColors.acked.fg },
  no_answer: { label: 'нет ответа', color: stateColors.escalated.fg },
  busy: { label: 'занято', color: stateColors.escalated.fg },
  failed: { label: 'сбой', color: stateColors.critical.fg },
};

function formatTime(iso: string): string {
  const date = new Date(iso);
  return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

function formatAgo(iso: string): string {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60_000));
  if (minutes < 1) return 'только что';
  if (minutes < 60) return `${minutes} мин назад`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} ч назад`;
  return `${Math.round(hours / 24)} дн назад`;
}

/** Карточка алерта для списка. */
export function AlertCard({ alert, onPress }: { alert: Alert; onPress: () => void }) {
  const accent = colorForState(alert.state, alert.severity, alert.project.isVip);
  const active = alert.state !== 'resolved' && alert.state !== 'acked';

  return (
    <Card onPress={onPress} accent={accent}>
      <View style={styles.rowBetween}>
        <View style={styles.rowGap}>
          <StateBadge state={alert.state} severity={alert.severity} isVip={alert.project.isVip} />
          {alert.occurrences > 1 ? (
            <Chip label={`повторов: ${alert.occurrences}`} tone={active ? 'critical' : 'neutral'} />
          ) : null}
        </View>
        <AppText variant="captionMuted" color={palette.textMuted}>
          {formatAgo(alert.lastSeenAt)}
        </AppText>
      </View>

      <AppText variant="heading" numberOfLines={2}>
        {alert.summary}
      </AppText>

      <View style={styles.metaRow}>
        <AppText variant="caption" color={palette.textSecondary} numberOfLines={1} style={styles.metaItem}>
          {alert.project.name}
        </AppText>
        <View style={styles.dot} />
        <AppText variant="caption" color={palette.textSecondary} numberOfLines={1} style={styles.metaItem}>
          {alert.host.name}
        </AppText>
        <View style={styles.dot} />
        <AppText variant="caption" color={palette.textMuted} numberOfLines={1} style={styles.metaItem}>
          {alert.check.name}
        </AppText>
      </View>

      {alert.nextActionAt && active ? (
        <View style={styles.nextRow}>
          <Badge
            label={`следующий шаг ${formatTime(alert.nextActionAt)}`}
            fg={palette.warning}
            bg={palette.warningSurface}
            border={palette.warningBorder}
          />
        </View>
      ) : null}
    </Card>
  );
}

/** История эскалации: кто, куда, когда, с каким результатом. */
export function EscalationTimeline({ steps }: { steps: EscalationStep[] }) {
  if (steps.length === 0) {
    return (
      <AppText variant="caption" color={palette.textMuted}>
        Шагов эскалации ещё не было.
      </AppText>
    );
  }

  return (
    <View>
      {steps.map((step, index) => {
        const outcome = OUTCOME_LABELS[step.outcome];
        const isLast = index === steps.length - 1;

        return (
          <View key={step.id} style={styles.timelineRow}>
            <View style={styles.rail}>
              <View
                style={[
                  styles.marker,
                  {
                    borderColor: outcome.color,
                    backgroundColor: outcome.color + '33',
                  },
                ]}
              />
              {!isLast ? <View style={styles.railLine} /> : null}
            </View>

            <View style={[styles.timelineBody, isLast && styles.timelineBodyLast]}>
              <View style={styles.rowBetween}>
                <AppText variant="bodyStrong">
                  Шаг {step.step} · {CHANNEL_LABELS[step.channel]}
                </AppText>
                <AppText variant="captionMuted" color={palette.textMuted}>
                  {formatTime(step.at)}
                </AppText>
              </View>

              <AppText variant="caption" color={palette.textSecondary}>
                {step.target}
              </AppText>

              <AppText variant="caption" color={outcome.color}>
                {outcome.label}
                {step.note ? ` — ${step.note}` : ''}
              </AppText>
            </View>
          </View>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  rowBetween: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  rowGap: { flexDirection: 'row', alignItems: 'center', gap: spacing.sm, flexShrink: 1 },

  metaRow: { flexDirection: 'row', alignItems: 'center', gap: spacing.sm, marginTop: 2 },
  metaItem: { flexShrink: 1 },
  dot: { width: 3, height: 3, borderRadius: 2, backgroundColor: palette.textMuted },

  nextRow: { flexDirection: 'row', marginTop: spacing.xs },

  timelineRow: { flexDirection: 'row', gap: spacing.md },
  rail: { alignItems: 'center', width: 20 },
  marker: { width: 12, height: 12, borderRadius: 6, borderWidth: 2, marginTop: 4 },
  railLine: { flex: 1, width: 2, backgroundColor: palette.border, marginVertical: 2 },
  timelineBody: { flex: 1, gap: 2, paddingBottom: spacing.lg },
  timelineBodyLast: { paddingBottom: 0 },
});
