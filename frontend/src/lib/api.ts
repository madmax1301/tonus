import { base } from '$app/paths';
import { get } from 'svelte/store';
import {
  getToken,
  accessToken,
  refreshToken,
  apiToken,
  setJwtPair,
  logoutLocal,
  challengeAuth
} from './auth';

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public body?: unknown
  ) {
    super(message);
  }
}

// In-flight-Refresh dedupliziert parallele 401s — wenn 5 API-Calls
// gleichzeitig 401 bekommen, soll nur 1 Refresh-Call laufen, nicht 5.
let inflightRefresh: Promise<boolean> | null = null;

async function tryRefresh(): Promise<boolean> {
  const refresh = get(refreshToken);
  if (!refresh) return false;
  if (inflightRefresh) return inflightRefresh;
  inflightRefresh = (async () => {
    try {
      const url = `${base}/api/auth/refresh`;
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refresh })
      });
      if (!resp.ok) {
        // Refresh fehlgeschlagen → komplett ausloggen, Layout-Guard
        // redirected zu /login.
        logoutLocal();
        return false;
      }
      const data = (await resp.json()) as { tokens: { access: string; refresh: string } };
      setJwtPair(data.tokens);
      return true;
    } catch {
      logoutLocal();
      return false;
    } finally {
      inflightRefresh = null;
    }
  })();
  return inflightRefresh;
}

async function request<T>(path: string, init: RequestInit = {}, _retry = false): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  const url = `${base}${path.startsWith('/') ? path : `/${path}`}`;
  const resp = await fetch(url, { ...init, headers });
  if (!resp.ok) {
    // 401 + Access-Token vorhanden + nicht schon ein Retry → Refresh +
    // einmal wiederholen. Nur wenn ein Refresh-Token da ist; sonst direkt
    // durchfallen lassen damit der Layout-Guard zum Login redirected.
    if (resp.status === 401 && !_retry && get(accessToken) && get(refreshToken)) {
      const ok = await tryRefresh();
      if (ok) return request<T>(path, init, true);
    }
    if (resp.status === 401 || resp.status === 403) {
      // TokenSheet-Challenge nur im Legacy/PAT-Mode triggern (User hat
      // manuelles Token gesetzt, aber keinen JWT-Login). Im JWT-Mode soll
      // der Layout-Guard via goto('/login') übernehmen — Login-Form zeigt
      // dann ggf. das TOTP-Feld an. Während wir bereits auf der Login-
      // Route sind (kein accessToken, kein apiToken) → KEIN TokenSheet,
      // sonst öffnet's beim Submit-401 auf der Login-Page selbst.
      if (get(apiToken) && !get(accessToken)) {
        challengeAuth();
      }
    }
    // Erst Text lesen, dann optional als JSON parsen — wenn der erste
    // .json() fehlschlägt (z.B. bei HTML-Traceback in einer FastAPI-500),
    // ist der ReadableStream bereits konsumiert und ein zweiter .text()-
    // Call wirft "body stream already read". Sicher: einmal lesen, dann
    // entscheiden.
    const raw = await resp.text();
    let body: unknown = raw;
    try {
      body = JSON.parse(raw);
    } catch {
      /* nicht-JSON Response — body bleibt der raw-Text */
    }
    throw new ApiError(resp.status, `${resp.status} ${resp.statusText}`, body);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: 'POST',
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined
    }),
  health: () => request<{ status: string }>('/api/health')
};

// ── Auth-Endpoints (Phase F.2) ─────────────────────────────────────
export interface AuthSetupStatus {
  setup_required: boolean;
  auth_active: boolean;
  legacy_token_active: boolean;
}
export interface AuthUser {
  id: number;
  username: string;
  is_admin: boolean;
  totp_enabled: boolean;
  auth_method?: string;
  last_login_at_ms?: number | null;
}
export interface AuthTokens {
  access: string;
  refresh: string;
  access_expires_at: number;
  refresh_expires_at: number;
}
export interface AuthLoginResponse {
  user: AuthUser;
  tokens: AuthTokens;
}
export interface AuthSetupResponse extends AuthLoginResponse {
  totp_secret?: string | null;
  totp_uri?: string | null;
  /** Server-rendered QR-PNG (data-URL). Bevorzugt vor totp_uri durch externen Service. */
  totp_qr_data_url?: string | null;
}

