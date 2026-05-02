<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import {
    albumApi,
    downloadApi,
    ApiError,
    type AlbumDetail,
    type Track
  } from '$lib/api';
  import { defaultLocation, defaultFormat, defaultQuality } from '$lib/preferences';
  import GlassCard from '$lib/components/GlassCard.svelte';
  import AlbumArt from '$lib/components/AlbumArt.svelte';
  import ProgressLine from '$lib/components/ProgressLine.svelte';
  import { ArrowLeft, Download, Loader2 } from 'lucide-svelte';

  type DownloadState = { kind: 'queued' | 'done' | 'exists' | 'error'; message?: string };

  const albumId = $derived($page.params.id);
  const provider = $derived($page.url.searchParams.get('provider') ?? '');

  let album = $state<AlbumDetail | null>(null);
  let loading = $state(true);
  let loadError = $state<string | null>(null);
  let queuedIds = $state<Record<string, DownloadState>>({});
  let albumState = $state<DownloadState | null>(null);

  $effect(() => {
    albumId; // dependency
    fetchAlbum();
  });

  async function fetchAlbum() {
    loading = true;
    loadError = null;
    try {
      album = await albumApi.get(albumId, provider || undefined);
    } catch (err) {
      if (!(err instanceof ApiError && (err.status === 401 || err.status === 403))) {
        loadError = err instanceof Error ? err.message : 'Album konnte nicht geladen werden';
      }
    } finally {
      loading = false;
    }
  }

  function fmtDuration(ms: number): string {
    if (!ms) return '—';
    const s = Math.round(ms / 1000);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  function totalDuration(tracks: Track[]): string {
    const ms = tracks.reduce((acc, t) => acc + (t.duration_ms || 0), 0);
    const min = Math.floor(ms / 60000);
    if (min < 60) return `${min} Min`;
    const h = Math.floor(min / 60);
    const m = min % 60;
    return `${h} Std ${m} Min`;
  }

  function setQueueState(id: string, state: DownloadState) {
    queuedIds = { ...queuedIds, [id]: state };
  }

  function handleError(id: string | null, err: unknown, isAlbum = false) {
    const set = (state: DownloadState) => {
      if (isAlbum) albumState = state;
      else if (id) setQueueState(id, state);
    };
    if (err instanceof ApiError && err.status === 409) {
      const detail =
        err.body && typeof err.body === 'object' && 'detail' in err.body
          ? String((err.body as { detail: unknown }).detail)
          : 'bereits vorhanden';
      set({ kind: 'exists', message: detail });
    } else if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
      if (isAlbum) albumState = null;
      else if (id) {
        queuedIds = Object.fromEntries(Object.entries(queuedIds).filter(([k]) => k !== id));
      }
    } else {
      set({ kind: 'error' });
    }
  }

  async function queueTrack(track: Track) {
    setQueueState(track.id, { kind: 'queued' });
    try {
      await downloadApi.start(track.id, {
        location: $defaultLocation,
        provider: provider || undefined,
        format: $defaultFormat || undefined,
        quality: $defaultQuality || undefined
      });
      setQueueState(track.id, { kind: 'done' });
    } catch (err) {
      handleError(track.id, err);
    }
  }

  async function queueAlbum() {
    if (!album) return;
    albumState = { kind: 'queued' };
    try {
      const r = await downloadApi.album(album.id, {
        location: $defaultLocation,
        provider: provider || undefined,
        format: $defaultFormat || undefined,
        quality: $defaultQuality || undefined
      });
      const queued = r.queued ?? album.tracks.length;
      const skipped = r.skipped ?? 0;
      albumState = {
        kind: 'done',
        message: skipped > 0 ? `${queued} queued, ${skipped} schon da` : `${queued} queued`
      };
    } catch (err) {
      handleError(null, err, true);
    }
  }
</script>

