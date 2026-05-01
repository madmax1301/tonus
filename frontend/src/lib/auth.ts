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

export function getToken(): string {
  return get(apiToken);
}
