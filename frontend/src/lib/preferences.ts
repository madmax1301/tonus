import { browser } from '$app/environment';
import { writable } from 'svelte/store';

type Location = 'local' | 'navidrome';

const PROVIDER_KEY = 'tonus_default_provider';
const LOCATION_KEY = 'tonus_default_location';

function readString(key: string, fallback: string): string {
  if (!browser) return fallback;
  return localStorage.getItem(key) ?? fallback;
}

function persistedString(key: string, fallback: string) {
  const store = writable<string>(readString(key, fallback));
  if (browser) {
    store.subscribe((v) => {
      if (v) localStorage.setItem(key, v);
      else localStorage.removeItem(key);
    });
  }
  return store;
}

/** "Welcher Provider wird in Suche/Reverse/Album-Detail vorausgewählt?"
 *  Leer = Backend-Default aus /api/metadata/providers wird genommen. */
export const defaultProvider = persistedString(PROVIDER_KEY, '');

/** "Wohin landen Downloads standardmäßig — local oder navidrome?" */
export const defaultLocation = (() => {
  const initial = browser
    ? ((localStorage.getItem(LOCATION_KEY) as Location | null) ?? 'navidrome')
    : 'navidrome';
  const store = writable<Location>(initial);
  if (browser) {
    store.subscribe((v) => localStorage.setItem(LOCATION_KEY, v));
  }
  return store;
})();
