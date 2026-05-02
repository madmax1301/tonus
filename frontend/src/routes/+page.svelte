<script lang="ts">
  import { onMount } from 'svelte';
  import {
    searchApi,
    downloadApi,
    providersApi,
    urlApi,
    reverseApi,
    ApiError,
    type Track,
    type Album,
    type MetadataProvidersResponse,
    type ReverseLookupResult
  } from '$lib/api';
  import { base } from '$app/paths';
  import { defaultProvider, defaultLocation } from '$lib/preferences';
  import { get } from 'svelte/store';
  import GlassCard from '$lib/components/GlassCard.svelte';
  import AlbumArt from '$lib/components/AlbumArt.svelte';
  import { Search, Download, Loader2, ChevronRight, Link2, Youtube } from 'lucide-svelte';

  type Mode = 'tracks' | 'albums' | 'url' | 'reverse';

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

  // ── URL-Direktdownload ──────────────────────────────────
  let urlInput = $state('');
  let urlBusy = $state(false);
  let urlMessage = $state<string | null>(null);
  let urlError = $state<string | null>(null);

  async function submitUrl() {
    if (!urlInput.trim()) return;
    urlBusy = true;
    urlMessage = null;
    urlError = null;
    try {
      const r = await urlApi.download(urlInput.trim(), { location: $defaultLocation });
      urlMessage = r.message ?? `In Queue als ${r.job_id}`;
      urlInput = '';
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const detail =
          err.body && typeof err.body === 'object' && 'detail' in err.body
            ? String((err.body as { detail: unknown }).detail)
            : 'bereits vorhanden';
        urlMessage = `${detail}`;
      } else {
        urlError = err instanceof Error ? err.message : 'URL-Download fehlgeschlagen';
      }
    } finally {
      urlBusy = false;
    }
  }

  // ── Reverse YouTube (URL → Match-Kandidaten) ─────────────
  let revUrl = $state('');
  let revBusy = $state(false);
  let revLookup = $state<ReverseLookupResult | null>(null);
  let revError = $state<string | null>(null);
  let revQueuing = $state<Record<string, DownloadState>>({});

  async function submitReverse() {
    if (!revUrl.trim()) return;
    revBusy = true;
    revError = null;
    revLookup = null;
    revQueuing = {};
    try {
      revLookup = await reverseApi.lookup(revUrl.trim(), provider || undefined);
    } catch (err) {
      revError = err instanceof Error ? err.message : 'Reverse-Lookup fehlgeschlagen';
    } finally {
      revBusy = false;
    }
  }

  async function pickRevCandidate(c: Track) {
    revQueuing = { ...revQueuing, [c.id]: { kind: 'queued' } };
    try {
      await reverseApi.download(revUrl.trim(), c, {
        location: $defaultLocation,
        provider: provider || undefined
      });
      revQueuing = { ...revQueuing, [c.id]: { kind: 'done' } };
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const detail =
          err.body && typeof err.body === 'object' && 'detail' in err.body
            ? String((err.body as { detail: unknown }).detail)
            : 'bereits vorhanden';
        revQueuing = { ...revQueuing, [c.id]: { kind: 'exists', message: detail } };
      } else {
        revQueuing = { ...revQueuing, [c.id]: { kind: 'error' } };
      }
    }
  }

  async function pickRevRaw() {
    if (!revUrl.trim()) return;
    revError = null;
    try {
      await reverseApi.download(revUrl.trim(), null, { location: $defaultLocation });
      revUrl = '';
      revLookup = null;
    } catch (err) {
      revError = err instanceof Error ? err.message : 'Direkter Download fehlgeschlagen';
    }
  }

  onMount(async () => {
    try {
      providersData = await providersApi.list();
      const userPref = get(defaultProvider);
      provider = userPref || providersData.default;
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
      await downloadApi.start(track.id, {
        location: $defaultLocation,
        provider: provider || undefined
      });
      setQueueState(track.id, { kind: 'done' });
    } catch (err) {
      handleDownloadError(track.id, err);
    }
  }

  async function queueAlbum(album: Album) {
    setQueueState(album.id, { kind: 'queued' });
    try {
      const r = await downloadApi.album(album.id, {
        location: $defaultLocation,
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

  <!-- Mode-Toggle: Tracks · Alben · URL · Reverse YouTube -->
  <div class="flex items-center gap-1 flex-wrap">
    {#each [
      { id: 'tracks' as Mode, label: 'Tracks', icon: Search },
      { id: 'albums' as Mode, label: 'Alben', icon: Search },
      { id: 'url' as Mode, label: 'URL', icon: Link2 },
      { id: 'reverse' as Mode, label: 'Reverse YouTube', icon: Youtube }
    ] as m}
      <button
        onclick={() => setMode(m.id)}
        class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[12px] transition-colors"
        style="background: {mode === m.id
          ? 'var(--color-accent)'
          : 'transparent'}; color: {mode === m.id
          ? '#1a1410'
          : 'var(--color-fg-secondary)'}; border: 1px solid {mode === m.id
          ? 'transparent'
          : 'var(--color-border-soft)'};"
      >
        <svelte:component this={m.icon} size={13} strokeWidth={1.5} />
        {m.label}
      </button>
    {/each}
    {#if providersData}
      <select
        bind:value={provider}
        class="ml-auto text-[12px] px-2 py-1 rounded-md outline-none"
        style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-secondary);"
      >
        {#each providersData.providers.filter((p) => p.configured) as p}
          <option value={p.id}>{p.label}</option>
        {/each}
      </select>
    {/if}
  </div>

  {#if mode === 'tracks' || mode === 'albums'}
    <GlassCard padding="md">
      <div class="flex items-center gap-3">
        <Search
          size={18}
          strokeWidth={1.5}
          style="color: var(--color-fg-tertiary); flex-shrink: 0;"
        />
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
        {#if searching}
          <Loader2 size={16} class="animate-spin" style="color: var(--color-fg-tertiary);" />
        {/if}
      </div>
    </GlassCard>
  {/if}

  {#if searchError && (mode === 'tracks' || mode === 'albums')}
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
  {:else if mode === 'url'}
    <!-- ────────── URL (yt-dlp direct) ────────── -->
    <GlassCard padding="md">
      <div class="space-y-3">
        <label class="block space-y-2">
          <span class="text-[13px] font-medium" style="color: var(--color-fg-primary);">
            URL — YouTube, SoundCloud, Bandcamp, Vimeo, …
          </span>
          <input
            type="url"
            bind:value={urlInput}
            onkeydown={(e) => e.key === 'Enter' && submitUrl()}
            placeholder="https://…"
            spellcheck="false"
            autocomplete="off"
            class="w-full px-3 py-2.5 rounded-md text-[13px] font-mono outline-none focus:border-[var(--color-accent)]"
            style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-primary);"
          />
        </label>
        <div class="flex items-center gap-3 flex-wrap">
          <button
            onclick={submitUrl}
            disabled={urlBusy || !urlInput.trim()}
            class="inline-flex items-center gap-2 px-4 py-2 rounded-md text-[13px] font-medium transition-opacity disabled:opacity-40"
            style="background: var(--color-accent); color: #1a1410;"
          >
            {#if urlBusy}
              <Loader2 size={13} class="animate-spin" />
            {:else}
              <Download size={13} strokeWidth={1.8} />
            {/if}
            In Queue
          </button>
          {#if urlMessage}
            <span class="text-[12px]" style="color: var(--color-status-done);">
              ✓ {urlMessage}
            </span>
          {/if}
          {#if urlError}
            <span class="text-[12px]" style="color: var(--color-status-error);">{urlError}</span>
          {/if}
        </div>
      </div>
    </GlassCard>
    <p class="text-[12px]" style="color: var(--color-fg-tertiary);">
      Lädt direkt via yt-dlp ohne Metadata-Match. Title und Artist-Tag bleiben so wie auf der
      Quelle. Brauchst du saubere Tags, nutze stattdessen <strong>Reverse YouTube</strong>.
    </p>
  {:else if mode === 'reverse'}
    <!-- ────────── Reverse YouTube ────────── -->
    <GlassCard padding="md">
      <div class="space-y-3">
        <label class="block space-y-2">
          <span class="text-[13px] font-medium" style="color: var(--color-fg-primary);">
            YouTube-URL — wir matchen den Track im Provider und queuen mit sauberen Tags
          </span>
          <input
            type="url"
            bind:value={revUrl}
            onkeydown={(e) => e.key === 'Enter' && submitReverse()}
            placeholder="https://www.youtube.com/watch?v=…"
            spellcheck="false"
            autocomplete="off"
            class="w-full px-3 py-2.5 rounded-md text-[13px] font-mono outline-none focus:border-[var(--color-accent)]"
            style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-primary);"
          />
        </label>
        <div class="flex items-center gap-3 flex-wrap">
          <button
            onclick={submitReverse}
            disabled={revBusy || !revUrl.trim()}
            class="inline-flex items-center gap-2 px-4 py-2 rounded-md text-[13px] font-medium transition-opacity disabled:opacity-40"
            style="background: var(--color-accent); color: #1a1410;"
          >
            {#if revBusy}
              <Loader2 size={13} class="animate-spin" />
            {:else}
              Match suchen
            {/if}
          </button>
          {#if revLookup || revError}
            <button
              onclick={pickRevRaw}
              class="inline-flex items-center gap-2 px-3 py-2 rounded-md text-[12px] transition-colors"
              style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-secondary);"
            >
              Direkt laden ohne Match
            </button>
          {/if}
          {#if revError}
            <span class="text-[12px]" style="color: var(--color-status-error);">{revError}</span>
          {/if}
        </div>
      </div>
    </GlassCard>

    {#if revLookup}
      <div class="space-y-3">
        {#if revLookup.youtube?.title}
          <div class="text-[13px]" style="color: var(--color-fg-secondary);">
            <span style="color: var(--color-fg-tertiary);">YouTube:</span>
            <span style="color: var(--color-fg-primary);" class="font-medium">
              {revLookup.youtube.title}
            </span>
            {#if revLookup.youtube.channel}
              · {revLookup.youtube.channel}
            {/if}
          </div>
        {/if}
        <div class="text-[12px]" style="color: var(--color-fg-tertiary);">
          {revLookup.spotify_candidates.length} mögliche Treffer · wähle einen für Tags + Cover
        </div>
        <div class="space-y-2">
          {#each revLookup.spotify_candidates as c (c.id)}
            {@const state = revQueuing[c.id]}
            {@const rLoading = state?.kind === 'queued'}
            <div class="relative" class:skeleton-card={rLoading}>
              <GlassCard padding="sm" interactive>
                <div class="flex items-center gap-4" class:opacity-60={rLoading}>
                  <AlbumArt src={c.album_art} alt={c.album} size="md" />
                  <div class="flex-1 min-w-0">
                    <div
                      class="font-medium text-[14px] truncate"
                      style="color: var(--color-fg-primary);"
                    >
                      {c.name}
                    </div>
                    <div
                      class="text-[12px] truncate"
                      style="color: var(--color-fg-secondary);"
                    >
                      {c.artist}
                      {#if c.album}
                        <span style="color: var(--color-fg-tertiary);"> · {c.album}</span>
                      {/if}
                    </div>
                  </div>
                  <div
                    class="text-[12px] tabular-nums"
                    style="color: var(--color-fg-tertiary);"
                  >
                    {fmtDuration(c.duration_ms)}
                  </div>
                  <button
                    onclick={() => pickRevCandidate(c)}
                    disabled={rLoading || state?.kind === 'done' || state?.kind === 'exists'}
                    title={state?.message ?? ''}
                    class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium transition-all disabled:cursor-default"
                    style="background: {state?.kind === 'done'
                      ? 'var(--color-status-done)'
                      : state?.kind === 'exists'
                        ? 'var(--color-surface-3)'
                        : state?.kind === 'error'
                          ? 'var(--color-status-error)'
                          : rLoading
                            ? 'var(--color-surface-3)'
                            : 'var(--color-accent)'}; color: {state?.kind === 'exists' || rLoading
                      ? 'var(--color-fg-secondary)'
                      : '#1a1410'}; border: {state?.kind === 'exists' || rLoading
                      ? '1px solid var(--color-border-soft)'
                      : 'none'}; min-width: 110px; justify-content: center;"
                  >
                    {#if rLoading}
                      <span class="skeleton-text">queue …</span>
                    {:else if state?.kind === 'done'}
                      ✓ in Queue
                    {:else if state?.kind === 'exists'}
                      ✓ vorhanden
                    {:else if state?.kind === 'error'}
                      Fehler
                    {:else}
                      <Download size={13} strokeWidth={1.8} />
                      Diesen Match
                    {/if}
                  </button>
                </div>
              </GlassCard>
            </div>
          {/each}
        </div>
      </div>
    {/if}
  {:else if (mode === 'tracks' || mode === 'albums') && query && !searching}
    <div class="text-sm" style="color: var(--color-fg-tertiary);">Keine Treffer.</div>
  {:else if (mode === 'tracks' || mode === 'albums') && !query}
    <div class="text-sm" style="color: var(--color-fg-tertiary);">
      Tipp: Suchbegriff eingeben und ↵ drücken, oder einfach 320 ms tippen.
    </div>
  {/if}
</section>

<!-- skeleton-card / skeleton-text leben global in app.css -->
