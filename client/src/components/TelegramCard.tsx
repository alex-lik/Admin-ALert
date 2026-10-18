/**
 * Карточка привязки Telegram (задача 4.8).
 *
 * Зачем она в настройках: Telegram — дублирующий канал, и он бесполезен, пока
 * чат не привязан. При этом «чат привязан» и «бот реально доходит до него» —
 * разные вещи: чат можно удалить из бота, заблокировать или просто указать
 * неверный id. Поэтому карточка показывает состояние канала, а не только
 * наличие привязки.
 */

import { useCallback, useEffect, useState } from 'react';
import { StyleSheet, TextInput, View } from 'react-native';

import { api } from '../api/client';
import { AppText, Badge, Button } from './ui';
import { palette, radius, spacing, stateColors } from '../theme';

type Status = {
  bot_configured: boolean;
  chat_id: string | null;
  chat_reachable: boolean | null;
  detail: string | null;
};

type Load =
  | { kind: 'loading' }
  | { kind: 'ready'; status: Status }
  | { kind: 'error'; message: string };

export function TelegramCard() {
  const [load, setLoad] = useState<Load>({ kind: 'loading' });
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');

  const refresh = useCallback(async () => {
    try {
      setLoad({ kind: 'ready', status: await api.telegramStatus() });
    } catch (error) {
      setLoad({ kind: 'error', message: describe(error) });
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const unlink = useCallback(async () => {
    setBusy(true);
    try {
      await api.telegramLink('');
      setEditing(false);
      await refresh();
    } catch (error) {
      setLoad({ kind: 'error', message: describe(error) });
    } finally {
      setBusy(false);
    }
  }, [refresh]);

  const link = useCallback(async () => {
    setBusy(true);
    try {
      await api.telegramLink(draft.trim());
      setEditing(false);
      await refresh();
    } catch (error) {
      setLoad({ kind: 'error', message: describe(error) });
    } finally {
      setBusy(false);
    }
  }, [draft, refresh]);

  if (load.kind === 'loading') {
    return (
      <AppText variant="caption" color={palette.textMuted}>
        проверка канала…
      </AppText>
    );
  }

  if (load.kind === 'error') {
    return (
      <View style={styles.body}>
        <AppText variant="caption" color={palette.critical}>
          канал недоступен: {load.message}
        </AppText>
        <Button label="Повторить" variant="ghost" onPress={refresh} fullWidth />
      </View>
    );
  }

  const { bot_configured: botOk, chat_id: chatId, chat_reachable: reachable, detail } = load.status;

  // Три разных состояния показываем по-разному, потому что и чинить их надо
  // по-разному: нет токена (админ бота), нет привязки (дежурный), бот не
  // доходит (чат удалили или заблокировали).
  const tone = !botOk
    ? stateColors.acked
    : reachable
      ? stateColors.acked
      : stateColors.critical;
  const badge = !botOk ? 'бот не создан' : reachable === null ? 'не привязан' : reachable ? 'подключён' : 'не доходит';

  return (
    <View style={styles.body}>
      <View style={styles.header}>
        <AppText variant="bodyStrong">Резервный канал</AppText>
        <Badge
          label={badge}
          fg={tone.fg}
          bg={tone.bg}
          border={tone.border}
        />
      </View>

      <AppText variant="caption" color={palette.textSecondary}>
        {!botOk
          ? 'Бот не создан. Это делает администратор сервера: токен от @BotFather прописывается в TELEGRAM_BOT_TOKEN.'
          : chatId === null
            ? 'Напишите боту любое сообщение — он покажет ваш chat id. Введите его здесь, и подтверждать алерты можно будет прямо из чата, не открывая приложение.'
            : reachable
              ? `Чат ${chatId} привязан. В нём будут приходить алерты с кнопками «Принял» и «Отложить».`
              : `Чат ${chatId} привязан, но бот не может до него достучаться. Откройте бота и нажмите «Запустить» — возможно, чат удалён или заблокирован.`}
      </AppText>

      {detail && reachable === false ? (
        <AppText variant="mono" color={palette.critical}>
          {detail}
        </AppText>
      ) : null}

      {editing ? (
        <View style={styles.form}>
          <TextInput
            value={draft}
            onChangeText={setDraft}
            placeholder="например, 123456789"
            placeholderTextColor={palette.textMuted}
            keyboardType="number-pad"
            autoCapitalize="none"
            style={styles.input}
            accessibilityLabel="Telegram chat id"
          />
          <View style={styles.buttons}>
            <Button
              label="Отмена"
              variant="ghost"
              onPress={() => setEditing(false)}
              disabled={busy}
            />
            <Button
              label="Привязать"
              variant="outline"
              onPress={link}
              disabled={busy || draft.trim().length === 0}
            />
          </View>
        </View>
      ) : chatId ? (
        <Button label="Отвязать чат" variant="ghost" onPress={unlink} disabled={busy} fullWidth />
      ) : (
        <Button
          label="Указать chat id"
          variant="outline"
          onPress={() => setEditing(true)}
          disabled={busy}
          fullWidth
        />
      )}
    </View>
  );
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

const styles = StyleSheet.create({
  body: { gap: spacing.md, paddingVertical: spacing.md },
  header: { flexDirection: 'row', alignItems: 'center', gap: spacing.md },
  form: { gap: spacing.sm },
  input: {
    borderWidth: 1,
    borderColor: palette.border,
    borderRadius: radius.md,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
    color: palette.text,
    backgroundColor: palette.bgElevated,
    fontSize: 16,
  },
  buttons: { flexDirection: 'row', gap: spacing.sm },
});