<section class="space-y-8">
  <button
    onclick={() => goto(`${base}/`)}
    class="inline-flex items-center gap-1.5 text-[13px] transition-colors"
    style="color: var(--color-fg-secondary);"
    onmouseenter={(e) => (e.currentTarget.style.color = 'var(--color-fg-primary)')}
    onmouseleave={(e) => (e.currentTarget.style.color = 'var(--color-fg-secondary)')}
  >
    <ArrowLeft size={14} strokeWidth={1.5} />
    Bibliothek
  </button>

  {#if loading}
    <div class="text-sm" style="color: var(--color-fg-tertiary);">lade Album …</div>
  {:else if loadError}
    <div class="text-sm" style="color: var(--color-status-error);">{loadError}</div>
  {:else if album}
    <!-- Hero -->
    <header class="flex items-end gap-8 flex-wrap">
      <AlbumArt src={album.cover ?? album.album_art} alt={album.name} size="xl" />
      <div class="flex-1 min-w-0 space-y-3">
        <div class="text-[11px] font-medium uppercase tracking-widest" style="color: var(--color-fg-tertiary);">
          Album
        </div>
        <h1
          class="text-4xl font-semibold tracking-tight leading-tight"
          style="color: var(--color-fg-primary);"
        >
          {album.name}
        </h1>
        <div class="text-[14px]" style="color: var(--color-fg-secondary);">
          <span style="color: var(--color-fg-primary);">{album.artist}</span>
          {#if album.release_date}
            <span> · {album.release_date.slice(0, 4)}</span>
          {/if}
          {#if album.tracks?.length}
            <span> · {album.tracks.length} Tracks</span>
            <span style="color: var(--color-fg-tertiary);"> · {totalDuration(album.tracks)}</span>
          {/if}
        </div>
        {#if album.genres && album.genres.length > 0}
          <div class="flex flex-wrap gap-1.5">
            {#each album.genres as g}
              <span
                class="px-2 py-0.5 rounded-full text-[11px]"
                style="background: var(--color-surface-3); color: var(--color-fg-secondary);"
              >
                {g}
              </span>
            {/each}
          </div>
        {/if}
        <div class="pt-2 space-y-2 max-w-[280px]">
          <button
            onclick={queueAlbum}
            disabled={albumState?.kind === 'queued' ||
              albumState?.kind === 'done' ||
              albumState?.kind === 'exists'}
            class="w-full inline-flex items-center justify-center gap-2 px-5 py-2.5 rounded-md text-[13px] font-medium transition-opacity disabled:cursor-default"
            style="background: {albumState?.kind === 'done'
              ? 'var(--color-status-done)'
              : albumState?.kind === 'exists'
                ? 'var(--color-surface-3)'
                : albumState?.kind === 'error'
                  ? 'var(--color-status-error)'
                  : 'var(--color-accent)'}; color: {albumState?.kind === 'exists'
              ? 'var(--color-fg-secondary)'
              : '#1a1410'};"
          >
            {#if albumState?.kind === 'queued'}
              <span class="skeleton-text">Album wird gequeued …</span>
            {:else if albumState?.kind === 'done'}
              ✓ {albumState.message}
            {:else if albumState?.kind === 'exists'}
              ✓ vorhanden
            {:else if albumState?.kind === 'error'}
              Fehler
            {:else}
              <Download size={14} strokeWidth={1.8} />
              Komplettes Album laden
            {/if}
          </button>
          {#if albumState?.kind === 'queued'}
            <ProgressLine pareto thin />
          {/if}
        </div>
      </div>
    </header>

    <!-- Track-Liste -->
    <div class="space-y-1">
      {#each album.tracks as track, idx (track.id)}
        {@const state = queuedIds[track.id]}
        {@const tLoading = state?.kind === 'queued'}
        <div class="relative" class:skeleton-card={tLoading}>
          <GlassCard padding="sm" interactive>
            <div class="flex items-center gap-4" class:opacity-60={tLoading}>
              <div
                class="w-7 text-right text-[13px] tabular-nums"
                style="color: var(--color-fg-tertiary);"
              >
                {track.track_number ?? idx + 1}
              </div>
              <div class="flex-1 min-w-0">
                <div
                  class="font-medium text-[14px] truncate"
                  style="color: var(--color-fg-primary);"
                >
                  {track.name}
                </div>
                {#if track.artist && track.artist !== album.artist}
                  <div
                    class="text-[12px] truncate"
                    style="color: var(--color-fg-secondary);"
                  >
                    {track.artist}
                  </div>
                {/if}
              </div>
              <div
                class="text-[12px] tabular-nums"
                style="color: var(--color-fg-tertiary); min-width: 42px; text-align: right;"
              >
                {fmtDuration(track.duration_ms)}
              </div>
              <button
                onclick={() => queueTrack(track)}
                disabled={tLoading || state?.kind === 'done' || state?.kind === 'exists'}
                title={state?.message ?? ''}
                class="inline-flex items-center justify-center px-2.5 py-1 rounded-md text-[11px] font-medium transition-all disabled:cursor-default"
                style="background: {state?.kind === 'done'
                  ? 'var(--color-status-done)'
                  : state?.kind === 'exists'
                    ? 'transparent'
                    : state?.kind === 'error'
                      ? 'var(--color-status-error)'
                      : tLoading
                        ? 'var(--color-surface-3)'
                        : 'var(--color-surface-3)'}; color: {state?.kind === 'done'
                  ? '#1a1410'
                  : state?.kind === 'error'
                    ? '#1a1410'
                    : 'var(--color-fg-secondary)'}; border: 1px solid {state?.kind === 'done' ||
                state?.kind === 'error'
                  ? 'transparent'
                  : 'var(--color-border-soft)'}; min-width: 96px;"
              >
                {#if tLoading}
                  <span class="skeleton-text">queue …</span>
                {:else if state?.kind === 'done'}
                  ✓ in Queue
                {:else if state?.kind === 'exists'}
                  ✓ vorhanden
                {:else if state?.kind === 'error'}
                  Fehler
                {:else}
                  <Download size={11} strokeWidth={1.8} />
                  <span class="ml-1">Track</span>
                {/if}
              </button>
            </div>
          </GlassCard>
        </div>
      {/each}
    </div>
  {/if}
</section>

<!-- skeleton-card / skeleton-text leben global in app.css -->
