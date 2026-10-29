import { writable, type Readable } from 'svelte/store';
import { ApiError, openStream, type LaneStatusResponse, type QueueJob } from './api';

/** Ein Event von /api/queue/events. Alle Felder optional: der Server
 *  schickt nur, was sich seit dem letzten Tick geändert hat. */
export interface QueueEvent {
  status_counts?: Partial<Record<QueueJob['status'], number>>;
  jobs?: QueueJob[];
  lanes?: LaneStatusResponse;
  /** Zu viele Änderungen für einen Diff — Client soll /api/queue neu laden. */
  resync?: boolean;
}

type Handler = (ev: QueueEvent) => void;

const handlers = new Set<Handler>();
const connectedStore = writable(false);
/** true, solange der Stream steht. Konsumenten pollen nur, wenn nicht. */
export const queueStreamConnected: Readable<boolean> = connectedStore;

const RECONNECT_MIN_MS = 2000;
const RECONNECT_MAX_MS = 30000;
/** Backend ohne Stream-Endpoint (älteres Image) — nicht weiter versuchen. */
let unsupported = false;
let controller: AbortController | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let reconnectMs = RECONNECT_MIN_MS;
/** Letzter Stand von Zählungen und Lanes — neue Abonnenten bekommen ihn
 *  sofort, statt auf die nächste Änderung zu warten. */
let lastState: QueueEvent = {};

function dispatch(ev: QueueEvent) {
  if (ev.status_counts) lastState = { ...lastState, status_counts: ev.status_counts };
  if (ev.lanes) lastState = { ...lastState, lanes: ev.lanes };
  for (const h of handlers) {
    try {
      h(ev);
    } catch (err) {
      console.error('[queue-events] handler failed', err);
    }
  }
}

async function run(signal: AbortSignal) {
  const resp = await openStream('/api/queue/events', signal);
  const body = resp.body;
  if (!body) throw new Error('stream without body');
  connectedStore.set(true);
  reconnectMs = RECONNECT_MIN_MS;

  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value;
    // SSE-Events enden mit einer Leerzeile.
    let sep: number;
    while ((sep = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const data = block
        .split('\n')
        .filter((l) => l.startsWith('data:'))
        .map((l) => l.slice(5).trimStart())
        .join('\n');
      if (!data) continue; // retry:- oder Keepalive-Kommentar
      try {
        dispatch(JSON.parse(data) as QueueEvent);
      } catch {
        /* kaputtes Event überspringen */
      }
    }
  }
}

function connect() {
  if (controller || unsupported || handlers.size === 0) return;
  const ctrl = new AbortController();
  controller = ctrl;
  run(ctrl.signal)
    .catch((err) => {
      if (err instanceof ApiError && err.status === 404) unsupported = true;
    })
    .finally(() => {
      connectedStore.set(false);
      if (controller === ctrl) controller = null;
      if (ctrl.signal.aborted || unsupported || handlers.size === 0) return;
      // Server-Neustart, Netzwechsel, App im Hintergrund: mit Backoff neu
      // verbinden. In der Zwischenzeit pollen die Konsumenten.
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, reconnectMs);
      reconnectMs = Math.min(RECONNECT_MAX_MS, reconnectMs * 2);
    });
}

function disconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  controller?.abort();
  controller = null;
  connectedStore.set(false);
}

/** Abonniert den geteilten Queue-Stream. Eine Verbindung für alle
 *  Abonnenten; sie schließt, wenn der letzte abmeldet. */
export function subscribeQueueEvents(handler: Handler): () => void {
  handlers.add(handler);
  if (lastState.status_counts || lastState.lanes) handler(lastState);
  connect();
  return () => {
    handlers.delete(handler);
    if (handlers.size === 0) {
      disconnect();
      lastState = {};
    }
  };
}

/** Nach dem Wiederöffnen der App sofort neu verbinden statt den Backoff
 *  abzuwarten — iOS kappt Verbindungen im Hintergrund. */
if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible' || controller || handlers.size === 0) return;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    reconnectMs = RECONNECT_MIN_MS;
    connect();
  });
}
