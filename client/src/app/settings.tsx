/**
 * Настройки дежурного (задача 3.2).
 *
 * Здесь собрано то, что админ настраивает под себя: звук, откладывание,
 * язык, тестовый пейдж. Системные вещи вроде выдачи разрешений живут в
 * диагностике — настройки идут отдельно от разрешений намеренно, потому что
 * это разные по смыслу вещи: настройка это «как я хочу», разрешение это
 * «что разрешил Android».
 */

import { Link, Stack } from 'expo-router';
import { useState } from 'react';
import { Pressable, StyleSheet, Switch, View } from 'react-native';

import { AppText, Button, Card, Chip, Screen, SectionHeader } from '../components/ui';
import { TelegramCard } from '../components/TelegramCard';
import { palette, radius, spacing } from '../theme';

export default function SettingsScreen() {
  const [soundCritical, setSoundCritical] = useState(true);
  const [soundWarning, setSoundWarning] = useState(false);
  const [snoozeEnabled, setSnoozeEnabled] = useState(true);
  const [weeklyTest, setWeeklyTest] = useState(true);

  return (
    <>
      <Stack.Screen options={{ title: 'Настройки' }} />
      <Screen>
        {/* Заголовок «Настройки» уже показывает нативный header, поэтому
            повторять его в контенте не нужно — иначе текст печатается дважды. */}
        <AppText variant="caption" color={palette.textMuted}>
          Иван Петров · дежурный · дежурство до 08:00
        </AppText>

        <SectionHeader title="Уведомления" />
        <Card>
          <ToggleRow
            label="Звук и вибрация для критичных"
            hint="Каналы critical и critical-vip. Если выключить, останется только вибрация — звонок всё равно будет."
            value={soundCritical}
            onChange={setSoundCritical}
          />
          <ToggleRow
            label="Звук для предупреждений"
            hint="Канал warning. По умолчанию выключено: постоянные звуки предупреждений вызывают привыкание."
            value={soundWarning}
            onChange={setSoundWarning}
            last
          />
        </Card>

        <SectionHeader title="Telegram" />
        <Card>
          <TelegramCard />
        </Card>

        <SectionHeader title="Работа с алертами" />
        <Card>
          <ToggleRow
            label="Разрешить откладывание"
            hint="Не работает для критичных. Не больше двух откладываний в час на один алерт."
            value={snoozeEnabled}
            onChange={setSnoozeEnabled}
          />
          <ToggleRow
            label="Еженедельный учебный алерт"
            hint="Раз в неделю приходит фейковый тревожный алерт. Нужен, чтобы вовремя заметить, что уведомления перестали доходить."
            value={weeklyTest}
            onChange={setWeeklyTest}
            last
          />
        </Card>

        <SectionHeader title="Дежурное расписание" />
        <Card>
          <View style={styles.oncallRow}>
            <View style={styles.avatar}>
              <AppText variant="bodyStrong" color={palette.textInverse}>
                ИП
              </AppText>
            </View>
            <View style={styles.oncallInfo}>
              <AppText variant="bodyStrong">Иван Петров</AppText>
              <AppText variant="caption" color={palette.textSecondary}>
                Основной дежурный · сейчас на связи
              </AppText>
            </View>
            <Chip label="дежурный" tone="accent" />
          </View>

          <View style={styles.oncallRow}>
            <View style={[styles.avatar, styles.avatarMuted]}>
              <AppText variant="bodyStrong" color={palette.textSecondary}>
                МК
              </AppText>
            </View>
            <View style={styles.oncallInfo}>
              <AppText variant="bodyStrong">Мария Кузнецова</AppText>
              <AppText variant="caption" color={palette.textSecondary}>
                Запасной · подключается, если не отвечаете
              </AppText>
            </View>
            <Chip label="запасной" />
          </View>
        </Card>

        <SectionHeader title="Язык" />
        <Card>
          <View style={styles.langRow}>
            <LangChip label="RU" active />
            <LangChip label="EN" />
            <LangChip label="DE" />
          </View>
          <AppText variant="captionMuted" color={palette.textMuted}>
            Немецкий в плане: позже. Держим строки в одном файле, чтобы перевод
            не превратился в поиск по коду.
          </AppText>
        </Card>

        <SectionHeader title="Система" />
        <Card>
          <View style={styles.linkRow}>
            <View style={styles.oncallInfo}>
              <AppText variant="bodyStrong">Диагностика канала</AppText>
              <AppText variant="caption" color={palette.textSecondary}>
                Разрешения, каналы, токен. Проверяйте раз в неделю.
              </AppText>
            </View>
          </View>
          <Link href="/diagnostics" asChild>
            <Pressable
              accessibilityRole="button"
              style={({ pressed }) => [styles.diagnosticsButton, pressed && { opacity: 0.7 }]}
            >
              <AppText variant="bodyStrong" color={palette.info}>
                Открыть диагностику
              </AppText>
            </Pressable>
          </Link>
        </Card>

        <AppText variant="captionMuted" color={palette.textMuted} style={styles.footer}>
          Admin-ALert 0.2.0 · dev-сборка
        </AppText>
      </Screen>
    </>
  );
}

function ToggleRow({
  label,
  hint,
  value,
  onChange,
  last = false,
}: {
  label: string;
  hint: string;
  value: boolean;
  onChange: (next: boolean) => void;
  last?: boolean;
}) {
  return (
    <View style={[styles.toggleRow, last && styles.toggleRowLast]}>
      <View style={styles.toggleText}>
        <AppText variant="bodyStrong">{label}</AppText>
        <AppText variant="caption" color={palette.textSecondary} style={styles.toggleHint}>
          {hint}
        </AppText>
      </View>
      <Switch
        value={value}
        onValueChange={onChange}
        trackColor={{ false: palette.border, true: palette.accent }}
        thumbColor={palette.text}
        accessibilityLabel={label}
      />
    </View>
  );
}

function LangChip({ label, active = false }: { label: string; active?: boolean }) {
  return (
    <View style={[styles.langChip, active && styles.langChipActive]}>
      <AppText variant="bodyStrong" color={active ? palette.textInverse : palette.textSecondary}>
        {label}
      </AppText>
    </View>
  );
}

const styles = StyleSheet.create({
  toggleRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.lg,
    paddingVertical: spacing.md,
    borderBottomWidth: 1,
    borderBottomColor: palette.border,
  },
  toggleRowLast: { borderBottomWidth: 0, paddingBottom: 0 },
  toggleText: { flex: 1, gap: 2 },
  toggleHint: { lineHeight: 17 },

  oncallRow: { flexDirection: 'row', alignItems: 'center', gap: spacing.md, paddingVertical: spacing.sm },
  avatar: {
    width: 40,
    height: 40,
    borderRadius: radius.pill,
    backgroundColor: palette.text,
    alignItems: 'center',
    justifyContent: 'center',
  },
  avatarMuted: { backgroundColor: palette.neutralSurface, borderWidth: 1, borderColor: palette.border },
  oncallInfo: { flex: 1, gap: 1 },

  langRow: { flexDirection: 'row', gap: spacing.sm, marginBottom: spacing.md },
  langChip: {
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: palette.border,
    backgroundColor: palette.bgElevated,
  },
  langChipActive: { backgroundColor: palette.text, borderColor: palette.text },

  linkRow: { flexDirection: 'row', alignItems: 'center' },
  diagnosticsButton: { marginTop: spacing.md, paddingVertical: spacing.sm },

  footer: { textAlign: 'center', paddingTop: spacing.xl },
});
