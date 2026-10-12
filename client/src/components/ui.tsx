/**
 * Базовые переиспользуемые компоненты интерфейса.
 *
 * Держатся в одном файле осознанно: компоненты маленькие, живут в одном
 * слое и разбирать их по файлам ради пятидесяти строк кода — избыточно.
 */

import type { ReactNode } from 'react';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
  type StyleProp,
  type TextStyle,
  type ViewStyle,
} from 'react-native';

import { colorForState, palette, radius, spacing, stateColors, typography } from '../theme';
import type { AlertState, Severity } from '../types/alert';

/* ─────────────────────────── Текст ─────────────────────────── */

type TextVariant = keyof typeof typography;

type AppTextProps = {
  children: ReactNode;
  variant?: TextVariant;
  color?: string;
  style?: StyleProp<TextStyle>;
  numberOfLines?: number;
};

export function AppText({ children, variant = 'body', color = palette.text, style, numberOfLines }: AppTextProps) {
  const preset = typography[variant];

  return (
    <Text
      numberOfLines={numberOfLines}
      style={[
        {
          fontSize: preset.fontSize,
          lineHeight: preset.lineHeight,
          fontWeight: preset.fontWeight,
          color,
          ...('letterSpacing' in preset ? { letterSpacing: preset.letterSpacing } : null),
          ...(variant === 'mono' ? { fontFamily: 'monospace' as const } : null),
        },
        style,
      ]}
    >
      {children}
    </Text>
  );
}

/* ─────────────────────────── Кнопка ─────────────────────────── */

type ButtonVariant = 'primary' | 'critical' | 'ghost' | 'outline';

type ButtonProps = {
  label: string;
  onPress?: () => void;
  variant?: ButtonVariant;
  disabled?: boolean;
  loading?: boolean;
  fullWidth?: boolean;
  icon?: ReactNode;
  style?: StyleProp<ViewStyle>;
};

const BUTTON_TONES: Record<ButtonVariant, { bg: string; fg: string; border: string }> = {
  primary: { bg: palette.accent, fg: '#FFFFFF', border: palette.accent },
  critical: { bg: palette.critical, fg: '#1A0505', border: palette.critical },
  outline: { bg: 'transparent', fg: palette.text, border: palette.border },
  ghost: { bg: palette.surfaceHover, fg: palette.textSecondary, border: 'transparent' },
};

export function Button({
  label,
  onPress,
  variant = 'primary',
  disabled = false,
  loading = false,
  fullWidth = false,
  icon,
  style,
}: ButtonProps) {
  const tone = BUTTON_TONES[variant];
  const inactive = disabled || loading;

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ disabled: inactive }}
      onPress={onPress}
      disabled={inactive}
      style={({ pressed }) => [
        styles.button,
        {
          backgroundColor: tone.bg,
          borderColor: tone.border,
          opacity: inactive ? 0.45 : pressed ? 0.82 : 1,
        },
        fullWidth && styles.buttonFull,
        style,
      ]}
    >
      {loading ? (
        <ActivityIndicator color={tone.fg} size="small" />
      ) : (
        <>
          {icon}
          <AppText variant="bodyStrong" color={tone.fg}>
            {label}
          </AppText>
        </>
      )}
    </Pressable>
  );
}

/* ─────────────────────────── Бейджи ─────────────────────────── */

/** Цветной бейдж статуса/уровня. */
export function Badge({
  label,
  fg,
  bg,
  border,
}: {
  label: string;
  fg: string;
  bg: string;
  border: string;
}) {
  return (
    <View style={[styles.badge, { backgroundColor: bg, borderColor: border }]}>
      <AppText variant="caption" color={fg} style={styles.badgeLabel}>
        {label}
      </AppText>
    </View>
  );
}

const STATE_LABELS: Record<AlertState, string> = {
  triggered: 'Новый',
  notifying: 'Ожидает',
  acked: 'Подтверждён',
  escalated: 'Эскалация',
  resolved: 'Устранён',
  expired: 'Истёк',
};

export function StateBadge({ state, severity, isVip }: { state: AlertState; severity: Severity; isVip: boolean }) {
  const color = colorForState(state, severity, isVip);
  const key: keyof typeof stateColors =
    state === 'acked' ? 'acked' : state === 'resolved' || state === 'expired' ? 'resolved' : isVip ? 'vip' : severity;
  const tone = stateColors[key];

  return (
    <Badge label={isVip && state !== 'resolved' && state !== 'acked' ? 'VIP' : STATE_LABELS[state]} fg={color} bg={tone.bg} border={tone.border} />
  );
}

/** Небольшая метка-капсула для второстепенных свойств (профиль, число повторов). */
export function Chip({ label, tone = 'neutral' }: { label: string; tone?: 'neutral' | 'accent' | 'critical' }) {
  const tones = {
    neutral: { fg: palette.textSecondary, bg: palette.neutralSurface, border: palette.border },
    accent: { fg: palette.info, bg: palette.infoSurface, border: palette.infoBorder },
    critical: { fg: palette.critical, bg: palette.criticalSurface, border: palette.criticalBorder },
  }[tone];

  return (
    <View style={[styles.chip, { backgroundColor: tones.bg, borderColor: tones.border }]}>
      <AppText variant="captionMuted" color={tones.fg}>
        {label}
      </AppText>
    </View>
  );
}

/* ─────────────────────────── Каркас ─────────────────────────── */

