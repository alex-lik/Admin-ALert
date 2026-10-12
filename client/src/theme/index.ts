/**
 * Дизайн-токены Admin-ALert.
 *
 * Приложение открывают ночью и в панике, поэтому выбор цветов продиктован не
 * вкусом, а условиями использования:
 *
 * - тёмный фон основной: ночью белый экран бьёт по глазам и ослепляет;
 * - насыщенные цвета только у критичных вещей — если красным горит всё,
 *   красным перестаёт быть важно;
 * - контраст текста выше 7:1, потому что админ читает это на бегу и ночью;
 * - размеры отступов кратны 4 — иначе разъезжаются вертикали в списках.
 */

export const palette = {
  // Фоны, от самого глубокого к приподнятому
  bg: '#0B0F14',
  bgElevated: '#111820',
  surface: '#161E28',
  surfaceHover: '#1D2733',
  border: '#243040',
  borderStrong: '#31405280',

  // Текст
  text: '#E8EEF5',
  textSecondary: '#93A1B2',
  textMuted: '#5F6E80',
  textInverse: '#0B0F14',

  // Семантические состояния
  critical: '#FF453A',
  criticalSurface: '#2A1416',
  criticalBorder: '#5C2226',

  warning: '#FFB020',
  warningSurface: '#2A1F0E',
  warningBorder: '#5C4415',

  vip: '#B57BFF',
  vipSurface: '#1F1630',
  vipBorder: '#422C63',

  success: '#32D74B',
  successSurface: '#0F2415',
  successBorder: '#1F4A2C',

  info: '#5AC8FA',
  infoSurface: '#0E1F2A',
  infoBorder: '#1C4256',

  accent: '#4C8DFF',
  accentSurface: '#101F38',
  accentBorder: '#22406E',

  neutralSurface: '#1A222C',
} as const;

/** Цвет состояния алерта — используется в бейджах, рамках и точках статуса. */
export const stateColors = {
  critical: { fg: palette.critical, bg: palette.criticalSurface, border: palette.criticalBorder },
  warning: { fg: palette.warning, bg: palette.warningSurface, border: palette.warningBorder },
  vip: { fg: palette.vip, bg: palette.vipSurface, border: palette.vipBorder },
  acked: { fg: palette.success, bg: palette.successSurface, border: palette.successBorder },
  resolved: { fg: palette.textMuted, bg: palette.neutralSurface, border: palette.border },
  escalated: { fg: palette.warning, bg: palette.warningSurface, border: palette.warningBorder },
} as const;

/** Отступы кратны 4. */
export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
  xxxl: 48,
} as const;

export const radius = {
  sm: 6,
  md: 10,
  lg: 14,
  xl: 20,
  pill: 999,
} as const;

/**
 * Типографика. sizes — кегль, lineHeight подобран так, чтобы строки
 * не «слипались» на тёмном фоне.
 */
export const typography = {
  display: { fontSize: 30, lineHeight: 36, fontWeight: '700' as const, letterSpacing: -0.5 },
  title: { fontSize: 22, lineHeight: 28, fontWeight: '700' as const, letterSpacing: -0.3 },
  heading: { fontSize: 17, lineHeight: 24, fontWeight: '600' as const },
  body: { fontSize: 15, lineHeight: 22, fontWeight: '400' as const },
  bodyStrong: { fontSize: 15, lineHeight: 22, fontWeight: '600' as const },
  caption: { fontSize: 13, lineHeight: 18, fontWeight: '500' as const },
  captionMuted: { fontSize: 12, lineHeight: 16, fontWeight: '500' as const },
  mono: { fontSize: 13, lineHeight: 19, fontWeight: '400' as const },
} as const;

/**
 * Тени. На тёмном фоне обычная тень почти не видна, поэтому работает в основном
 * обводка, а тень даёт только глубину для приподнятых карточек.
 */
export const elevation = {
  card: {
    shadowColor: '#000',
    shadowOpacity: 0.35,
    shadowRadius: 12,
    shadowOffset: { width: 0, height: 4 },
    elevation: 4,
  },
  sheet: {
    shadowColor: '#000',
    shadowOpacity: 0.5,
    shadowRadius: 24,
    shadowOffset: { width: 0, height: -6 },
    elevation: 16,
  },
} as const;

export type Severity = 'critical' | 'warning';
export type AlertState = 'triggered' | 'notifying' | 'acked' | 'escalated' | 'resolved' | 'expired';

export type StateColorKey = keyof typeof stateColors;

/** Единая точка правды: состояние -> цвета. Иначе появятся «почти красные» оттенки. */
export function colorForState(state: AlertState, severity: Severity, isVip: boolean): string {
  if (isVip && state !== 'resolved' && state !== 'acked') {
    return severity === 'critical' ? stateColors.vip.fg : stateColors.vip.fg;
  }
  switch (state) {
    case 'resolved':
      return stateColors.resolved.fg;
    case 'acked':
      return stateColors.acked.fg;
    case 'expired':
      return stateColors.resolved.fg;
    case 'escalated':
      return stateColors.escalated.fg;
    default:
      return severity === 'critical' ? stateColors.critical.fg : stateColors.warning.fg;
  }
}
