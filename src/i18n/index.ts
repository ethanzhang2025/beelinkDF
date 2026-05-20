// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import { en, zh } from './locales';

const resources = {
  en: { translation: en },
  zh: { translation: zh },
};

// 智能问数：固定中文，不做浏览器语言探测。保留 en 资源以便未来恢复。
// 覆盖既有 localStorage 中可能残留的 en 选择，避免老用户首屏仍是英文。
try {
  if (typeof window !== 'undefined' && window.localStorage) {
    window.localStorage.setItem('i18nextLng', 'zh');
  }
} catch { /* localStorage 不可用时静默忽略 */ }

i18n
  .use(initReactI18next)
  .init({
    resources,
    lng: 'zh',
    fallbackLng: 'zh',
    interpolation: {
      escapeValue: false,
    },
  });

export default i18n;
