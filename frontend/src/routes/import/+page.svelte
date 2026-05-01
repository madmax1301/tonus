<script lang="ts">
  import { onDestroy } from 'svelte';
  import {
    importApi,
    urlApi,
    reverseApi,
    providersApi,
    ApiError,
    type CsvImportStatus,
    type CsvImportResult,
    type ReverseLookupResult,
    type Track,
    type MetadataProvidersResponse
  } from '$lib/api';
  import GlassCard from '$lib/components/GlassCard.svelte';
  import AlbumArt from '$lib/components/AlbumArt.svelte';
  import ProgressLine from '$lib/components/ProgressLine.svelte';
  import { Upload, Link2, Youtube, Loader2, Download, FileText } from 'lucide-svelte';

  type Tab = 'csv' | 'url' | 'reverse';
  let tab = $state<Tab>('csv');

  // ── Provider (shared) ───────────────────────────────────
  let provider = $state<string>('');
  let providersData = $state<MetadataProvidersResponse | null>(null);

  (async () => {
    try {
      providersData = await providersApi.list();
      provider = providersData.default;
    } catch {
      /* sheet handles auth */
    }
  })();

  // ── CSV ──────────────────────────────────────────────────
  let csvText = $state('');
  let csvJobId = $state<string | null>(null);
  let csvStatus = $state<CsvImportStatus | null>(null);
  let csvResult = $state<CsvImportResult | null>(null);
  let csvBusy = $state(false);
  let csvError = $state<string | null>(null);
  let csvQueueAllBusy = $state(false);
  let csvQueueAllResult = $state<string | null>(null);

  let csvPollTimer: ReturnType<typeof setInterval> | null = null;

  function stopCsvPoll() {
    if (csvPollTimer) {
      clearInterval(csvPollTimer);
      csvPollTimer = null;
    }
  }
  onDestroy(stopCsvPoll);

  async function startCsv() {
    if (!csvText.trim()) return;
    csvBusy = true;
    csvError = null;
    csvResult = null;
    csvQueueAllResult = null;
    try {
      const r = await importApi.startCsv(csvText, provider || undefined);
      csvJobId = r.job_id;
      csvStatus = {
        status: 'queued',
        total: r.total ?? 0,
        processed: 0,
        found: 0,
        not_found: 0,
        message: r.message
      };
      pollCsv();
    } catch (err) {
      csvError = err instanceof Error ? err.message : 'CSV-Import fehlgeschlagen';
    } finally {
      csvBusy = false;
    }
  }

  function pollCsv() {
    if (!csvJobId) return;
    stopCsvPoll();
    const tick = async () => {
      if (!csvJobId) return;
      try {
        const s = await importApi.status(csvJobId);
        csvStatus = s;
        if (s.status === 'completed') {
          stopCsvPoll();
          csvResult = await importApi.result(csvJobId);
        } else if (s.status === 'error') {
          stopCsvPoll();
          csvError = s.message ?? 'CSV-Import-Fehler';
        }
      } catch {
        /* keep polling */
      }
    };
    tick();
    csvPollTimer = setInterval(tick, 1500);
  }

  async function queueAllMatched() {
    if (!csvJobId) return;
    csvQueueAllBusy = true;
    csvQueueAllResult = null;
    try {
      const r = await importApi.queueAll(csvJobId, {
        location: 'navidrome',
        provider: provider || undefined
      });
      const queued = r.queued ?? 0;
      const skipped = r.skipped ?? 0;
      csvQueueAllResult =
        skipped > 0 ? `${queued} queued, ${skipped} schon da` : `${queued} queued`;
    } catch (err) {
      csvQueueAllResult = err instanceof Error ? err.message : 'Fehler beim Queuen';
    } finally {
      csvQueueAllBusy = false;
    }
  }

  function resetCsv() {
    stopCsvPoll();
    csvJobId = null;
    csvStatus = null;
    csvResult = null;
    csvError = null;
    csvText = '';
    csvQueueAllResult = null;
  }

  async function onCsvFile(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const f = input.files?.[0];
    if (!f) return;
    csvText = await f.text();
    input.value = '';
  }

  // ── URL ──────────────────────────────────────────────────
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
      const r = await urlApi.download(urlInput.trim(), { location: 'navidrome' });
      urlMessage = r.message ?? `In Queue als ${r.job_id}`;
      urlInput = '';
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const detail =
          err.body && typeof err.body === 'object' && 'detail' in err.body
            ? String((err.body as { detail: unknown }).detail)
            : 'bereits vorhanden';
        urlMessage = `✓ ${detail}`;
      } else {
        urlError = err instanceof Error ? err.message : 'URL-Download fehlgeschlagen';
      }
    } finally {
      urlBusy = false;
    }
  }

  // ── Reverse YouTube ──────────────────────────────────────
  let revUrl = $state('');
  let revBusy = $state(false);
  let revLookup = $state<ReverseLookupResult | null>(null);
  let revError = $state<string | null>(null);
  let revQueuing = $state<Record<string, 'queued' | 'done' | 'exists' | 'error'>>({});

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

  async function pickCandidate(c: Track) {
    if (!revLookup) return;
    revQueuing = { ...revQueuing, [c.id]: 'queued' };
    try {
      await reverseApi.download(revLookup.query || revUrl.trim(), c, {
        location: 'navidrome',
        provider: provider || undefined
      });
      revQueuing = { ...revQueuing, [c.id]: 'done' };
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        revQueuing = { ...revQueuing, [c.id]: 'exists' };
      } else {
        revQueuing = { ...revQueuing, [c.id]: 'error' };
      }
    }
  }

  async function pickRaw() {
    if (!revUrl.trim()) return;
    try {
      await reverseApi.download(revUrl.trim(), null, { location: 'navidrome' });
      revError = null;
      revLookup = null;
      revUrl = '';
    } catch (err) {
      revError = err instanceof Error ? err.message : 'Direkter Download fehlgeschlagen';
    }
  }

  function fmtDuration(ms: number): string {
    if (!ms) return '';
    const s = Math.round(ms / 1000);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  const csvProgress = $derived(
    csvStatus && csvStatus.total
      ? Math.round((csvStatus.processed / csvStatus.total) * 100)
      : 0
  );

  const tabs: { id: Tab; label: string; icon: typeof Upload }[] = [
    { id: 'csv', label: 'CSV', icon: FileText },
    { id: 'url', label: 'URL', icon: Link2 },
    { id: 'reverse', label: 'Reverse YouTube', icon: Youtube }
  ];
</script>

<section class="space-y-8">
  <header class="space-y-2">
    <h1 class="text-4xl font-semibold tracking-tight" style="color: var(--color-fg-primary);">
      Import
    </h1>
    <p class="text-sm" style="color: var(--color-fg-secondary);">
      CSV-Bulk-Import, beliebige URLs (yt-dlp), Reverse-Lookup für YouTube-Tracks.
    </p>
  </header>

  <!-- Tabs -->
  <div class="flex items-center gap-1">
    {#each tabs as t}
      <button
        onclick={() => (tab = t.id)}
        class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[12px] transition-colors"
        style="background: {tab === t.id
          ? 'var(--color-accent)'
          : 'transparent'}; color: {tab === t.id
          ? '#1a1410'
          : 'var(--color-fg-secondary)'}; border: 1px solid {tab === t.id
          ? 'transparent'
          : 'var(--color-border-soft)'};"
      >
        <svelte:component this={t.icon} size={13} strokeWidth={1.5} />
        {t.label}
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

  {#if tab === 'csv'}
    <!-- ────────── CSV ────────── -->
    {#if !csvJobId}
      <GlassCard padding="md">
        <div class="space-y-3">
          <label class="block space-y-2">
            <span class="text-[13px] font-medium" style="color: var(--color-fg-primary);"
              >CSV-Inhalt</span
            >
            <textarea
              bind:value={csvText}
              rows="10"
              spellcheck="false"
              placeholder={'Format: Künstler;Titel\noder: Künstler,Titel\noder: 1 Zeile pro Track als Freitext'}
              class="w-full px-3 py-2.5 rounded-md text-[13px] font-mono outline-none resize-y focus:border-[var(--color-accent)]"
              style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-primary);"
            ></textarea>
          </label>
          <div class="flex items-center gap-3">
            <label
              class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] cursor-pointer transition-colors"
              style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-secondary);"
            >
              <Upload size={13} strokeWidth={1.5} />
              CSV-Datei wählen
              <input type="file" accept=".csv,.tsv,.txt,text/*" onchange={onCsvFile} class="hidden" />
            </label>
            <button
              onclick={startCsv}
              disabled={csvBusy || !csvText.trim()}
              class="inline-flex items-center gap-2 px-4 py-2 rounded-md text-[13px] font-medium transition-opacity disabled:opacity-40"
              style="background: var(--color-accent); color: #1a1410;"
            >
              {#if csvBusy}
                <Loader2 size={13} class="animate-spin" />
                Lade …
              {:else}
                Import starten
              {/if}
            </button>
            <span class="text-[12px]" style="color: var(--color-fg-tertiary);">
              {csvText.split(/\r?\n/).filter((l) => l.trim()).length} Zeilen
            </span>
          </div>
          {#if csvError}
            <div class="text-sm" style="color: var(--color-status-error);">{csvError}</div>
          {/if}
        </div>
      </GlassCard>
    {:else if csvStatus && csvStatus.status !== 'completed'}
      <GlassCard padding="md">
        <div class="space-y-3">
          <div class="flex items-center justify-between text-[13px]">
            <span style="color: var(--color-fg-primary);" class="font-medium">
              CSV-Import läuft
            </span>
            <span class="tabular-nums" style="color: var(--color-fg-secondary);">
              {csvStatus.processed.toLocaleString('de-DE')} / {csvStatus.total.toLocaleString('de-DE')}
            </span>
          </div>
          <ProgressLine
            value={csvProgress > 0 ? csvProgress : undefined}
            pareto={csvProgress === 0}
          />
          <div class="text-[12px]" style="color: var(--color-fg-tertiary);">
            {csvStatus.found} matched · {csvStatus.not_found} nicht gefunden
            {#if csvStatus.message}
              <span class="ml-2">· {csvStatus.message}</span>
            {/if}
          </div>
        </div>
      </GlassCard>
    {:else if csvResult}
      <GlassCard padding="md">
        <div class="flex items-end justify-between gap-4 flex-wrap">
          <div>
            <div
              class="text-[12px] uppercase tracking-widest"
              style="color: var(--color-fg-tertiary);"
            >
              CSV-Import abgeschlossen
            </div>
            <div class="text-[20px] font-semibold mt-1" style="color: var(--color-fg-primary);">
              {csvResult.found.toLocaleString('de-DE')} matched
              <span style="color: var(--color-fg-tertiary);" class="font-normal">/</span>
              <span style="color: var(--color-status-error);"
                >{csvResult.not_found.toLocaleString('de-DE')} nicht gefunden</span
              >
            </div>
          </div>
          <div class="flex items-center gap-3">
            <button
              onclick={queueAllMatched}
              disabled={csvQueueAllBusy || csvResult.found === 0}
              class="inline-flex items-center gap-2 px-4 py-2 rounded-md text-[13px] font-medium transition-opacity disabled:opacity-40"
              style="background: var(--color-accent); color: #1a1410;"
            >
              {#if csvQueueAllBusy}
                <Loader2 size={13} class="animate-spin" />
              {:else}
                <Download size={13} strokeWidth={1.8} />
              {/if}
              Alle matched queuen
            </button>
            <button
              onclick={resetCsv}
              class="inline-flex items-center gap-2 px-3 py-2 rounded-md text-[12px] transition-colors"
              style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-secondary);"
            >
              Neuer Import
            </button>
          </div>
        </div>
        {#if csvQueueAllResult}
          <div class="mt-3 text-[12px]" style="color: var(--color-fg-secondary);">
            {csvQueueAllResult}
          </div>
        {/if}
      </GlassCard>

      {#if csvResult.unmatched.length > 0}
        <details class="space-y-2" open>
          <summary
            class="cursor-pointer text-[13px] font-medium select-none"
            style="color: var(--color-fg-primary);"
          >
            {csvResult.unmatched.length.toLocaleString('de-DE')} nicht gefundene Zeilen
          </summary>
          <div class="space-y-1">
            {#each csvResult.unmatched as u}
              <GlassCard padding="sm">
                <div class="flex items-center justify-between gap-3">
                  <div class="text-[13px] truncate" style="color: var(--color-fg-secondary);">
                    {u.artist || u.query || u.raw_line || '?'}
                    {#if u.title}
                      <span style="color: var(--color-fg-tertiary);"> · {u.title}</span>
                    {/if}
                  </div>
                  {#if u.reason}
                    <div
                      class="text-[11px] flex-shrink-0"
                      style="color: var(--color-fg-tertiary);"
                    >
                      {u.reason}
                    </div>
                  {/if}
                </div>
              </GlassCard>
            {/each}
          </div>
        </details>
      {/if}
    {/if}
  {:else if tab === 'url'}
    <!-- ────────── URL ────────── -->
    <GlassCard padding="md">
      <div class="space-y-3">
        <label class="block space-y-2">
          <span class="text-[13px] font-medium" style="color: var(--color-fg-primary);">
            URL (YouTube, SoundCloud, Bandcamp, Vimeo, …)
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
        <div class="flex items-center gap-3">
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
      Quelle. Brauchst du saubere Tags, nutze stattdessen den <strong>Reverse YouTube</strong>-Tab.
    </p>
  {:else if tab === 'reverse'}
    <!-- ────────── Reverse YouTube ────────── -->
    <GlassCard padding="md">
      <div class="space-y-3">
        <label class="block space-y-2">
          <span class="text-[13px] font-medium" style="color: var(--color-fg-primary);">
            YouTube-URL
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
              onclick={pickRaw}
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
        <header class="text-[13px]" style="color: var(--color-fg-secondary);">
          {#if revLookup.youtube?.title}
            <span style="color: var(--color-fg-tertiary);">YouTube:</span>
            <span style="color: var(--color-fg-primary);" class="font-medium">
              {revLookup.youtube.title}
            </span>
            {#if revLookup.youtube.channel}
              · {revLookup.youtube.channel}
            {/if}
          {/if}
        </header>
        <div class="text-[12px] mb-2" style="color: var(--color-fg-tertiary);">
          {revLookup.spotify_candidates.length} mögliche Treffer · wähle einen für Tags + Cover
        </div>
        <div class="space-y-2">
          {#each revLookup.spotify_candidates as c (c.id)}
            {@const state = revQueuing[c.id]}
            {@const loading = state === 'queued'}
            <div class="relative" class:skeleton-card={loading}>
              <GlassCard padding="sm" interactive>
                <div class="flex items-center gap-4" class:opacity-60={loading}>
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
                  <div class="text-[12px] tabular-nums" style="color: var(--color-fg-tertiary);">
                    {fmtDuration(c.duration_ms)}
                  </div>
                  <button
                    onclick={() => pickCandidate(c)}
                    disabled={loading || state === 'done' || state === 'exists'}
                    class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium transition-all disabled:cursor-default"
                    style="background: {state === 'done'
                      ? 'var(--color-status-done)'
                      : state === 'exists'
                        ? 'var(--color-surface-3)'
                        : state === 'error'
                          ? 'var(--color-status-error)'
                          : loading
                            ? 'var(--color-surface-3)'
                            : 'var(--color-accent)'}; color: {state === 'exists' || loading
                      ? 'var(--color-fg-secondary)'
                      : '#1a1410'}; border: {state === 'exists' || loading
                      ? '1px solid var(--color-border-soft)'
                      : 'none'}; min-width: 110px; justify-content: center;"
                  >
                    {#if loading}
                      <span class="skeleton-text">queue …</span>
                    {:else if state === 'done'}
                      ✓ in Queue
                    {:else if state === 'exists'}
                      ✓ vorhanden
                    {:else if state === 'error'}
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
  {/if}
</section>
