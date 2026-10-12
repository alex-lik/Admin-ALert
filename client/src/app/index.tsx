/**
 * Главный экран: список алертов (задача 3.2).
 *
 * Порядок в списке — не «по времени». Дежурному важнее то, что горит и на
 * него не ответили. Поэтому сортировка: активные → VIP → критичность →
 * время последнего срабатывания. Устранённые и подтверждённые уходят вниз.
 */

import { Link, Stack, router } from 'expo-router';
import { useMemo, useState } from 'react';
import { Pressable, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { AlertCard } from '../components/AlertCard';
import { AppText, Card, EmptyState, Screen, SettingsIcon } from '../components/ui';
import { demoAlerts } from '../api/demoData';
import { colorForState, palette, radius, spacing } from '../theme';
import type { Alert } from '../types/alert';

type Filter = 'active' | 'acked' | 'all';

const FILTERS: { key: Filter; label: string }[] = [
  { key: 'active', label: 'Активные' },
  { key: 'acked', label: 'Закрытые' },
  { key: 'all', label: 'Все' },
];

/** Ранг важности: чем больше, тем выше в списке. */
function priority(alert: Alert): number {
  const active = alert.state !== 'resolved' && alert.state !== 'acked';
  if (!active) return -1;
  if (alert.project.isVip && alert.severity === 'critical') return 100;
  if (alert.severity === 'critical') return 80;
  if (alert.project.isVip) return 60;
  return 40;
}

function sortAlerts(alerts: Alert[]): Alert[] {
  return [...alerts].sort((a, b) => {
    const diff = priority(b) - priority(a);
    if (diff !== 0) return diff;
    return new Date(b.lastSeenAt).getTime() - new Date(a.lastSeenAt).getTime();
  });
}

export default function AlertsScreen() {
  const insets = useSafeAreaInsets();
  const [filter, setFilter] = useState<Filter>('active');

  const visible = useMemo(() => {
    const sorted = sortAlerts(demoAlerts);
    if (filter === 'all') return sorted;
    if (filter === 'active') {
      return sorted.filter((a) => a.state !== 'resolved' && a.state !== 'acked');
    }
    return sorted.filter((a) => a.state === 'resolved' || a.state === 'acked');
  }, [filter]);

  const activeCount = demoAlerts.filter((a) => a.state !== 'resolved' && a.state !== 'acked').length;
  const criticalCount = demoAlerts.filter(
    (a) => a.severity === 'critical' && a.state !== 'resolved' && a.state !== 'acked',
  ).length;

  return (
    <>
      <Stack.Screen options={{ title: 'Алерты', headerShown: false }} />
      <Screen contentStyle={[styles.screen, { paddingTop: insets.top + spacing.md }]}>
        <View style={styles.header}>
          <View style={styles.headerTop}>
            <View style={styles.headerTitles}>
              <AppText variant="display">Алерты</AppText>
              <AppText variant="caption" color={palette.textMuted}>
                дежурный Иван П.
              </AppText>
            </View>
            <Link href="/settings" asChild>
              <Pressable
                accessibilityRole="button"
                accessibilityLabel="Настройки"
                style={({ pressed }) => [styles.iconButton, pressed && styles.iconButtonPressed]}
              >
                <SettingsIcon />
              </Pressable>
            </Link>
          </View>

          <View style={styles.stats}>
            <StatTile value={activeCount} label="активных" tone={palette.text} />
            <StatTile value={criticalCount} label="критичных" tone={palette.critical} />
            <StatTile value={demoAlerts.length} label="всего за сутки" tone={palette.textSecondary} />
          </View>
        </View>

        <View style={styles.filters}>
          {FILTERS.map((item) => {
            const selected = filter === item.key;
            return (
              <Pressable
                key={item.key}
                onPress={() => setFilter(item.key)}
                accessibilityRole="button"
                accessibilityState={{ selected }}
                style={[styles.filterChip, selected && styles.filterChipActive]}
              >
                <AppText
                  variant="caption"
                  color={selected ? palette.textInverse : palette.textSecondary}
                >
                  {item.label}
                </AppText>
              </Pressable>
            );
          })}
        </View>

        {visible.length === 0 ? (
          <EmptyState
            title="Алертов нет"
            hint={
              filter === 'active'
                ? 'Все текущие алерты подтверждены или устранены. Это хороший знак.'
                : 'Закрытых алертов пока не было.'
            }
          />
        ) : (
          visible.map((alert) => (
            <AlertCard
              key={alert.id}
              alert={alert}
              onPress={() => router.push({ pathname: '/alert/[id]', params: { id: alert.id } })}
            />
          ))
        )}

        <Link href="/diagnostics" asChild>
          <Pressable
            accessibilityRole="button"
            style={({ pressed }) => [styles.diagnosticsLink, pressed && { opacity: 0.7 }]}
          >
            <AppText variant="caption" color={palette.textMuted}>
              Диагностика канала →
            </AppText>
          </Pressable>
        </Link>
      </Screen>
    </>
  );
}

function StatTile({ value, label, tone }: { value: number; label: string; tone: string }) {
  return (
    <Card style={styles.statTile}>
      <AppText variant="title" color={tone}>
        {value}
      </AppText>
      <AppText variant="captionMuted" color={palette.textMuted}>
        {label}
      </AppText>
    </Card>
  );
}

const styles = StyleSheet.create({
  screen: { gap: 0 },
  header: { gap: spacing.lg, marginBottom: spacing.xs },
  headerTop: { flexDirection: 'row', alignItems: 'flex-start', justifyContent: 'space-between' },
  headerTitles: { gap: 2, flex: 1 },

  iconButton: {
    width: 44,
    height: 44,
    borderRadius: radius.md,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: palette.surface,
    borderWidth: 1,
    borderColor: palette.border,
  },
  iconButtonPressed: { opacity: 0.7 },

  stats: { flexDirection: 'row', gap: spacing.sm },
  statTile: { flex: 1, padding: spacing.md, gap: 2, borderRadius: radius.md },

  filters: { flexDirection: 'row', gap: spacing.sm, marginBottom: spacing.xs },
  filterChip: {
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: palette.border,
    backgroundColor: palette.bgElevated,
  },
  filterChipActive: { backgroundColor: palette.text, borderColor: palette.text },

  diagnosticsLink: { alignItems: 'center', paddingVertical: spacing.lg },
});
