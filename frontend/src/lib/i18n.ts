/**
 * i18n-Foundation für Tonus.
 *
 * Verwendung in Komponenten:
 *
 *   import { t } from '$lib/i18n';
 *   <h1>{$t('library.eyebrow')}</h1>
 *   <p>{$t('import.dropzone.lines_ready', { count: 42 })}</p>
 *
 * Sprache umschalten:
 *
 *   import { lang } from '$lib/i18n';
 *   lang.set('en');
 *
 * Persistenz: lang wird in localStorage als `tonus_lang` gespeichert
 * und beim nächsten Page-Load automatisch wiederhergestellt. Default
 * ist DE; falls Browser-Locale englisch ist und kein gespeicherter
 * Wert existiert, wird EN als initialer Wert genommen.
 */

import { browser } from '$app/environment';
import { writable, derived } from 'svelte/store';
import { strings, type Lang, type StringKey } from './i18n/strings';

const STORAGE_KEY = 'tonus_lang';

function readInitialLang(): Lang {
  if (!browser) return 'de';
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === 'de' || stored === 'en') return stored;
  // Fallback: Browser-Locale auswerten. navigator.language liefert z.B.
  // "en-US", "de-DE" — wir splitten auf "-" und nehmen das Sprachkürzel.
  const browserLang = (navigator.language ?? 'de').split('-')[0];
  if (browserLang === 'en') return 'en';
  return 'de';
}

export const lang = writable<Lang>(readInitialLang());

if (browser) {
  lang.subscribe((v) => {
    try {
      localStorage.setItem(STORAGE_KEY, v);
    } catch {
      /* Quota / private mode — silently ignore */
    }
    // <html lang="…"> mit aktualisieren, damit Browser-Features
    // (Hyphenation, Spell-Check, AT) die richtige Sprache kennen.
    if (typeof document !== 'undefined') {
      document.documentElement.setAttribute('lang', v);
    }
  });
}

export type { Lang, StringKey };

/**
 * Reactive Translation-Store. Liefert eine Funktion `(key, params?) => string`,
 * die sich automatisch updatet wenn `lang` umgeschaltet wird.
 *
 * Fallback-Kette pro Lookup:
 *   1. strings[$lang][key]
 *   2. strings.de[key]  (DE als Reference, immer vollständig)
 *   3. key selbst       (sichtbar als Bug-Marker)
 */
export const t = derived(lang, ($lang) => {
  return (key: StringKey, params?: Record<string, string | number>): string => {
    let s: string =
      (strings[$lang] as Record<string, string>)[key] ??
      (strings.de as Record<string, string>)[key] ??
      key;
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        s = s.replace(new RegExp(`\\{${k}\\}`, 'g'), String(v));
      }
    }
    return s;
  };
});