export function Screen({
  children,
  scroll = true,
  contentStyle,
}: {
  children: ReactNode;
  scroll?: boolean;
  contentStyle?: StyleProp<ViewStyle>;
}) {
  if (!scroll) {
    return <View style={[styles.screen, contentStyle]}>{children}</View>;
  }
  return (
    <ScrollView
      style={styles.screen}
      contentContainerStyle={[styles.screenContent, contentStyle]}
      showsVerticalScrollIndicator={false}
    >
      {children}
    </ScrollView>
  );
}

export function Card({
  children,
  style,
  accent,
  onPress,
}: {
  children: ReactNode;
  style?: StyleProp<ViewStyle>;
  /** Цветная полоса слева — визуальный якорь уровня. */
  accent?: string;
  onPress?: () => void;
}) {
  const body = (
    <View style={[styles.card, accent && { borderLeftColor: accent, borderLeftWidth: 3 }, style]}>
      {children}
    </View>
  );

  if (!onPress) return body;

  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [pressed && styles.cardPressed]}
      accessibilityRole="button"
    >
      {body}
    </Pressable>
  );
}

export function SectionHeader({ title, action }: { title: string; action?: ReactNode }) {
  return (
    <View style={styles.sectionHeader}>
      <AppText variant="captionMuted" color={palette.textMuted} style={styles.sectionHeaderTitle}>
        {title.toUpperCase()}
      </AppText>
      {action}
    </View>
  );
}

export function Divider() {
  return <View style={styles.divider} />;
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <View style={styles.empty}>
      <View style={styles.emptyMark} />
      <AppText variant="heading" color={palette.textSecondary} style={styles.emptyTitle}>
        {title}
      </AppText>
      {hint ? (
        <AppText variant="caption" color={palette.textMuted} style={styles.emptyHint}>
          {hint}
        </AppText>
      ) : null}
    </View>
  );
}

/** Строка «подпись — значение». */
export function FieldRow({ label, value, valueColor, mono }: { label: string; value: string; valueColor?: string; mono?: boolean }) {
  return (
    <View style={styles.fieldRow}>
      <AppText variant="caption" color={palette.textMuted}>
        {label}
      </AppText>
      <AppText variant={mono ? 'mono' : 'body'} color={valueColor ?? palette.text} style={styles.fieldValue}>
        {value}
      </AppText>
    </View>
  );
}

/**
 * Иконка «настройки» из трёх ползунков.
 *
 * Символ ⚙ на Android рендерится системным эмодзи-шрифтом и выглядит как
 * круглая пятнашка, а не как иконка. Рисовать из View дешевле, чем тянуть
 * векторный шрифт ради одной иконки, и результат одинаковый на всех
 * устройствах.
 */
export function SettingsIcon({ size = 20, color = palette.textSecondary }: { size?: number; color?: string }) {
  const barWidth = size;
  const thickness = Math.max(2, Math.round(size / 9));
  const knob = Math.max(4, Math.round(size / 4.5));
  const rows = [
    { knobAt: 0.28 },
    { knobAt: 0.66 },
    { knobAt: 0.42 },
  ];

  return (
    <View style={{ width: barWidth, height: size, justifyContent: 'space-between' }} accessibilityElementsHidden>
      {rows.map((row, index) => (
        <View key={index} style={{ height: thickness, justifyContent: 'center' }}>
          <View style={{ height: thickness, borderRadius: thickness, backgroundColor: color, opacity: 0.55 }} />
          <View
            style={{
              position: 'absolute',
              left: row.knobAt * barWidth - knob / 2,
              width: knob,
              height: knob,
              borderRadius: knob / 2,
              backgroundColor: color,
            }}
          />
        </View>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: palette.bg },
  screenContent: { padding: spacing.lg, paddingBottom: spacing.xxxl, gap: spacing.md },

  button: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.sm,
    minHeight: 48,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
    borderRadius: radius.md,
    borderWidth: 1,
  },
  buttonFull: { alignSelf: 'stretch' },
  buttonPressed: { opacity: 0.8 },

  badge: {
    paddingHorizontal: spacing.sm,
    paddingVertical: 3,
    borderRadius: radius.pill,
    borderWidth: 1,
  },
  badgeLabel: { letterSpacing: 0.3 },

  chip: {
    paddingHorizontal: spacing.sm,
    paddingVertical: 3,
    borderRadius: radius.sm,
    borderWidth: 1,
  },

  card: {
    backgroundColor: palette.surface,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: palette.border,
    padding: spacing.lg,
    gap: spacing.sm,
  },
  cardPressed: { opacity: 0.75 },

  sectionHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginTop: spacing.sm,
    marginBottom: spacing.xs,
  },
  sectionHeaderTitle: { letterSpacing: 1 },

  divider: { height: 1, backgroundColor: palette.border, marginVertical: spacing.sm },

  empty: { alignItems: 'center', paddingVertical: spacing.xxxl, gap: spacing.sm },
  emptyMark: { width: 40, height: 40, borderRadius: radius.pill, borderWidth: 2, borderColor: palette.border, marginBottom: spacing.sm },
  emptyTitle: { textAlign: 'center' },
  emptyHint: { textAlign: 'center', maxWidth: 280 },

  fieldRow: { gap: 2, paddingVertical: spacing.xs },
  fieldValue: { flexShrink: 1 },
});
