<script lang="ts">
  import { onMount } from 'svelte';
  import {
    searchApi,
    downloadApi,
    providersApi,
    ApiError,
    type Track,
    type MetadataProvidersResponse
  } from '$lib/api';
  import GlassCard from '$lib/components/GlassCard.svelte';
  import AlbumArt from '$lib/components/AlbumArt.svelte';
  import { Search, Download, Loader2 } from 'lucide-svelte';

  let query = $state('');
  let provider = $state<string>('');
  let providersData = $state<MetadataProvidersResponse | null>(null);
  let results = $state<Track[]>([]);
  let searching = $state(false);
  let searchError = $state<string | null>(null);
  let queuedIds = $state<Record<string, 'queued' | 'done' | 'error'>>({});

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
      results = [];
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
      results = await searchApi.tracks(query.trim(), provider || undefined, 20);
    } catch (err) {
      results = [];
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        searchError = null; // sheet öffnet sich
      } else {
        searchError = err instanceof Error ? err.message : 'Suche fehlgeschlagen';
      }
    } finally {
      searching = false;
    }
  }

  async function queue(track: Track) {
    queuedIds = { ...queuedIds, [track.id]: 'queued' };
    try {
      await downloadApi.start(track.id, { location: 'navidrome', provider: provider || undefined });
      queuedIds = { ...queuedIds, [track.id]: 'done' };
    } catch (err) {
      queuedIds = { ...queuedIds, [track.id]: 'error' };
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
        placeholder="Track, Artist oder Album …"
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

  {#if searchError}
    <div class="text-sm" style="color: var(--color-status-error);">{searchError}</div>
  {/if}

  {#if results.length > 0}
    <div class="space-y-2">
      {#each results as track (track.id)}
        {@const state = queuedIds[track.id]}
        <GlassCard padding="sm" interactive>
          <div class="flex items-center gap-4">
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
              onclick={() => queue(track)}
              disabled={state === 'queued' || state === 'done'}
              class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium transition-all disabled:opacity-60"
              style="background: {state === 'done'
                ? 'var(--color-status-done)'
                : state === 'error'
                  ? 'var(--color-status-error)'
                  : 'var(--color-accent)'}; color: #1a1410;"
            >
              {#if state === 'queued'}
                <Loader2 size={13} class="animate-spin" />
                queue …
              {:else if state === 'done'}
                ✓ in Queue
              {:else if state === 'error'}
                Fehler
              {:else}
                <Download size={13} strokeWidth={1.8} />
                Download
              {/if}
            </button>
          </div>
        </GlassCard>
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
