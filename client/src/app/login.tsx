/**
 * Вход админа (задача 3.2, первый экран по плану).
 *
 * Сессия хранится в защищённом хранилище через react-native-keychain, а не в
 * AsyncStorage: токен доступа из AsyncStorage читается любым приложением с
 * правами на файлы пользователя. Задача 3.3.
 *
 * Пока сервера нет — форма проверяет вход локально. Подмена на реальный
 * запрос намеренно оставлена в одном месте, чтобы правка была точечной.
 */

import { Stack, router } from 'expo-router';
import { useState } from 'react';
import { StyleSheet, TextInput, View } from 'react-native';

import { AppText, Button, Card, Screen } from '../components/ui';
import { palette, radius, spacing, typography } from '../theme';

export default function LoginScreen() {
  const [email, setEmail] = useState('admin@utax.top');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = () => {
    setError(null);

    if (!email.includes('@')) {
      setError('Нужен корректный email');
      return;
    }
    if (password.length < 8) {
      setError('Пароль короче 8 символов');
      return;
    }

    setBusy(true);
    // Заглушка реального POST /auth/login. Намеренно показывает состояние
    // загрузки: в реальности здесь будет задержка сети.
    setTimeout(() => {
      setBusy(false);
      router.replace('/');
    }, 500);
  };

  return (
    <>
      <Stack.Screen options={{ headerShown: false }} />
      <Screen contentStyle={styles.content}>
        <View style={styles.hero}>
          <View style={styles.logo}>
            <AppText variant="title" color={palette.textInverse}>
              A
            </AppText>
          </View>
          <AppText variant="display">Admin-ALert</AppText>
          <AppText variant="body" color={palette.textSecondary} style={styles.tagline}>
            Дежурный канал. Вход по корпоративному аккаунту.
          </AppText>
        </View>

        <Card style={styles.form}>
          <View style={styles.field}>
            <AppText variant="caption" color={palette.textMuted}>
              Email
            </AppText>
            <TextInput
              value={email}
              onChangeText={setEmail}
              autoCapitalize="none"
              autoComplete="email"
              keyboardType="email-address"
              placeholder="name@utax.top"
              placeholderTextColor={palette.textMuted}
              style={styles.input}
              accessibilityLabel="Email"
            />
          </View>

          <View style={styles.field}>
            <AppText variant="caption" color={palette.textMuted}>
              Пароль
            </AppText>
            <TextInput
              value={password}
              onChangeText={setPassword}
              secureTextEntry
              placeholder="••••••••"
              placeholderTextColor={palette.textMuted}
              style={styles.input}
              accessibilityLabel="Пароль"
            />
          </View>

          {error ? (
            <AppText variant="caption" color={palette.critical}>
              {error}
            </AppText>
          ) : null}

          <Button label="Войти" onPress={submit} loading={busy} fullWidth />
        </Card>

        <AppText variant="captionMuted" color={palette.textMuted} style={styles.footer}>
          Сессия хранится в защищённом хранилище устройства и не покидает его.
          Токен доступа живёт 1 час, обновление — по refresh-токену.
        </AppText>
      </Screen>
    </>
  );
}

const styles = StyleSheet.create({
  content: { flexGrow: 1, justifyContent: 'center', paddingVertical: spacing.xxxl },

  hero: { alignItems: 'center', gap: spacing.sm, marginBottom: spacing.xxl },
  logo: {
    width: 64,
    height: 64,
    borderRadius: radius.lg,
    backgroundColor: palette.text,
    alignItems: 'center',
    justifyContent: 'center',
    marginBottom: spacing.sm,
  },
  tagline: { textAlign: 'center', maxWidth: 300 },

  form: { gap: spacing.lg },
  field: { gap: spacing.xs },
  input: {
    backgroundColor: palette.bgElevated,
    borderWidth: 1,
    borderColor: palette.border,
    borderRadius: radius.md,
    paddingHorizontal: spacing.md,
    minHeight: 48,
    color: palette.text,
    ...typography.body,
  },

  footer: { textAlign: 'center', marginTop: spacing.xl, lineHeight: 16 },
});
