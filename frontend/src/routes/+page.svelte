<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { replaceState } from '$app/navigation';
  import {
    searchApi,
    downloadApi,
    providersApi,
    urlApi,
    reverseApi,
    systemApi,
    ApiError,
    type Track,
    type Album,
    type MetadataProvidersResponse,
    type ReverseLookupResult,
    type HealthResponse
  } from '$lib/api';
  import { base } from '$app/paths';
  import { defaultProvider, defaultLocation, defaultFormat, defaultQuality } from '$lib/preferences';
  import { get } from 'svelte/store';
  import { tint, DEFAULT_HUE } from '$lib/accent';
  import { t } from '$lib/i18n';
  import { flyToQueue } from '$lib/fly-to-queue';
  import EmptyState from '$lib/components/EmptyState.svelte';
  import VinylWithCover from '$lib/components/VinylWithCover.svelte';
  import CinemaBackdrop from '$lib/components/CinemaBackdrop.svelte';
  import GlassCard from '$lib/components/GlassCard.svelte';
  import AlbumArt from '$lib/components/AlbumArt.svelte';
  import AlbumGridCard from '$lib/components/AlbumGridCard.svelte';
  import { Download, LoaderCircle, Link2, Play } from 'lucide-svelte';

  type Mode = 'tracks' | 'albums' | 'url' | 'reverse';

  let query = $state('');
  let mode = $state<Mode>('tracks');
  // Bind aufs Search-Input — Empty-State CTA "Track suchen" focused dieses
  // Element via .focus(), damit der User direkt tippen kann ohne den
  // Search-Bar erst manuell anzuklicken.
  let searchInput = $state<HTMLInputElement | null>(null);
  let provider = $state<string>('');
  let providersData = $state<MetadataProvidersResponse | null>(null);
  // Health wird non-blocking geladen — ausschließlich für den Empty-State-Link
  // zum Navidrome-UI. Bei aktiver Suche ist Empty-State eh nie sichtbar, also
  // kein Render-Block nötig.
  let health = $state<HealthResponse | null>(null);
  // Link auf Navidrome-Web-UI nur zeigen, wenn URL gesetzt UND nicht localhost/127.
  // Default `http://localhost:4533` ist vom Browser eines anderen Hosts (NAS-Setup
  // ist Standard) nicht erreichbar — toter Link wäre schlechter als kein Link.
  const navidromeWebUrl = $derived.by(() => {
    const u = health?.navidrome_api_url;
    if (!u) return null;
    if (/(localhost|127\.0\.0\.1|0\.0\.0\.0)/i.test(u)) return null;
    return u;
  });
  let trackResults = $state<Track[]>([]);
  let albumResults = $state<Album[]>([]);
  let searching = $state(false);
  let searchError = $state<string | null>(null);
  type DownloadState = { kind: 'queued' | 'done' | 'exists' | 'error'; message?: string };
  let queuedIds = $state<Record<string, DownloadState>>({});

  // Featured album drives backdrop + hero vinyl. Falls keine Suche aktiv ist
  // (oder Track-Modus ohne Album-Cover): Default-Gold (DEFAULT_HUE).
  let featured = $state<{ src?: string | null; artist?: string; year?: string; hue: number } | null>(
    null
  );

  // ── URL-Direktdownload ──────────────────────────────────
  let urlInput = $state('');
  let urlBusy = $state(false);
  let urlMessage = $state<string | null>(null);
  let urlError = $state<string | null>(null);

  async function submitUrl(ev?: MouseEvent | KeyboardEvent) {
    if (!urlInput.trim()) return;
    urlBusy = true;
    urlMessage = null;
    urlError = null;
    // URL-Download hat kein Cover-Bild — wir nehmen den Click-Auslöser
    // als Source. Bei Enter-Trigger im Input nehmen wir das Input-Feld
    // selbst. Klon zeigt das Gradient-Fallback (kein src) — der Effekt
    // dient hier hauptsächlich als visuelle Bestätigung "in Queue".
    let coverEl: HTMLElement | null = null;
    if (ev) {
      coverEl = ev.currentTarget as HTMLElement;
    }
    try {
      const r = await urlApi.download(urlInput.trim(), { location: $defaultLocation });
      urlMessage = r.message ?? `In Queue als ${r.job_id}`;
      urlInput = '';
      if (coverEl) {
        flyToQueue(coverEl, null, accent, 32);
      }
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

  // ── Reverse YouTube ─────────────
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

  async function pickRevCandidate(c: Track, ev?: MouseEvent) {
    revQueuing = { ...revQueuing, [c.id]: { kind: 'queued' } };
    let coverEl: HTMLElement | null = null;
    if (ev) {
      const btn = ev.currentTarget as HTMLElement;
      const row = btn.closest<HTMLElement>('[data-track-row]');
      coverEl = row?.querySelector<HTMLElement>('[data-cover]') ?? null;
    }
    try {
      await reverseApi.download(revUrl.trim(), c, {
        location: $defaultLocation,
        provider: provider || undefined
      });
      revQueuing = { ...revQueuing, [c.id]: { kind: 'done' } };
      if (coverEl) {
        flyToQueue(coverEl, c.album_art ?? null, accent, coverEl.offsetWidth);
      }
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

  async function pickRevRaw(ev?: MouseEvent) {
    if (!revUrl.trim()) return;
    revError = null;
    const coverEl = ev ? (ev.currentTarget as HTMLElement) : null;
    try {
      await reverseApi.download(revUrl.trim(), null, { location: $defaultLocation });
      revUrl = '';
      revLookup = null;
      if (coverEl) {
        flyToQueue(coverEl, null, accent, 32);
      }
    } catch (err) {
      revError = err instanceof Error ? err.message : 'Direkter Download fehlgeschlagen';
    }
  }

  // sessionStorage-Key — Library-State überlebt Album-Detail-Roundtrips auch
  // wenn die URL-Sync wegen replaceState-Edge-Cases verloren ging.
  const LIBRARY_STATE_KEY = 'tonus-library-state-v1';

  type PersistedState = {
    q: string;
    mode: Mode;
    /** ms-Timestamp; auto-expire nach 1 h damit alte Sessions nicht aufpoppen */
    t: number;
  };

  function readPersistedState(): PersistedState | null {
    if (typeof window === 'undefined') return null;
    try {
      const raw = sessionStorage.getItem(LIBRARY_STATE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as PersistedState;
      if (!parsed || typeof parsed !== 'object') return null;
      if (Date.now() - parsed.t > 60 * 60 * 1000) return null;
      return parsed;
    } catch {
      return null;
    }
  }

  function writePersistedState() {
    if (typeof window === 'undefined') return;
    try {
      const payload: PersistedState = { q: query, mode, t: Date.now() };
      sessionStorage.setItem(LIBRARY_STATE_KEY, JSON.stringify(payload));
    } catch {
      // Quota / private-mode — silently ignore
    }
  }

  onMount(async () => {
    // Restore search state. URL ist primary (deep-link-friendly), sessionStorage
    // ist Fallback wenn URL nichts hergibt (Browser-back-Edge-Cases, syncUrl
    // wurde vor Album-Klick nicht durchgelaufen, etc.).
    const sp = $page.url.searchParams;
    const qFromUrl = sp.get('q') ?? '';
    const modeFromUrl = sp.get('mode') as Mode | null;

    let restoredQ = '';
    let restoredMode: Mode | null = null;

    if (qFromUrl) restoredQ = qFromUrl;
    if (modeFromUrl && ['tracks', 'albums', 'url', 'reverse'].includes(modeFromUrl)) {
      restoredMode = modeFromUrl;
    }

    if (!restoredQ || !restoredMode) {
      const cached = readPersistedState();
      if (cached) {
        if (!restoredQ && cached.q) restoredQ = cached.q;
        if (!restoredMode && cached.mode) restoredMode = cached.mode;
      }
    }

    if (restoredQ) query = restoredQ;
    if (restoredMode) mode = restoredMode;

    try {
      providersData = await providersApi.list();
      const userPref = get(defaultProvider);
      provider = userPref || providersData.default;
    } catch {
      // Auth-Sheet handled in api.ts
    }

    // Health non-blocking — nur für den optionalen Navidrome-Link im Empty-State.
    systemApi
      .health()
      .then((h) => {
        health = h;
      })
      .catch(() => {
        // Egal — ohne Health zeigen wir den Empty-State ohne Navidrome-Link.
      });

    // URL erneuern damit beide Quellen synchron sind (falls URL leer war)
    syncUrl();

    if (query.trim() && (mode === 'tracks' || mode === 'albums')) {
      runSearch();
    }
  });

  /**
   * Sync `query` + `mode` zu URL-Params (replaceState — kein History-Spam)
   * UND zu sessionStorage (überlebt URL-Quirks beim Browser-Back).
   */
  function syncUrl() {
    if (typeof window === 'undefined') return;
    const url = new URL(window.location.href);
    if (query.trim()) {
      url.searchParams.set('q', query.trim());
    } else {
      url.searchParams.delete('q');
    }
    if (mode !== 'tracks') {
      url.searchParams.set('mode', mode);
    } else {
      url.searchParams.delete('mode');
    }
    replaceState(url, $page.state ?? {});
    writePersistedState();
  }

  let debounceTimer: ReturnType<typeof setTimeout> | null = null;
  function onInput() {
    if (debounceTimer) clearTimeout(debounceTimer);
    if (!query.trim()) {
      trackResults = [];
      albumResults = [];
      searchError = null;
      syncUrl();
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
        const first = trackResults[0];
        if (first?.album_art) {
          featured = {
            src: first.album_art,
            artist: first.artist,
            year: first.release_date?.slice(0, 4),
            hue: DEFAULT_HUE
          };
        }
      } else {
        albumResults = await searchApi.albums(query.trim(), provider || undefined, 20);
        trackResults = [];
        const first = albumResults[0];
        if (first?.album_art) {
          featured = {
            src: first.album_art,
            artist: first.artist,
            year: first.release_date?.slice(0, 4),
            hue: DEFAULT_HUE
          };
        }
      }
    } catch (err) {
      trackResults = [];
      albumResults = [];
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        searchError = null;
      } else {
        searchError = err instanceof Error ? err.message : 'Suche fehlgeschlagen';
      }
    } finally {
      searching = false;
      syncUrl();
    }
  }

  function setMode(next: Mode) {
    if (mode === next) return;
    mode = next;
    if (query.trim()) runSearch();
    else syncUrl();
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
      clearQueueState(id);
    } else {
      setQueueState(id, { kind: 'error' });
    }
  }

  async function queueTrack(track: Track, ev?: MouseEvent) {
    setQueueState(track.id, { kind: 'queued' });
    // Cover-Klon für Fly-Animation. Findet das nächste data-track-row
    // (gesetzt via JSX), greift den data-cover-Wrapper. Animation startet
    // erst nach erfolgreichem queue-API-Call — sonst fliegt der Cover
    // auch bei Fehler, was verwirrend ist.
    let coverEl: HTMLElement | null = null;
    if (ev) {
      const btn = ev.currentTarget as HTMLElement;
      const row = btn.closest<HTMLElement>('[data-track-row]');
      coverEl = row?.querySelector<HTMLElement>('[data-cover]') ?? null;
    }
    try {
      await downloadApi.start(track.id, {
        location: $defaultLocation,
        provider: provider || undefined,
        format: $defaultFormat || undefined,
        quality: $defaultQuality || undefined
      });
      setQueueState(track.id, { kind: 'done' });
      if (coverEl) {
        flyToQueue(coverEl, track.album_art ?? null, accent, coverEl.offsetWidth);
      }
    } catch (err) {
      handleDownloadError(track.id, err);
    }
  }

  async function queueAlbum(album: Album) {
    setQueueState(album.id, { kind: 'queued' });
    try {
      const r = await downloadApi.album(album.id, {
        location: $defaultLocation,
        provider: provider || undefined,
        format: $defaultFormat || undefined,
        quality: $defaultQuality || undefined
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

  // Reactive accent — driven by featured album, fallback DEFAULT_HUE
  const accentHue = $derived(featured?.hue ?? DEFAULT_HUE);
  const accent = $derived(tint(accentHue));
  const accentSoft = $derived(tint(accentHue, 0.95));

  function setFeaturedHue(h: number) {
    if (featured) featured = { ...featured, hue: h };
  }
</script>

<CinemaBackdrop hue={accentHue} />

<section class="relative z-10 mx-auto max-w-[1180px] w-full" style="padding: clamp(20px, 4vw, 40px) clamp(14px, 4vw, 36px) clamp(28px, 5vw, 50px);">
  <!-- Editorial Hero: oversized title + featured vinyl -->
  <div class="grid items-center tonus-library-hero" style="grid-template-columns: 1.3fr 1fr; gap: 48px; margin-bottom: clamp(28px, 5vw, 48px);">
    <div>
      <div
        class="font-semibold uppercase"
        style="
          font-size: 11px;
          letter-spacing: 0.24em;
          color: {accent};
          margin-bottom: 14px;
        "
      >
        {$t('library.eyebrow')}
      </div>
      <h1
        class="font-semibold m-0"
        style="
          font-family: var(--font-display);
          font-size: clamp(30px, 7vw, 48px);
          font-weight: 600;
          line-height: 0.95;
          letter-spacing: -0.035em;
        "
      >
        {$t('library.title.before')}<br />
        <em style="color: {accent}; font-weight: 400; font-style: italic;"
          >{$t('library.title.italic')}</em
        >{$t('library.title.after')}
      </h1>
      <p
        style="
          font-size: 14px;
          color: var(--color-fg-secondary);
          max-width: 440px;
          margin-top: 18px;
          line-height: 1.6;
        "
      >
        {$t('library.description')}
      </p>
    </div>
    <div class="flex justify-center tonus-library-vinyl">
      <VinylWithCover
        src={featured?.src ?? null}
        alt={featured?.artist ?? ''}
        artist={featured?.artist ?? 'Tonus'}
        year={featured?.year ?? ''}
        size={260}
        spinning={!!featured}
        onhue={setFeaturedHue}
      />
    </div>
  </div>

  <!-- Universal Glass-Search-Bar — Inhalt switcht mit Mode, immer oben -->
  <div
    class="flex items-center"
    style="
      background: rgba(20, 20, 24, 0.5);
      backdrop-filter: blur(40px) saturate(1.2);
      -webkit-backdrop-filter: blur(40px) saturate(1.2);
      border: 1px solid var(--color-border-soft);
      border-radius: 18px;
      padding: 18px 22px;
      gap: 16px;
      margin-bottom: 22px;
      box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);
    "
  >
    {#if mode === 'tracks' || mode === 'albums'}
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke={accent} stroke-width="1.5">
        <circle cx="11" cy="11" r="7" />
        <path d="M21 21l-4.3-4.3" />
      </svg>
      <input
        bind:this={searchInput}
        bind:value={query}
        oninput={onInput}
        onkeydown={(e) => e.key === 'Enter' && runSearch()}
        type="text"
        placeholder={mode === 'tracks'
          ? $t('library.placeholder.tracks')
          : $t('library.placeholder.albums')}
        class="flex-1 bg-transparent outline-none"
        style="font-size: 18px; font-weight: 300; letter-spacing: -0.005em; color: var(--color-fg-primary);"
        autocomplete="off"
        spellcheck="false"
      />
      {#if searching}
        <LoaderCircle size={16} class="animate-spin" style="color: var(--color-fg-tertiary);" />
      {:else if mode === 'tracks' && trackResults.length > 0}
        <span class="uppercase" style="font-size: 11px; color: var(--color-fg-tertiary); font-family: var(--font-mono); letter-spacing: 0.04em;">
          {trackResults.length} {$t('common.matches')}
        </span>
      {:else if mode === 'albums' && albumResults.length > 0}
        <span class="uppercase" style="font-size: 11px; color: var(--color-fg-tertiary); font-family: var(--font-mono); letter-spacing: 0.04em;">
          {albumResults.length} {$t('common.matches')}
        </span>
      {/if}
    {:else if mode === 'url'}
      <Link2 size={20} strokeWidth={1.5} style="color: {accent}; flex-shrink: 0;" />
      <input
        type="url"
        bind:value={urlInput}
        onkeydown={(e) => e.key === 'Enter' && submitUrl(e)}
        placeholder={$t('library.placeholder.url')}
        spellcheck="false"
        autocomplete="off"
        class="flex-1 bg-transparent outline-none"
        style="font-size: 18px; font-weight: 300; letter-spacing: -0.005em; color: var(--color-fg-primary);"
      />
      <button
        onclick={(e) => submitUrl(e)}
        disabled={urlBusy || !urlInput.trim()}
        class="inline-flex items-center gap-1.5 transition-opacity disabled:opacity-40"
        style="background: {accent}; color: #0a0a0c; padding: 4px 12px; border-radius: 999px; font-size: 11px; font-weight: 600; letter-spacing: 0.04em; line-height: 1; text-transform: uppercase; flex-shrink: 0;"
      >
        {#if urlBusy}
          <LoaderCircle size={11} class="animate-spin" />
        {:else}
          <Download size={11} strokeWidth={2} />
        {/if}
        {$t('library.url.queue_button')}
      </button>
    {:else if mode === 'reverse'}
      <Play size={20} strokeWidth={1.5} style="color: {accent}; flex-shrink: 0;" />
      <input
        type="url"
        bind:value={revUrl}
        onkeydown={(e) => e.key === 'Enter' && submitReverse()}
        placeholder={$t('library.placeholder.youtube')}
        spellcheck="false"
        autocomplete="off"
        class="flex-1 bg-transparent outline-none"
        style="font-size: 18px; font-weight: 300; letter-spacing: -0.005em; color: var(--color-fg-primary);"
      />
      <button
        onclick={submitReverse}
        disabled={revBusy || !revUrl.trim()}
        class="inline-flex items-center gap-1.5 transition-opacity disabled:opacity-40"
        style="background: {accent}; color: #0a0a0c; padding: 4px 12px; border-radius: 999px; font-size: 11px; font-weight: 600; letter-spacing: 0.04em; line-height: 1; text-transform: uppercase; flex-shrink: 0;"
      >
        {#if revBusy}
          <LoaderCircle size={11} class="animate-spin" />
        {:else}
          {$t('library.youtube.search_button', { provider: provider || 'Provider' })}
        {/if}
      </button>
    {/if}
  </div>

  <!-- Mode strip — underline tabs + provider info -->
  <div
    class="flex items-center"
    style="
      gap: 24px;
      font-size: 13px;
      margin-bottom: 24px;
      border-bottom: 1px solid var(--color-border-soft);
      padding-bottom: 14px;
    "
  >
    {#each [
      { id: 'tracks' as Mode, labelKey: 'library.mode.tracks' as const, count: trackResults.length, icon: null },
      { id: 'albums' as Mode, labelKey: 'library.mode.albums' as const, count: albumResults.length, icon: null },
      { id: 'url' as Mode, labelKey: 'library.mode.url' as const, count: null, icon: Link2 },
      { id: 'reverse' as Mode, labelKey: 'library.mode.youtube_match' as const, count: null, icon: Play }
    ] as m}
      {@const active = mode === m.id}
      <button
        onclick={() => setMode(m.id)}
        class="relative inline-flex items-center gap-1.5 transition-colors"
        style="
          color: {active ? 'var(--color-fg-primary)' : 'var(--color-fg-secondary)'};
          font-weight: {active ? 500 : 400};
          padding-bottom: 14px;
          margin-bottom: -14px;
          border-bottom: 2px solid {active ? accent : 'transparent'};
        "
      >
        {#if m.icon}
          <svelte:component this={m.icon} size={13} strokeWidth={1.5} />
        {/if}
        {$t(m.labelKey)}
        {#if m.count !== null && m.count > 0}
          <span style="color: var(--color-fg-tertiary);"> · {m.count}</span>
        {/if}
      </button>
    {/each}
    {#if providersData}
      <select
        bind:value={provider}
        class="ml-auto text-[11px] px-2 py-1 rounded-md outline-none"
        style="
          background: rgba(255, 255, 255, 0.02);
          border: 1px solid var(--color-border-soft);
          color: var(--color-fg-tertiary);
        "
      >
        {#each providersData.providers.filter((p) => p.configured) as p}
          <option value={p.id}>{p.label} · {$t('library.provider.default_suffix')}</option>
        {/each}
      </select>
    {/if}
  </div>

  {#if searchError && (mode === 'tracks' || mode === 'albums')}
    <div class="text-sm mb-4" style="color: var(--color-status-error);">{searchError}</div>
  {/if}

  <!-- ─── Album grid (Direction-B signature) ─── -->
  {#if mode === 'albums' && albumResults.length > 0}
    <div class="grid tonus-album-grid" style="grid-template-columns: repeat(4, 1fr); gap: 20px;">
      {#each albumResults as album, i (album.id)}
        <AlbumGridCard
          {album}
          queueState={queuedIds[album.id]}
          loading={queuedIds[album.id]?.kind === 'queued'}
          index={i}
          {provider}
          onqueue={() => queueAlbum(album)}
        />
      {/each}
    </div>

  <!-- ─── Track list (mode=tracks) ─── -->
  {:else if mode === 'tracks' && trackResults.length > 0}
    <div class="space-y-2">
      {#each trackResults as track (track.id)}
        {@const state = queuedIds[track.id]}
        {@const loading = state?.kind === 'queued'}
        <div class="relative" class:skeleton-card={loading} data-track-row>
          <GlassCard padding="sm" interactive>
            <div class="flex items-center gap-4" class:opacity-60={loading}>
              <div data-cover>
                <AlbumArt src={track.album_art} alt={track.album} size="md" />
              </div>
              <div class="flex-1 min-w-0">
                <div class="font-medium text-[15px] truncate" style="color: var(--color-fg-primary);">
                  {track.name}
                </div>
                <div class="text-[13px] truncate" style="color: var(--color-fg-secondary);">
                  {track.artist}
                  {#if track.album}
                    <span style="color: var(--color-fg-tertiary);"> · {track.album}</span>
                  {/if}
                </div>
              </div>
              <div
                class="text-[12px] tabular-nums"
                style="color: var(--color-fg-tertiary); font-family: var(--font-mono);"
              >
                {fmtDuration(track.duration_ms)}
              </div>
              <button
                onclick={(e) => queueTrack(track, e)}
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
                        : accentSoft}; color: {state?.kind === 'exists' || loading
                  ? 'var(--color-fg-secondary)'
                  : '#0a0a0c'}; border: {state?.kind === 'exists' || loading
                  ? '1px solid var(--color-border-soft)'
                  : 'none'}; min-width: 110px; justify-content: center;"
              >
                {#if loading}
                  <span class="skeleton-text">{$t('common.queueing')}</span>
                {:else if state?.kind === 'done'}
                  {$t('common.in_queue')}
                {:else if state?.kind === 'exists'}
                  {$t('common.exists')}
                {:else if state?.kind === 'error'}
                  {$t('common.error')}
                {:else}
                  <Download size={13} strokeWidth={1.8} />
                  {$t('common.download')}
                {/if}
              </button>
            </div>
          </GlassCard>
        </div>
      {/each}
    </div>

  <!-- ─── URL: nur Status + Hint (Eingabe lebt jetzt in der Top-Search-Bar) ─── -->
  {:else if mode === 'url'}
    {#if urlMessage || urlError}
      <div class="flex items-center gap-3 flex-wrap mb-3" style="font-size: 12px;">
        {#if urlMessage}
          <span style="color: var(--color-status-done);">✓ {urlMessage}</span>
        {/if}
        {#if urlError}
          <span style="color: var(--color-status-error);">{urlError}</span>
        {/if}
      </div>
    {/if}
    <p style="font-size: 12px; color: var(--color-fg-tertiary);">
      {$t('library.url.hint')}
    </p>

  <!-- ─── YouTube-Match: Hint + Direct-Action + Lookup-Results ─── -->
  {:else if mode === 'reverse'}
    {#if revLookup || revError}
      <div class="flex items-center gap-3 flex-wrap mb-3" style="font-size: 12px;">
        <button
          onclick={(e) => pickRevRaw(e)}
          class="inline-flex items-center gap-2 transition-colors"
          style="background: rgba(255, 255, 255, 0.04); border: 1px solid var(--color-border-soft); color: var(--color-fg-secondary); padding: 6px 14px; border-radius: 999px; font-size: 11.5px;"
        >
          {$t('library.youtube.direct_button')}
        </button>
        {#if revError}
          <span style="color: var(--color-status-error);">{revError}</span>
        {/if}
      </div>
    {/if}
    {#if !revLookup}
      <p style="font-size: 12px; color: var(--color-fg-tertiary); line-height: 1.55;">
        {$t('library.youtube.hint', { provider: provider || 'Provider' })}
      </p>
    {/if}

    {#if revLookup}
      <div class="space-y-3 mt-4">
        {#if revLookup.youtube?.title}
          <div class="text-[13px]" style="color: var(--color-fg-secondary);">
            <span style="color: var(--color-fg-tertiary);">{$t('library.youtube.label')}</span>
            <span style="color: var(--color-fg-primary);" class="font-medium">
              {revLookup.youtube.title}
            </span>
            {#if revLookup.youtube.channel}· {revLookup.youtube.channel}{/if}
          </div>
        {/if}
        <div class="text-[12px]" style="color: var(--color-fg-tertiary);">
          {$t('library.youtube.candidates_hint', {
            count: revLookup.spotify_candidates.length,
            provider: provider || 'Provider'
          })}
        </div>
        <div class="space-y-2">
          {#each revLookup.spotify_candidates as c (c.id)}
            {@const state = revQueuing[c.id]}
            {@const rLoading = state?.kind === 'queued'}
            <div class="relative" class:skeleton-card={rLoading} data-track-row>
              <GlassCard padding="sm" interactive>
                <div class="flex items-center gap-4" class:opacity-60={rLoading}>
                  <div data-cover>
                    <AlbumArt src={c.album_art} alt={c.album} size="md" />
                  </div>
                  <div class="flex-1 min-w-0">
                    <div class="font-medium text-[14px] truncate" style="color: var(--color-fg-primary);">
                      {c.name}
                    </div>
                    <div class="text-[12px] truncate" style="color: var(--color-fg-secondary);">
                      {c.artist}
                      {#if c.album}
                        <span style="color: var(--color-fg-tertiary);"> · {c.album}</span>
                      {/if}
                    </div>
                  </div>
                  <div
                    class="text-[12px] tabular-nums"
                    style="color: var(--color-fg-tertiary); font-family: var(--font-mono);"
                  >
                    {fmtDuration(c.duration_ms)}
                  </div>
                  <button
                    onclick={(e) => pickRevCandidate(c, e)}
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
                            : accentSoft}; color: {state?.kind === 'exists' || rLoading
                      ? 'var(--color-fg-secondary)'
                      : '#0a0a0c'}; border: {state?.kind === 'exists' || rLoading
                      ? '1px solid var(--color-border-soft)'
                      : 'none'}; min-width: 110px; justify-content: center;"
                  >
                    {#if rLoading}
                      <span class="skeleton-text">{$t('common.queueing')}</span>
                    {:else if state?.kind === 'done'}
                      {$t('common.in_queue')}
                    {:else if state?.kind === 'exists'}
                      {$t('common.exists')}
                    {:else if state?.kind === 'error'}
                      {$t('common.error')}
                    {:else}
                      <Download size={13} strokeWidth={1.8} />
                      {$t('library.button.this_match')}
                    {/if}
                  </button>
                </div>
              </GlassCard>
            </div>
          {/each}
        </div>
      </div>
    {/if}

  <!-- Empty states -->
  {:else if (mode === 'tracks' || mode === 'albums') && query && !searching}
    <!-- Aktive Suche, kein Treffer — kompakter Hinweis statt Full-Page-State.
         Der User soll die Query refinen, nicht zur Onboarding-Seite springen. -->
    <p style="font-size: 12px; color: var(--color-fg-tertiary);">{$t('common.no_results')}</p>
  {:else if (mode === 'tracks' || mode === 'albums') && !query}
    <!-- Library-Onboarding-Empty-State: Hero-Crate-Glyph + Editorial-Copy
         + Tipp-Footer. Wird gezeigt sobald der User auf Tracks/Alben-Mode
         ist und nichts in der Suchleiste steht — typisch direkt nach
         dem ersten Login. -->
    <EmptyState
      glyph="library"
      eyebrow={$t('empty.library.eyebrow')}
      title={$t('empty.library.title')}
      body={$t('empty.library.body')}
    >
      {#snippet actions()}
        <button
          type="button"
          onclick={() => searchInput?.focus()}
          class="inline-flex items-center transition-transform"
          style="
            padding: 11px 22px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            background: {accent};
            color: #0a0a0c;
            border: none;
            box-shadow: 0 8px 24px {accent}40;
            cursor: pointer;
          "
        >
          {$t('empty.library.cta_search')}
        </button>
        <a
          href="{base}/import"
          class="inline-flex items-center transition-colors"
          style="
            padding: 11px 22px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 500;
            letter-spacing: 0.02em;
            background: rgba(255, 255, 255, 0.06);
            color: var(--color-fg-primary);
            border: 1px solid var(--color-border-soft);
            text-decoration: none;
          "
        >
          {$t('empty.library.cta_csv')}
        </a>
        {#if navidromeWebUrl}
          <a
            href={navidromeWebUrl}
            target="_blank"
            rel="noopener noreferrer"
            class="inline-flex items-center transition-colors"
            style="
              padding: 11px 22px;
              border-radius: 999px;
              font-size: 12px;
              font-weight: 500;
              letter-spacing: 0.02em;
              background: transparent;
              color: var(--color-fg-secondary);
              border: 1px dashed var(--color-border-soft);
              text-decoration: none;
            "
          >
            {$t('empty.library.cta_navidrome')} →
          </a>
        {/if}
      {/snippet}
    </EmptyState>
  {/if}
</section>

<style>
  /* Mobile-Pass: Hero stacked, Vinyl ausgeblendet (zu groß für Phone),
     Album-Grid auto-fill statt fixe 4 Spalten. !important schlägt
     die Inline-Styles. */
  @media (max-width: 640px) {
    .tonus-library-hero {
      grid-template-columns: 1fr !important;
      gap: 20px !important;
    }
    .tonus-library-vinyl {
      display: none !important;
    }
    .tonus-album-grid {
      grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)) !important;
      gap: 12px !important;
    }
  }
</style>

