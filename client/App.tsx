/**
 * Admin-ALert — экран диагностики (задача 3.13).
 *
 * Существует не для красоты: по плану критичные нюансы Android
 * (bypassDnd, точные будильники, оптимизация батареи) ломаются
 * по-разному на Pixel, Samsung, Xiaomi и Huawei. Этот экран позволяет
 * админу за 10 секунд увидеть, что у него сломано, до того как
 * реальный алерт не дошёл.
 */

import { useCallback, useEffect, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { SafeAreaView } from 'react-native-safe-area-context';

import {
  ChannelId,
  cancelEscalation,
  ensureChannels,
  getPermissionStatus,
  requestPermission,
  scheduleEscalation,
  sendTestAlert,
  subscribeToActions,
  type DiagnosticsEntry,
} from './src/notifications/channels';
import { fetchToken, subscribeToMessaging } from './src/notifications/fcm';

type Line = { label: string; value: string; ok: boolean | null };

export default function App() {
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
      next.push({ label: 'Разрешение на уведомления', value: permission, ok: !permission.includes('ЗАПРЕЩЕНО') });

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

    const unsubscribe = subscribeToActions((id, pressAction) => {
      append(`действие ${pressAction} для ${id}`);
      if (pressAction === 'ack') {
        cancelEscalation(id).then((n) => append(`эскалация отменена: ${n}`));
      }
    });

    let unsubscribeMessaging: (() => void) | undefined;
    try {
      unsubscribeMessaging = subscribeToMessaging({
        onToken: (token) => append(`токен обновлён: ${token.slice(0, 20)}…`),
        onMessage: (title, body) => append(`push: ${title} — ${body}`),
        onError: (error) => append(`FCM ошибка: ${error}`),
      });
    } catch {
      // Firebase без настроек не инициализируется — это не должно ронять экран.
    }

    return () => {
      unsubscribe();
      unsubscribeMessaging?.();
    };
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

  const testEscalation = useCallback(async () => {
    setBusy(true);
    const alertId = `test-${Date.now()}`;
    const result = await scheduleEscalation(alertId);
    append(`эскалация ${alertId}: ${result.join(', ')}`);
    setBusy(false);
  }, [append]);

  return (
    <SafeAreaView style={styles.root}>
      <StatusBar style="light" />
      <ScrollView contentContainerStyle={styles.content}>
        <Text style={styles.title}>Admin-ALert</Text>
        <Text style={styles.subtitle}>Диагностика дежурного канала</Text>

        <Section title="Состояние">
          {lines.map((line) => (
            <View key={line.label} style={styles.row}>
              <Text style={styles.label}>{line.label}</Text>
              <Text style={[styles.value, line.ok === false && styles.bad, line.ok && styles.good]}>
                {line.value}
              </Text>
            </View>
          ))}
          {lines.length === 0 && <Text style={styles.value}>проверка…</Text>}
        </Section>

        <Section title="Действия">
          <Button title="Повторить проверку" onPress={runDiagnostics} disabled={busy} />
          <Button title="Запросить разрешения" onPress={askPermission} disabled={busy} />
          <Button
            title="Тест: предупреждение"
            onPress={() => testAlert(ChannelId.warning)}
            disabled={busy}
          />
          <Button
            title="Тест: критичный (обходит DND)"
            onPress={() => testAlert(ChannelId.critical)}
            disabled={busy}
          />
          <Button
            title="Тест: критичный VIP"
            onPress={() => testAlert(ChannelId.criticalVip)}
            disabled={busy}
          />
          <Button title="Тест: эскалация 1/2/3 мин" onPress={testEscalation} disabled={busy} />
        </Section>

        <Section title="Журнал">
          {log.map((entry, index) => (
            <Text key={`${entry}-${index}`} style={styles.log}>
              {entry}
            </Text>
          ))}
          {log.length === 0 && <Text style={styles.value}>пусто</Text>}
        </Section>
      </ScrollView>
    </SafeAreaView>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>{title}</Text>
      {children}
    </View>
  );
}

function Button({
  title,
  onPress,
  disabled,
}: {
  title: string;
  onPress: () => void;
  disabled?: boolean;
}) {
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      style={({ pressed }) => [styles.button, pressed && styles.buttonPressed, disabled && styles.buttonDisabled]}
    >
      <Text style={styles.buttonText}>{title}</Text>
    </Pressable>
  );
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: '#0B0F14' },
  content: { padding: 20, paddingBottom: 48 },
  title: { color: '#E6EDF3', fontSize: 28, fontWeight: '700' },
  subtitle: { color: '#7D8590', fontSize: 15, marginBottom: 24, marginTop: 2 },
  section: { marginBottom: 28 },
  sectionTitle: {
    color: '#7D8590',
    fontSize: 12,
    fontWeight: '700',
    letterSpacing: 1,
    textTransform: 'uppercase',
    marginBottom: 12,
  },
  row: { marginBottom: 10 },
  label: { color: '#7D8590', fontSize: 13 },
  value: { color: '#E6EDF3', fontSize: 14, marginTop: 2 },
  good: { color: '#3FB950' },
  bad: { color: '#F85149' },
  button: {
    backgroundColor: '#1F2933',
    borderRadius: 8,
    paddingVertical: 14,
    paddingHorizontal: 16,
    marginBottom: 10,
    borderWidth: 1,
    borderColor: '#2D3748',
  },
  buttonPressed: { backgroundColor: '#2D3748' },
  buttonDisabled: { opacity: 0.5 },
  buttonText: { color: '#E6EDF3', fontSize: 15, fontWeight: '600' },
  log: { color: '#8B949E', fontSize: 13, marginBottom: 6, fontFamily: 'monospace' },
});