export const authApi = {
  /** Public — fetcht ob ein Initial-Setup-Wizard nötig ist. */
  setupStatus: () => request<AuthSetupStatus>('/api/auth/setup-status'),
  /** Public — bootstrappt den ersten Admin. */
  setup: (username: string, password: string, enable_totp = false) =>
    request<AuthSetupResponse>('/api/auth/setup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, enable_totp })
    }),
  /** Public — Username + Password (+ TOTP) → JWT pair. */
  login: (username: string, password: string, totp_code?: string) =>
    request<AuthLoginResponse>('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, totp_code })
    }),
  /** Auth — generiert ein frisches TOTP-Secret + provisioning-URI + QR-PNG
   *  (data-URL, server-rendered) fürs nachträgliche 2FA-Setup aus Settings.
   *  Wird NICHT persistiert — totpConfirm() macht das nach erfolgreichem
   *  Code-Verify. Server-side QR vermeidet Secret-Leak an externe Dienste. */
  totpInit: () =>
    request<{ secret: string; uri: string; qr_data_url: string }>('/api/auth/totp-init', {
      method: 'POST'
    }),
  /** Auth — verifiziert den ersten TOTP-Code beim Setup. Bei Erfolg wird
   *  das Secret scharf in der DB gespeichert. */
  totpConfirm: (secret: string, code: string) =>
    request<{ ok: boolean }>('/api/auth/totp-confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ secret, code })
    }),
  /** Auth — deaktiviert TOTP. Braucht Password-Re-Verify + (wenn aktiv) Code,
   *  damit ein geklauter JWT allein nicht 2FA aushebeln kann. */
  totpDisable: (password: string, totp_code?: string) =>
    request<{ ok: boolean }>('/api/auth/totp-disable', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password, totp_code })
    }),
  /** Auth — current user info, oder 401 wenn Session expired. */
  me: () => request<AuthUser>('/api/auth/me'),
  /** Auth — server-seitig Refresh-Token revoken. */
  logout: (refresh_token?: string) =>
    request<{ ok: boolean; revoked: number }>('/api/auth/logout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token })
    }),
  /** Auth — Liste aller PATs des eingeloggten Users (ohne Plain-Token). */
  patsList: () => request<{ pats: Pat[] }>('/api/auth/pats'),
  /** Auth — Erstellt PAT. Plain-Token im Response NUR diesmal sichtbar. */
  patsCreate: (name: string, expires_in_days?: number | null) =>
    request<PatCreateResponse>('/api/auth/pats', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, expires_in_days: expires_in_days ?? null })
    }),
  /** Auth — Widerruft PAT (hard delete). 404 wenn nicht ownership. */
  patsRevoke: (pat_id: number) =>
    request<{ ok: boolean }>(`/api/auth/pats/${pat_id}`, { method: 'DELETE' }),
  /** Admin-only — Liste aller lifetime-gebannten IPs. */
  bansList: () => request<{ banned: BannedIp[] }>('/api/auth/banned-ips'),
  /** Admin-only — Hebt einen Lifetime-Ban auf. IP wird URL-encoded ans Path-
   *  Parameter gehängt (kann IPv6 mit Doppelpunkten sein). */
  bansUnban: (ip: string) =>
    request<{ ok: boolean }>(`/api/auth/banned-ips/${encodeURIComponent(ip)}`, {
      method: 'DELETE'
    }),
  /** Admin-only — Liste aller User mit is_admin/totp/last-login. */
  usersList: () => request<{ users: ManagedUser[] }>('/api/auth/users'),
  /** Admin-only — Legt neuen User an. */
  usersCreate: (username: string, password: string, is_admin = false) =>
    request<{ user: ManagedUser }>('/api/auth/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, is_admin })
    }),
  /** Admin-only — Hard-Delete (cascades PATs + refresh_tokens). */
  usersDelete: (user_id: number) =>
    request<{ ok: boolean }>(`/api/auth/users/${user_id}`, { method: 'DELETE' }),
  /** Admin-only — Toggle is_admin oder reset Password. */
  usersPatch: (user_id: number, body: { is_admin?: boolean; password?: string }) =>
    request<{ ok: boolean }>(`/api/auth/users/${user_id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    })
};

export interface ManagedUser {
  id: number;
  username: string;
  is_admin: boolean;
  totp_enabled: boolean;
  created_at_ms: number;
  last_login_at_ms?: number | null;
}

export interface BannedIp {
  ip: string;
  reason: string | null;
  banned_at_ms: number;
  failed_count: number;
}

export interface Pat {
  id: number;
  name: string;
  prefix: string;
  scopes?: string | null;
  created_at_ms: number;
  last_used_at_ms?: number | null;
  expires_at_ms?: number | null;
}

export interface PatCreateResponse {
  id: number;
  name: string;
  prefix: string;
  /** Plain-Token — NUR in dieser einen Antwort verfügbar. Sofort dem User zeigen, dann verwerfen. */
  token: string;
  expires_at_ms?: number | null;
}

export interface QueueJobPayload {
  kind?: 'url';
  track?: { name?: string; artist?: string; album?: string; album_art?: string };
  album_id?: string;
  album_name?: string;
  location?: 'local' | 'navidrome';
  navidrome_library_path?: string;
  output_format?: string;
  audio_quality?: string;
  /** plugin-sync markers — set when track was queued via /api/plugin/sync */
  plugin_sync_run_id?: string;
  plugin_sync_playlist_name?: string;
  plugin_sync_navidrome_user?: string;
  /** url-direct download */
  url?: string;
}

export interface QueueJob {
  job_id: string;
  status: 'queued' | 'processing' | 'completed' | 'error';
  stage?: string;
  progress?: number;
  message?: string;
  payload?: QueueJobPayload;
}

export interface QueueResponse {
  items: QueueJob[];
  total: number;
  shown: number;
  status_counts: Partial<Record<QueueJob['status'], number>>;
}

export interface Track {
  id: string;
  name: string;
  artist: string;
  artists: string[];
  album: string;
  duration_ms: number;
  external_url: string;
  preview_url?: string;
  album_art?: string;
  release_date: string;
  track_number?: number;
  disc_number?: number;
}

export interface Album {
  id: string;
  name: string;
  artist: string;
  artists: string[];
  release_date: string;
  total_tracks: number;
  album_art?: string;
  external_url: string;
}

export interface MetadataProvider {
  id: string;
  label: string;
  configured: boolean;
}

export interface MetadataProvidersResponse {
  default: string;
  providers: MetadataProvider[];
}

export interface LaneInfo {
  name: string;
  ready_at_ms: number;
  remaining_ms: number;
}

export interface LaneStatusResponse {
  lanes: LaneInfo[];
  next_ready_in_ms: number;
  cooldown: {
    normal_seconds: [number, number];
    rate_limited_seconds: [number, number];
  };
}

export const queueApi = {
  list: (status?: string) => {
    const q = status ? `?status=${encodeURIComponent(status)}` : '';
    return api.get<QueueResponse>(`/api/queue${q}`);
  },
  lanes: () => api.get<LaneStatusResponse>('/api/queue/lanes'),
  retryAll: () => api.post<{ ok: boolean; retried: number }>('/api/queue/retry-all-errors'),
  clear: (status: 'completed' | 'error' | 'queued' | 'all' = 'completed') =>
    api.post<{ ok: boolean; deleted: number }>(`/api/queue/clear?status=${status}`),
  cancel: (jobId: string) => api.post<{ ok: boolean }>(`/api/queue/${jobId}/cancel`),
  retry: (jobId: string) => api.post<{ ok: boolean }>(`/api/queue/${jobId}/start`)
};

export const searchApi = {
  tracks: (query: string, provider?: string, limit = 20) =>
    api.post<Track[]>('/api/search', { query, provider, limit }),
  albums: (query: string, provider?: string, limit = 20) =>
    api.post<Album[]>('/api/search/albums', { query, provider, limit })
};

export interface AlbumDetail extends Album {
  cover?: string;
  tracks: Track[];
  genres?: string[];
}

export const albumApi = {
  get: (albumId: string, provider?: string) => {
    const q = provider ? `?provider=${encodeURIComponent(provider)}` : '';
    return api.get<AlbumDetail>(`/api/album/${encodeURIComponent(albumId)}${q}`);
  }
};

export interface DownloadOpts {
  location?: 'local' | 'navidrome';
  provider?: string;
  format?: string;
  quality?: string;
}

export const downloadApi = {
  start: (trackId: string, opts: DownloadOpts = {}) =>
    api.post<{ status: string; message: string }>('/api/download', {
      track_id: trackId,
      location: opts.location ?? 'navidrome',
      provider: opts.provider,
      format: opts.format,
      quality: opts.quality
    }),
  album: (albumId: string, opts: DownloadOpts = {}) =>
    api.post<{ message: string; total_tracks?: number; queued?: number; skipped?: number }>(
      '/api/download/album',
      {
        album_id: albumId,
        location: opts.location ?? 'navidrome',
        provider: opts.provider,
        format: opts.format,
        quality: opts.quality
      }
    )
};

export const providersApi = {
  list: () => api.get<MetadataProvidersResponse>('/api/metadata/providers')
};

export interface FormatOption {
  value: string;
  label: string;
  description?: string;
}

export interface FormatsInfo {
  formats: FormatOption[];
  qualities: FormatOption[];
  default_format: string;
  default_quality: string;
}

export interface NavidromeLibrary {
  path: string;
  label?: string;
}

export interface HealthResponse {
  status: string;
  default_metadata_provider?: string;
  spotify_configured?: boolean;
  navidrome_path?: string;
  navidrome_libraries?: NavidromeLibrary[];
  navidrome_api_url?: string;
}

export const systemApi = {
  formats: () => api.get<FormatsInfo>('/api/formats'),
  navidromeLibraries: () => api.get<{ libraries: NavidromeLibrary[] }>('/api/navidrome/libraries'),
  health: () => api.get<HealthResponse>('/api/health')
};

// ── Provider-Configs (admin-only) ─────────────────────────────────
export interface ProviderField {
  key: string;
  label: string;
  /** Bei secret=true wird der Klartext NIE zurückgegeben — nur is_set. UI zeigt masked Placeholder. */
  secret: boolean;
  is_set: boolean;
  /** Plain-Text Wert für non-secret Felder. Leer wenn nicht gesetzt. */
  value: string;
}

export interface ProviderConfig {
  name: string;
  label: string;
  fields: ProviderField[];
}

export const providersConfigApi = {
  list: () => api.get<{ providers: ProviderConfig[] }>('/api/providers/config'),
  /** Body: {fields: {[key]: value}}. Leere Strings ⇒ Reset auf env-Default. */
  update: (name: string, fields: Record<string, string>) =>
    request<{ ok: boolean; restart_required: boolean }>(
      `/api/providers/${encodeURIComponent(name)}`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fields })
      }
    )
};

// ─── Import / URL / Reverse ────────────────────────────────

export interface CsvImportStartResponse {
  job_id: string;
  total: number;
  message?: string;
}

export interface CsvImportStatus {
  status: 'queued' | 'processing' | 'completed' | 'error';
  total: number;
  processed: number;
  found: number;
  not_found: number;
  message?: string;
  /** Original-Filename (CSV-Upload), oder null bei Text-Paste. */
  filename?: string | null;
}

export interface CsvMatched {
  /** Roh-Zeile aus dem CSV-Input. */
  original: string;
  /** Geparster Artist aus der Zeile (best-effort). */
  requested_artist?: string;
  /** Geparster Titel aus der Zeile (best-effort). */
  requested_title?: string;
  /** Der gefundene Track aus dem Provider — null bei unmatched. */
  track: Track | null;
}

export interface CsvUnmatched {
  /** Roh-Zeile aus dem CSV-Input. */
  original: string;
  /** Geparster Artist aus der Zeile (best-effort). */
  requested_artist?: string;
  /** Geparster Titel aus der Zeile (best-effort). */
  requested_title?: string;
}

export interface CsvImportResult {
  total: number;
  found: number;
  not_found: number;
  matched: CsvMatched[];
  unmatched: CsvUnmatched[];
}

export const importApi = {
  startCsv: (csvText: string, provider?: string, limit?: number, filename?: string) =>
    api.post<CsvImportStartResponse>('/api/import/csv', {
      csv_text: csvText,
      provider,
      limit,
      filename
    }),
  status: (jobId: string) => api.get<CsvImportStatus>(`/api/import/csv/status/${jobId}`),
  result: (jobId: string, offset = 0, limit = 200) =>
    api.get<CsvImportResult>(
      `/api/import/csv/result/${jobId}?offset=${offset}&limit=${limit}`
    ),
  cancel: (jobId: string) => api.post<{ ok: boolean }>(`/api/import/csv/${jobId}/cancel`),
  queueAll: (
    jobId: string,
    opts: { location?: 'local' | 'navidrome'; provider?: string } = {}
  ) =>
    api.post<{ queued?: number; skipped?: number; message?: string }>(
      `/api/import/csv/queue-all/${jobId}`,
      { location: opts.location ?? 'navidrome', provider: opts.provider }
    )
};

export const urlApi = {
  download: (url: string, opts: { location?: 'local' | 'navidrome' } = {}) =>
    api.post<{ job_id: string; status: string; message?: string }>('/api/url/download', {
      url,
      location: opts.location ?? 'navidrome'
    })
};

export interface ReverseLookupResult {
  query: string;
  provider: string;
  youtube: { title?: string; channel?: string; duration?: number; thumbnail?: string };
  spotify_candidates: Track[];
}

export const reverseApi = {
  lookup: (url: string, provider?: string) =>
    api.post<ReverseLookupResult>('/api/reverse/youtube', { url, provider }),
  download: (
    youtubeUrl: string,
    candidate: Track | null,
    opts: { location?: 'local' | 'navidrome'; provider?: string } = {}
  ) =>
    api.post<{ status: string; message: string }>('/api/reverse/download', {
      youtube_url: youtubeUrl,
      spotify_track_id: candidate?.id,
      metadata: candidate
        ? {
            name: candidate.name,
            artist: candidate.artist,
            album: candidate.album,
            album_art: candidate.album_art,
            duration_ms: candidate.duration_ms
          }
        : undefined,
      location: opts.location ?? 'navidrome',
      provider: opts.provider
    })
};
