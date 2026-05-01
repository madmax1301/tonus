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

export interface QueueJob {
  job_id: string;
  status: 'queued' | 'processing' | 'completed' | 'error';
  stage?: string;
  progress?: number;
  message?: string;
  payload?: { track?: { name?: string; artist?: string; album?: string; album_art?: string } };
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

export const downloadApi = {
  start: (trackId: string, opts: { location?: 'local' | 'navidrome'; provider?: string } = {}) =>
    api.post<{ status: string; message: string }>('/api/download', {
      track_id: trackId,
      location: opts.location ?? 'navidrome',
      metadata_provider: opts.provider
    }),
  album: (
    albumId: string,
    opts: { location?: 'local' | 'navidrome'; provider?: string } = {}
  ) =>
    api.post<{ message: string; total_tracks?: number; queued?: number; skipped?: number }>(
      '/api/download/album',
      {
        album_id: albumId,
        location: opts.location ?? 'navidrome',
        provider: opts.provider
      }
    )
};

export const providersApi = {
  list: () => api.get<MetadataProvidersResponse>('/api/metadata/providers')
};
