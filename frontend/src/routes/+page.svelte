<script lang="ts">
  import { onMount } from 'svelte';
  import {
    searchApi,
    downloadApi,
    providersApi,
    ApiError,
    type Track,
    type Album,
    type MetadataProvidersResponse
  } from '$lib/api';
  import { base } from '$app/paths';
  import GlassCard from '$lib/components/GlassCard.svelte';
  import AlbumArt from '$lib/components/AlbumArt.svelte';
  import { Search, Download, Loader2, ChevronRight } from 'lucide-svelte';

  type Mode = 'tracks' | 'albums';

  let query = $state('');
  let mode = $state<Mode>('tracks');
  let provider = $state<string>('');
  let providersData = $state<MetadataProvidersResponse | null>(null);
  let trackResults = $state<Track[]>([]);
  let albumResults = $state<Album[]>([]);
  let searching = $state(false);
  let searchError = $state<string | null>(null);
  type DownloadState = { kind: 'queued' | 'done' | 'exists' | 'error'; message?: string };
  let queuedIds = $state<Record<string, DownloadState>>({});

  onMount(async () => {
    try {
      providersData = await providersApi.list();
      provider = providersData.default;
    } catch {
      // Auth-Sheet handled in api.ts
    }
  });

  let debounceTimer: ReturnType<typeof setTimeout> | null = null;
  function onInput() {
    if (debounceTimer) clearTimeout(debounceTimer);
    if (!query.trim()) {
      trackResults = [];
      albumResults = [];
      searchError = null;
      return;
    }
    debounceTimer = setTimeout(runSearch, 320);
  }

  async function runSearch() {
    if (!query.trim()) return;
    searching = true;
    searchError = null;
    try {
      if (mode === 'tracks') {
        trackResults = await searchApi.tracks(query.trim(), provider || undefined, 20);
        albumResults = [];
      } else {
        albumResults = await searchApi.albums(query.trim(), provider || undefined, 20);
        trackResults = [];
      }
    } catch (err) {
      trackResults = [];
      albumResults = [];
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        searchError = null; // sheet öffnet sich
      } else {
        searchError = err instanceof Error ? err.message : 'Suche fehlgeschlagen';
      }
    } finally {
      searching = false;
    }
  }

  function setMode(next: Mode) {
    if (mode === next) return;
    mode = next;
    if (query.trim()) runSearch();
  }

  function setQueueState(id: string, state: DownloadState) {
    queuedIds = { ...queuedIds, [id]: state };
  }

  function clearQueueState(id: string) {
    queuedIds = Object.fromEntries(Object.entries(queuedIds).filter(([k]) => k !== id));
  }

  function handleDownloadError(id: string, err: unknown) {
    if (err instanceof ApiError && err.status === 409) {
      const detail =
        err.body && typeof err.body === 'object' && 'detail' in err.body
          ? String((err.body as { detail: unknown }).detail)
          : 'bereits vorhanden';
      setQueueState(id, { kind: 'exists', message: detail });
    } else if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
      // Sheet öffnet sich automatisch via api.ts
      clearQueueState(id);
    } else {
      setQueueState(id, { kind: 'error' });
    }
  }

  async function queueTrack(track: Track) {
    setQueueState(track.id, { kind: 'queued' });
    try {
      await downloadApi.start(track.id, { location: 'navidrome', provider: provider || undefined });
      setQueueState(track.id, { kind: 'done' });
    } catch (err) {
      handleDownloadError(track.id, err);
    }
  }

  async function queueAlbum(album: Album) {
    setQueueState(album.id, { kind: 'queued' });
    try {
      const r = await downloadApi.album(album.id, {
        location: 'navidrome',
        provider: provider || undefined
      });
      const queued = r.queued ?? album.total_tracks;
      const skipped = r.skipped ?? 0;
      setQueueState(album.id, {
        kind: 'done',
        message: skipped > 0 ? `${queued} queued, ${skipped} schon da` : `${queued} queued`
      });
    } catch (err) {
      handleDownloadError(album.id, err);
    }
  }

  function fmtDuration(ms: number): string {
    if (!ms) return '';
    const s = Math.round(ms / 1000);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }
