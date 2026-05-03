/**
 * Phase F.2: Auth-State-Manager.
 *
 * Drei Token-Quellen werden vom Frontend unterstützt — in dieser Reihenfolge
 * von api.ts probiert:
 *
 *   1. **JWT Access-Token** (typisch nach Browser-Login):
 *      - in `accessToken` Store, persistiert in localStorage `tonus_jwt_access`
 *      - bei 401 versucht api.ts automatisch einen Refresh über `refreshToken`
 *      - bei Erfolg läuft der Request transparent weiter
 *      - bei Fail → redirect to /login (logoutLocal())
 *
 *   2. **Manueller API-Token** (Legacy oder PAT-Direkteingabe):
 *      - in `apiToken` Store, persistiert in `tonus_api_token`
 *      - wird gepostet wenn KEIN accessToken da ist
 *      - dient für Plugin/CLI-Use-Cases die manuell ein Token eintragen
 *
 *   3. **Setup-Mode**: gar kein Token nötig, Server lässt durch wenn 0 User
 *      in DB UND kein Legacy-TONUS_API_TOKEN gesetzt ist.
 *
 * `currentUser` enthält den aktuellen User-Record nach erfolgreichem Login
 * oder /api/auth/me-Call. Layout-Guard nutzt das um login/logout-States zu
 * dirigieren.
 */
import { browser } from '$app/environment';
import { writable, get, derived } from 'svelte/store';

const ACCESS_KEY = 'tonus_jwt_access';
const REFRESH_KEY = 'tonus_jwt_refresh';
const LEGACY_TOKEN_KEY = 'tonus_api_token';

function readLs(key: string): string {
  if (!browser) return '';
  try {
    return localStorage.getItem(key) ?? '';
  } catch {
    return '';
  }
}

function writeLs(key: string, value: string): void {
  if (!browser) return;
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch {
    /* private mode / quota — silent */
  }
}

// JWT-Access-Token (~15 min lebenszeit)
export const accessToken = writable<string>(readLs(ACCESS_KEY));
if (browser) accessToken.subscribe((v) => writeLs(ACCESS_KEY, v));

// JWT-Refresh-Token (~30 d lebenszeit)
export const refreshToken = writable<string>(readLs(REFRESH_KEY));
if (browser) refreshToken.subscribe((v) => writeLs(REFRESH_KEY, v));

// Legacy/PAT-Token (manuelle Eingabe). Bleibt erhalten für rückwärtskompatible
// Konfigurationen und Plugin/CLI-Use-Cases.
export const apiToken = writable<string>(readLs(LEGACY_TOKEN_KEY));
if (browser) apiToken.subscribe((v) => writeLs(LEGACY_TOKEN_KEY, v));

// Aktueller User nach Login. Wird vom Layout via /api/auth/me oder direkt
// nach Login gesetzt. null = niemand authenticiert.
export type CurrentUser = {
  id: number;
  username: string;
  is_admin: boolean;
  totp_enabled: boolean;
  auth_method?: string;
};
export const currentUser = writable<CurrentUser | null>(null);

// True wenn API-Calls mit Auth-Header gehen sollten — hat entweder JWT oder
// Manual-Token.
export const isAuthenticated = derived(
  [accessToken, apiToken],
  ([$a, $t]) => !!$a || !!$t,
);

/** Aktiver Token für outgoing API-Calls. JWT bevorzugt, dann Legacy/PAT. */
export function getActiveToken(): string {
  return get(accessToken) || get(apiToken);
}

/** Token-Lookup für Bearer-Header — wird in api.ts aufgerufen. */
export function getToken(): string {
  return getActiveToken();
}

/** Schreibt JWT-Pair nach Login/Setup/Refresh in den Store + localStorage. */
export function setJwtPair(pair: { access: string; refresh: string }): void {
  accessToken.set(pair.access);
  refreshToken.set(pair.refresh);
}

/** Lokales Logout — Tokens und User aus dem Browser entfernen. KEIN Server-
 *  Call (Server-Logout muss separat über authApi.logout() laufen). */
export function logoutLocal(): void {
  accessToken.set('');
  refreshToken.set('');
  currentUser.set(null);
}

/** Setze Legacy-Token (manuelle Eingabe via Settings oder TokenSheet). */
export function setLegacyToken(value: string): void {
  apiToken.set(value.trim());
}

// ── Compat-Layer fürs alte TokenSheet ─────────────────────────────────
//
// Vorher öffnete eine 401 das TokenSheet. Mit Phase F.2 wechseln wir zu
// einem Login-Screen, aber das TokenSheet bleibt als manuelle Token-
// Eingabe in den Settings — z.B. wenn jemand per PAT-Direkteingabe
// arbeitet statt mit Login. Layout/Routing-Guard dirigiert die normale
// 401-Handling.
export const authChallengeOpen = writable<boolean>(false);
export function challengeAuth(): void {
  authChallengeOpen.set(true);
}
export function dismissChallenge(): void {
  authChallengeOpen.set(false);
}
