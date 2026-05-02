<script lang="ts">
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
  import { tint, DEFAULT_HUE } from '$lib/accent';
  import CinemaBackdrop from '$lib/components/CinemaBackdrop.svelte';
  import VinylWithCover from '$lib/components/VinylWithCover.svelte';
  import { ArrowLeft, Download, Loader2, Check } from 'lucide-svelte';

  type DownloadState = { kind: 'queued' | 'done' | 'exists' | 'error'; message?: string };

  const albumId = $derived($page.params.id);
  const provider = $derived($page.url.searchParams.get('provider') ?? '');

  let album = $state<AlbumDetail | null>(null);
  let loading = $state(true);
  let loadError = $state<string | null>(null);
  let queuedIds = $state<Record<string, DownloadState>>({});
  let albumState = $state<DownloadState | null>(null);
  let albumHue: number = $state(DEFAULT_HUE);

  $effect(() => {
    albumId; // dependency
    fetchAlbum();
  });

  async function fetchAlbum() {
    loading = true;
    loadError = null;
    try {
      album = await albumApi.get(albumId ?? '', provider || undefined);
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

  function setQueueState(id: string, s: DownloadState) {
    queuedIds = { ...queuedIds, [id]: s };
  }

  function handleError(id: string | null, err: unknown, isAlbum = false) {
    const set = (s: DownloadState) => {
      if (isAlbum) albumState = s;
      else if (id) setQueueState(id, s);
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

  // Reactive accent — driven by extracted album hue
  const accent = $derived(tint(albumHue));
  const accentSoft = $derived(tint(albumHue, 0.5));
  const accentBg = $derived(tint(albumHue, 0.12));
</script>

<CinemaBackdrop hue={albumHue} intensity={1.2} />

<section class="relative z-10 mx-auto max-w-[1180px] w-full" style="padding: 24px 36px 60px;">
  <!-- Back link -->
  <button
    onclick={() => goto(`${base}/`)}
    class="inline-flex items-center gap-1.5 transition-colors mb-7"
    style="font-size: 12.5px; color: var(--color-fg-secondary);"
    onmouseenter={(e) => (e.currentTarget.style.color = 'var(--color-fg-primary)')}
    onmouseleave={(e) => (e.currentTarget.style.color = 'var(--color-fg-secondary)')}
  >
    <ArrowLeft size={13} strokeWidth={1.5} />
    Bibliothek
  </button>

  {#if loading}
    <div class="text-sm" style="color: var(--color-fg-tertiary);">lade Album …</div>
  {:else if loadError}
    <div class="text-sm" style="color: var(--color-status-error);">{loadError}</div>
  {:else if album}
    <!-- Hero: Vinyl-with-cover left, oversized type right -->
    <div
      class="grid items-center"
      style="grid-template-columns: auto 1fr; gap: 56px; margin-bottom: 48px;"
    >
      <VinylWithCover
        src={album.cover ?? album.album_art ?? null}
        alt={album.name}
        artist={album.artist}
        year={album.release_date?.slice(0, 4) ?? ''}
        size={280}
        spinning
        onhue={(h) => (albumHue = h)}
      />

      <div class="min-w-0">
        <div
          class="font-semibold uppercase"
          style="
            font-size: 11px;
            letter-spacing: 0.24em;
            color: {accent};
            margin-bottom: 12px;
          "
        >
          {#if album.release_date}
            {album.release_date.slice(0, 4)}
          {/if}
          {#if album.genres && album.genres.length > 0}
            · {album.genres[0]}
          {/if}
          {#if album.tracks?.length}
            · {album.tracks.length} Tracks
            {#if totalDuration(album.tracks)}
              · {totalDuration(album.tracks)}
            {/if}
          {/if}
        </div>

        <h1
          class="font-semibold m-0 truncate"
          style="
            font-family: var(--font-display);
            font-size: 64px;
            line-height: 0.95;
            letter-spacing: -0.04em;
          "
          title={album.name}
        >
          {album.name}
        </h1>

        <div
          class="mt-3.5"
          style="
            font-size: 22px;
            font-weight: 300;
            letter-spacing: -0.005em;
            color: var(--color-fg-primary);
          "
        >
          {album.artist}
        </div>

        <div class="flex flex-wrap gap-2.5 mt-7 items-center">
          <button
            onclick={queueAlbum}
            disabled={albumState?.kind === 'queued' ||
              albumState?.kind === 'done' ||
              albumState?.kind === 'exists'}
            class="inline-flex items-center gap-2 transition-transform disabled:cursor-default"
            style="
              background: {albumState?.kind === 'done'
              ? 'var(--color-status-done)'
              : albumState?.kind === 'exists'
                ? 'rgba(255,255,255,0.08)'
                : albumState?.kind === 'error'
                  ? 'var(--color-status-error)'
                  : accent};
              color: {albumState?.kind === 'exists' ? 'var(--color-fg-secondary)' : '#0a0a0c'};
              padding: 12px 22px;
              border-radius: 999px;
              border: {albumState?.kind === 'exists' ? '1px solid var(--color-border-soft)' : 'none'};
              font-size: 13px;
              font-weight: 600;
              letter-spacing: 0.02em;
              box-shadow: {albumState?.kind === 'exists' || albumState?.kind === 'done' || albumState?.kind === 'error'
                ? 'none'
                : `0 8px 24px ${accentSoft}`};
            "
          >
            {#if albumState?.kind === 'queued'}
              <Loader2 size={13} strokeWidth={2.4} class="animate-spin" />
              <span class="skeleton-text">wird gequeued …</span>
            {:else if albumState?.kind === 'done'}
              <Check size={14} strokeWidth={2.4} />
              {albumState.message}
            {:else if albumState?.kind === 'exists'}
              <Check size={14} strokeWidth={2} />
              vorhanden
            {:else if albumState?.kind === 'error'}
              Fehler — erneut versuchen
            {:else}
              <Download size={14} strokeWidth={2.2} />
              Komplettes Album · {album.tracks.length} Tracks
            {/if}
          </button>

          {#if album.external_url}
            <a
              href={album.external_url}
              target="_blank"
              rel="noopener noreferrer"
              class="inline-flex items-center gap-2 transition-colors"
              style="
                background: rgba(255,255,255,0.06);
                color: var(--color-fg-primary);
                border: 1px solid var(--color-border-soft);
                padding: 12px 18px;
                border-radius: 999px;
                font-size: 13px;
                backdrop-filter: blur(20px);
                -webkit-backdrop-filter: blur(20px);
                text-decoration: none;
              "
            >
              Bei Quelle öffnen
            </a>
          {/if}
        </div>
      </div>
    </div>

    <!-- Track list — Glass panel with hairlines -->
    <div
      class="overflow-hidden"
      style="
        background: rgba(15, 15, 18, 0.5);
        backdrop-filter: blur(40px) saturate(1.2);
        -webkit-backdrop-filter: blur(40px) saturate(1.2);
        border: 1px solid var(--color-border-soft);
        border-radius: 14px;
        contain: layout paint;
      "
    >
      {#each album.tracks as track, idx (track.id)}
        {@const state = queuedIds[track.id]}
        {@const tLoading = state?.kind === 'queued'}
        {@const isDone = state?.kind === 'done' || state?.kind === 'exists'}
        {@const isLast = idx === album.tracks.length - 1}
        <div
          class="grid items-center transition-colors group"
          style="
            grid-template-columns: 44px 1fr 70px 130px;
            gap: 16px;
            padding: 14px 22px;
            border-bottom: {isLast ? 'none' : '1px solid var(--color-border-soft)'};
            font-size: 14px;
            background: {tLoading ? `linear-gradient(90deg, ${accentBg}, transparent)` : 'transparent'};
          "
          onmouseenter={(e) => {
            if (!tLoading) e.currentTarget.style.background = 'rgba(255,255,255,0.03)';
          }}
          onmouseleave={(e) => {
            if (!tLoading)
              e.currentTarget.style.background = 'transparent';
          }}
          role="presentation"
        >
          <span
            class="tabular-nums"
            style="
              color: {tLoading ? accent : 'var(--color-fg-tertiary)'};
              font-weight: {tLoading ? 600 : 400};
              font-size: 13px;
              font-family: var(--font-display);
            "
          >
            {String(track.track_number ?? idx + 1).padStart(2, '0')}
          </span>

          <div class="min-w-0">
            <div
              class="truncate"
              style="
                font-weight: {tLoading ? 500 : 400};
                letter-spacing: -0.005em;
                color: var(--color-fg-primary);
              "
            >
              {track.name}
            </div>
            {#if tLoading}
              <div
                class="mt-0.5 inline-flex items-center gap-1.5"
                style="font-size: 11px; color: {accent}; font-weight: 500;"
              >
                <span
                  class="inline-block rounded-full"
                  style="width: 6px; height: 6px; background: {accent}; animation: tonus-pulse-soft 1.4s ease-in-out infinite;"
                ></span>
                wird heruntergeladen
              </div>
            {:else if track.artist && track.artist !== album.artist}
              <div class="text-[12px] truncate mt-0.5" style="color: var(--color-fg-secondary);">
                {track.artist}
              </div>
            {/if}
          </div>

          <span
            class="tabular-nums text-right"
            style="
              color: var(--color-fg-tertiary);
              font-size: 11.5px;
              font-family: var(--font-mono);
            "
          >
            {fmtDuration(track.duration_ms)}
          </span>

          <button
            onclick={() => queueTrack(track)}
            disabled={tLoading || isDone}
            title={state?.message ?? ''}
            class="inline-flex items-center justify-center gap-1.5 ml-auto transition-colors disabled:cursor-default"
            style="
              font-size: 11.5px;
              padding: 6px 14px;
              border-radius: 999px;
              font-weight: {tLoading ? 600 : 400};
              background: {state?.kind === 'done'
              ? 'transparent'
              : state?.kind === 'exists'
                ? 'transparent'
                : state?.kind === 'error'
                  ? 'var(--color-status-error)'
                  : tLoading
                    ? accent
                    : 'rgba(255,255,255,0.06)'};
              color: {state?.kind === 'done'
              ? 'var(--color-status-done)'
              : state?.kind === 'exists'
                ? 'var(--color-fg-tertiary)'
                : state?.kind === 'error'
                  ? '#0a0a0c'
                  : tLoading
                    ? '#0a0a0c'
                    : 'var(--color-fg-secondary)'};
              border: {state?.kind === 'done'
                ? `1px solid ${'rgba(48,209,88,0.4)'}`
                : state?.kind === 'exists'
                  ? '1px solid var(--color-border-soft)'
                  : 'none'};
              min-width: 110px;
            "
          >
            {#if tLoading}
              <span class="skeleton-text">queue …</span>
            {:else if state?.kind === 'done'}
              <Check size={11} strokeWidth={2.4} />
              in Library
            {:else if state?.kind === 'exists'}
              <Check size={11} strokeWidth={1.8} />
              vorhanden
            {:else if state?.kind === 'error'}
              Fehler
            {:else}
              <Download size={11} strokeWidth={1.8} />
              Track
            {/if}
          </button>
        </div>
      {/each}
    </div>
  {/if}
</section>

<style>
  @keyframes tonus-pulse-soft {
    0%,
    100% {
      opacity: 0.6;
    }
    50% {
      opacity: 1;
    }
  }
</style>
