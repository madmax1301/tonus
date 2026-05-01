import { base } from '$app/paths';
import { getToken } from './auth';

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
