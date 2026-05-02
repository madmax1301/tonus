import { base } from '$app/paths';
import { getToken, challengeAuth } from './auth';

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public body?: unknown
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  const url = `${base}${path.startsWith('/') ? path : `/${path}`}`;
  const resp = await fetch(url, { ...init, headers });
  if (!resp.ok) {
    if (resp.status === 401 || resp.status === 403) {
      challengeAuth();
    }
    let body: unknown;
    try {
      body = await resp.json();
    } catch {
      body = await resp.text();
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

export const queueApi = {
  list: (status?: string) => {
    const q = status ? `?status=${encodeURIComponent(status)}` : '';
    return api.get<QueueResponse>(`/api/queue${q}`);
  },
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
}

export const systemApi = {
  formats: () => api.get<FormatsInfo>('/api/formats'),
  navidromeLibraries: () => api.get<{ libraries: NavidromeLibrary[] }>('/api/navidrome/libraries'),
  health: () => api.get<HealthResponse>('/api/health')
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
}

export interface CsvMatched {
  query?: string;
  raw_line?: string;
  artist?: string;
  title?: string;
  matched_track: Track;
}

export interface CsvUnmatched {
  query?: string;
  raw_line?: string;
  artist?: string;
  title?: string;
  reason?: string;
}

export interface CsvImportResult {
  total: number;
  found: number;
  not_found: number;
  matched: CsvMatched[];
  unmatched: CsvUnmatched[];
}

export const importApi = {
  startCsv: (csvText: string, provider?: string, limit?: number) =>
    api.post<CsvImportStartResponse>('/api/import/csv', {
      csv_text: csvText,
      provider,
      limit
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
