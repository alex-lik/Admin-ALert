/**
 * Корневой layout: тема, шрифты и стек навигации.
 *
 * Тёмная тема здесь не украшение, а требование задачи 3.23: админ открывает
 * приложение ночью и в темноте, светлый фон в такой момент ослепляет.
 */

import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { StyleSheet, View } from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { AppText } from '../components/ui';
import { palette } from '../theme';

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <View style={styles.root}>
        <StatusBar style="light" />
        <Stack
          screenOptions={{
            headerStyle: { backgroundColor: palette.bgElevated },
            headerTintColor: palette.text,
            headerTitleStyle: styles.headerTitle,
            headerShadowVisible: false,
            contentStyle: { backgroundColor: palette.bg },
            animation: 'slide_from_right',
          }}
        >
          {/* На главном экране собственный крупный заголовок, поэтому нативный
              header скрыт — иначе «Алерты» печатается дважды. Остальные экраны
              оставляют нативный header: он сам умеет кнопку «назад» и отступы. */}
          <Stack.Screen name="index" options={{ title: 'Алерты', headerShown: false }} />
          <Stack.Screen name="alert/[id]" options={{ title: 'Алерт' }} />
          <Stack.Screen name="settings" options={{ title: 'Настройки' }} />
          <Stack.Screen name="diagnostics" options={{ title: 'Диагностика' }} />
          <Stack.Screen name="login" options={{ title: 'Вход', headerShown: false }} />
        </Stack>
      </View>
    </SafeAreaProvider>
  );
}

/** Заголовок с подзаголовком — используется на главном экране. */
export function HeaderBlock({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <>
      <AppText variant="display">{title}</AppText>
      {subtitle ? (
        <AppText variant="body" color={palette.textSecondary} style={styles.subtitle}>
          {subtitle}
        </AppText>
      ) : null}
    </>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: palette.bg },
  headerTitle: { fontWeight: '700', fontSize: 17 },
  subtitle: { marginTop: -4, marginBottom: 4 },
});
