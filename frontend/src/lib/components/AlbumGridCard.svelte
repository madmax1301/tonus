<script lang="ts">
  import { base } from '$app/paths';
  import { tint, DEFAULT_HUE } from '$lib/accent';
  import CoverArt from './CoverArt.svelte';
  import { Download, Loader2 } from 'lucide-svelte';
  import type { Album } from '$lib/api';

  type DownloadState = { kind: 'queued' | 'done' | 'exists' | 'error'; message?: string };

  type Props = {
    album: Album;
    queueState?: DownloadState;
    loading?: boolean;
    /** Cascading fade-in delay index (0 = first) */
    index?: number;
    provider?: string;
    onqueue: () => void;
  };

  let {
    album,
    queueState,
    loading = false,
    index = 0,
    provider = '',
    onqueue
  }: Props = $props();

  let hue: number = $state(DEFAULT_HUE);

  const accent = $derived(tint(hue, 0.95));
  const yearStr = $derived(album.release_date?.slice(0, 4) ?? '');

  function handleQueueClick(e: MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    onqueue();
  }

  /**
   * Speichert die aktuelle Library-URL beim Album-Klick. Album-Detail's
   * Back-Button liest diesen Wert und navigiert genau dorthin zurück —
   * deterministisch, anders als history.back() das je nach History-Stack
   * variieren kann.
   */
  function rememberFromUrl() {
    if (typeof window === 'undefined') return;
    try {
      sessionStorage.setItem(
        'tonus-album-back-url',
        window.location.pathname + window.location.search
      );
    } catch {
      // Quota / private mode — silently ignore
    }
  }
</script>

<a
  href="{base}/album/{album.id}?provider={provider}"
  onclick={rememberFromUrl}
  data-sveltekit-preload-data="hover"
  class="block tonus-fadein"
  style="
    text-decoration: none;
    color: inherit;
    animation-delay: {index * 0.04}s;
    contain: layout paint style;
    min-width: 0;
  "
>
  <article class="relative cursor-pointer group">
    <div
      class="relative overflow-hidden mb-2.5"
      style="
        aspect-ratio: 1;
        border-radius: 10px;
        box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.06);
      "
    >
      <CoverArt
        src={album.album_art}
        alt={album.name}
        artist={album.artist}
        radius={10}
        fluid
        onhue={(h) => (hue = h)}
      />

      <!-- Hover-tint overlay (subtle accent wash) -->
      <div
        aria-hidden="true"
        class="absolute inset-0 opacity-0 group-hover:opacity-100 transition-opacity duration-300 pointer-events-none"
        style="
          background: linear-gradient(180deg, transparent 50%, {tint(hue, 0.18)} 100%);
        "
      ></div>

      <!-- Download FAB, accent-tinted -->
      <button
        onclick={handleQueueClick}
        disabled={loading || queueState?.kind === 'done' || queueState?.kind === 'exists'}
        title={queueState?.message ?? 'Album in die Warteschlange laden'}
        class="absolute flex items-center justify-center transition-transform duration-200 group-hover:scale-105 disabled:cursor-default"
        style="
          right: 8px;
          bottom: 8px;
          width: 36px;
          height: 36px;
          border-radius: 50%;
          background: {queueState?.kind === 'done'
          ? 'var(--color-status-done)'
          : queueState?.kind === 'exists'
            ? 'rgba(255,255,255,0.15)'
            : queueState?.kind === 'error'
              ? 'var(--color-status-error)'
              : accent};
          color: #0a0a0c;
          box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
          border: none;
          opacity: {loading ? 0.7 : 1};
        "
        aria-label="In Queue"
      >
        {#if loading}
          <Loader2 size={14} strokeWidth={2.4} class="animate-spin" />
        {:else if queueState?.kind === 'done' || queueState?.kind === 'exists'}
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4">
            <path d="M5 12l5 5L20 7" />
          </svg>
        {:else}
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4">
            <path d="M12 3v12m0 0l-4-4m4 4l4-4M5 21h14" />
          </svg>
        {/if}
      </button>
    </div>

    <div
      class="font-medium leading-tight"
      style="
        font-size: 13.5px;
        letter-spacing: -0.005em;
        color: var(--color-fg-primary);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      "
      title={album.name}
    >
      {album.name}
    </div>
    <div
      class="mt-0.5"
      style="
        font-size: 11.5px;
        color: var(--color-fg-secondary);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      "
    >
      {album.artist}
      {#if yearStr}
        · <span style="color: var(--color-fg-tertiary); font-family: var(--font-mono);">{yearStr}</span>
      {/if}
    </div>
    {#if queueState?.kind === 'done' && queueState.message}
      <div
        class="mt-1"
        style="font-size: 10.5px; color: var(--color-status-done); font-family: var(--font-mono);"
      >
        ✓ {queueState.message}
      </div>
    {:else if queueState?.kind === 'exists'}
      <div
        class="mt-1"
        style="font-size: 10.5px; color: var(--color-fg-tertiary); font-family: var(--font-mono);"
      >
        ✓ vorhanden
      </div>
    {:else if queueState?.kind === 'error'}
      <div
        class="mt-1"
        style="font-size: 10.5px; color: var(--color-status-error);"
      >
        Fehler
      </div>
    {/if}
  </article>
</a>
