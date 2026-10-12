/**
 * Config plugin: добавляет maven-репозиторий Notifee в сгенерированный
 * проект Android.
 *
 * Зачем: Android-ядро Notifee (`app.notifee:core`) лежит не в Google Maven
 * и не в Maven Central, а в комплектном репозитории внутри пакета —
 * `node_modules/@notifee/react-native/android/libs`. Сам Notifee объявляет
 * этот репозиторий через `rootProject.allprojects` в собственном build.gradle,
 * но в проектах с Continuous Native Generation этот блок не доезжает до `:app`,
 * и сборка падает с:
 *
 *   Could not find any matches for app.notifee:core:+
 *
 * Править `android/build.gradle` руками нельзя — он перезаписывается при
 * каждом `npx expo prebuild`. Поэтому правим его отсюда.
 *
 * Используется локальный репозиторий из node_modules, а не сетевой
 * https://maven.notifee.com: сборка не зависит от доступности внешнего Maven
 * и работает оффлайн.
 */

const { withProjectBuildGradle } = require('expo/config-plugins');

const REPOSITORY = 'maven { url "$rootDir/../node_modules/@notifee/react-native/android/libs" }';

/** @type {import('@expo/config-plugins').ConfigPlugin} */
module.exports = function withNotifeeMavenRepository(config) {
  return withProjectBuildGradle(config, (modConfig) => {
    const { contents, language } = modConfig.modResults;

    if (language !== 'groovy') {
      // Kotlin DSL: не трогаем, чтобы не сломать сборку. Проект использует Groovy.
      return modConfig;
    }

    // Уже добавлен (или добавлен вручную) — идемпотентность.
    if (contents.includes('@notifee/react-native/android/libs')) {
      return modConfig;
    }

    const marker = 'allprojects {';
    const index = contents.indexOf(marker);

    if (index === -1) {
      throw new Error(
        'withNotifeeMavenRepository: в android/build.gradle не найден блок allprojects {}. ' +
          'Структура шаблона изменилась — плагин нужно обновить.',
      );
    }

    // Вставляем репозиторий в самое начало allprojects.repositories,
    // чтобы он проверялся первым и не уходил в сеть.
    const repositoriesMarker = 'repositories {';
    const repositoriesIndex = contents.indexOf(repositoriesMarker, index);

    if (repositoriesIndex === -1) {
      throw new Error(
        'withNotifeeMavenRepository: в блоке allprojects не найден repositories {}.',
      );
    }

    const insertAt = repositoriesIndex + repositoriesMarker.length;
    modConfig.modResults.contents =
      contents.slice(0, insertAt) + `\n    ${REPOSITORY}` + contents.slice(insertAt);

    return modConfig;
  });
};
