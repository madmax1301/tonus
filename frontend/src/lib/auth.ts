import { browser } from '$app/environment';
import { writable, get } from 'svelte/store';

const TOKEN_KEY = 'tonus_api_token';

function readInitial(): string {
  if (!browser) return '';
  return localStorage.getItem(TOKEN_KEY) ?? '';
}

export const apiToken = writable<string>(readInitial());

if (browser) {
  apiToken.subscribe((value) => {
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  });
}

/** True wenn ein API-Call gerade mit 401 fehlgeschlagen ist und Auth-UI geöffnet werden soll. */
export const authChallengeOpen = writable<boolean>(false);

export function getToken(): string {
  return get(apiToken);
}

export function challengeAuth(): void {
  authChallengeOpen.set(true);
}

export function dismissChallenge(): void {
  authChallengeOpen.set(false);
}