</script>

<section class="space-y-8">
  <header class="space-y-2">
    <h1 class="text-4xl font-semibold tracking-tight" style="color: var(--color-fg-primary);">
      Bibliothek
    </h1>
    <p class="text-sm" style="color: var(--color-fg-secondary);">
      Suche Tracks aus Deezer/Spotify/YouTube und schicke sie in die Warteschlange.
    </p>
  </header>

  <GlassCard padding="md">
    <div class="flex items-center gap-3">
      <Search size={18} strokeWidth={1.5} style="color: var(--color-fg-tertiary); flex-shrink: 0;" />
      <input
        bind:value={query}
        oninput={onInput}
        onkeydown={(e) => e.key === 'Enter' && runSearch()}
        type="text"
        placeholder={mode === 'tracks' ? 'Track, Artist oder Album …' : 'Album oder Artist …'}
        class="flex-1 bg-transparent outline-none text-base"
        style="color: var(--color-fg-primary);"
        autocomplete="off"
        spellcheck="false"
      />
      {#if providersData}
        <select
          bind:value={provider}
          class="text-[12px] px-2 py-1 rounded-md outline-none"
          style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-secondary);"
        >
          {#each providersData.providers.filter((p) => p.configured) as p}
            <option value={p.id}>{p.label}</option>
          {/each}
        </select>
      {/if}
      {#if searching}
        <Loader2 size={16} class="animate-spin" style="color: var(--color-fg-tertiary);" />
      {/if}
    </div>
  </GlassCard>

  <!-- Mode-Toggle: Tracks / Alben -->
  <div class="flex items-center gap-1">
    {#each [{ id: 'tracks', label: 'Tracks' }, { id: 'albums', label: 'Alben' }] as m}
      <button
        onclick={() => setMode(m.id as Mode)}
        class="px-3 py-1.5 rounded-full text-[12px] transition-colors"
        style="background: {mode === m.id
          ? 'var(--color-accent)'
          : 'transparent'}; color: {mode === m.id
          ? '#1a1410'
          : 'var(--color-fg-secondary)'}; border: 1px solid {mode === m.id
          ? 'transparent'
          : 'var(--color-border-soft)'};"
      >
        {m.label}
      </button>
    {/each}
  </div>

  {#if searchError}
    <div class="text-sm" style="color: var(--color-status-error);">{searchError}</div>
  {/if}

  {#if mode === 'tracks' && trackResults.length > 0}
    <div class="space-y-2">
      {#each trackResults as track (track.id)}
        {@const state = queuedIds[track.id]}
        {@const loading = state?.kind === 'queued'}
        <div class="relative" class:skeleton-card={loading}>
          <GlassCard padding="sm" interactive>
            <div class="flex items-center gap-4" class:opacity-60={loading}>
              <AlbumArt src={track.album_art} alt={track.album} size="md" />
              <div class="flex-1 min-w-0">
                <div
                  class="font-medium text-[15px] truncate"
                  style="color: var(--color-fg-primary);"
                >
                  {track.name}
                </div>
                <div
                  class="text-[13px] truncate"
                  style="color: var(--color-fg-secondary);"
                >
                  {track.artist}
                  {#if track.album}
                    <span style="color: var(--color-fg-tertiary);"> · {track.album}</span>
                  {/if}
                </div>
              </div>
              <div
                class="text-[12px] tabular-nums"
                style="color: var(--color-fg-tertiary);"
              >
                {fmtDuration(track.duration_ms)}
              </div>
              <button
                onclick={() => queueTrack(track)}
                disabled={loading || state?.kind === 'done' || state?.kind === 'exists'}
                title={state?.message ?? ''}
                class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium transition-all disabled:cursor-default"
                style="background: {state?.kind === 'done'
                  ? 'var(--color-status-done)'
                  : state?.kind === 'exists'
                    ? 'var(--color-surface-3)'
                    : state?.kind === 'error'
                      ? 'var(--color-status-error)'
                      : loading
                        ? 'var(--color-surface-3)'
                        : 'var(--color-accent)'}; color: {state?.kind === 'exists' || loading
                  ? 'var(--color-fg-secondary)'
                  : '#1a1410'}; border: {state?.kind === 'exists' || loading
                  ? '1px solid var(--color-border-soft)'
                  : 'none'}; min-width: 110px; justify-content: center;"
              >
                {#if loading}
                  <span class="skeleton-text">queue …</span>
                {:else if state?.kind === 'done'}
                  ✓ in Queue
                {:else if state?.kind === 'exists'}
                  ✓ vorhanden
                {:else if state?.kind === 'error'}
                  Fehler
                {:else}
                  <Download size={13} strokeWidth={1.8} />
                  Download
                {/if}
              </button>
            </div>
          </GlassCard>
        </div>
      {/each}
    </div>
  {:else if mode === 'albums' && albumResults.length > 0}
    <div class="space-y-2">
      {#each albumResults as album (album.id)}
        {@const state = queuedIds[album.id]}
        {@const loading = state?.kind === 'queued'}
        <div class="relative" class:skeleton-card={loading}>
          <a
            href="{base}/album/{album.id}?provider={provider}"
            class="block"
            data-sveltekit-preload-data="hover"
          >
            <GlassCard padding="sm" interactive>
              <div class="flex items-center gap-4" class:opacity-60={loading}>
                <AlbumArt src={album.album_art} alt={album.name} size="md" />
                <div class="flex-1 min-w-0">
                  <div
                    class="font-medium text-[15px] truncate"
                    style="color: var(--color-fg-primary);"
                  >
                    {album.name}
                  </div>
                  <div
                    class="text-[13px] truncate"
                    style="color: var(--color-fg-secondary);"
                  >
                    {album.artist}
                    {#if album.release_date}
                      <span style="color: var(--color-fg-tertiary);"
                        > · {album.release_date.slice(0, 4)}</span
                      >
                    {/if}
                    {#if album.total_tracks}
                      <span style="color: var(--color-fg-tertiary);"
                        > · {album.total_tracks} Tracks</span
                      >
                    {/if}
                  </div>
                </div>
                <button
                  onclick={(e) => {
                    e.preventDefault();
                    queueAlbum(album);
                  }}
                  disabled={loading || state?.kind === 'done' || state?.kind === 'exists'}
                  title={state?.message ?? ''}
                  class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium transition-all disabled:cursor-default"
                  style="background: {state?.kind === 'done'
                    ? 'var(--color-status-done)'
                    : state?.kind === 'exists'
                      ? 'var(--color-surface-3)'
                      : state?.kind === 'error'
                        ? 'var(--color-status-error)'
                        : loading
                          ? 'var(--color-surface-3)'
                          : 'var(--color-accent)'}; color: {state?.kind === 'exists' || loading
                    ? 'var(--color-fg-secondary)'
                    : '#1a1410'}; border: {state?.kind === 'exists' || loading
                    ? '1px solid var(--color-border-soft)'
                    : 'none'}; min-width: 130px; justify-content: center;"
                >
                  {#if loading}
                    <span class="skeleton-text">queue …</span>
                  {:else if state?.kind === 'done'}
                    ✓ {state.message ?? 'in Queue'}
                  {:else if state?.kind === 'exists'}
                    ✓ vorhanden
                  {:else if state?.kind === 'error'}
                    Fehler
                  {:else}
                    <Download size={13} strokeWidth={1.8} />
                    Album laden
                  {/if}
                </button>
                <ChevronRight
                  size={16}
                  strokeWidth={1.5}
                  style="color: var(--color-fg-tertiary); flex-shrink: 0;"
                />
              </div>
            </GlassCard>
          </a>
        </div>
      {/each}
    </div>
  {:else if query && !searching}
    <div class="text-sm" style="color: var(--color-fg-tertiary);">Keine Treffer.</div>
  {:else if !query}
    <div class="text-sm" style="color: var(--color-fg-tertiary);">
      Tipp: Suchbegriff eingeben und ↵ drücken, oder einfach 320 ms tippen.
    </div>
  {/if}
</section>

<!-- skeleton-card / skeleton-text leben global in app.css -->
