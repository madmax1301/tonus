<script lang="ts">
  import { onDestroy } from 'svelte';
  import {
    importApi,
    providersApi,
    type CsvImportStatus,
    type CsvImportResult,
    type MetadataProvidersResponse
  } from '$lib/api';
  import { defaultProvider, defaultLocation } from '$lib/preferences';
  import { get } from 'svelte/store';
  import GlassCard from '$lib/components/GlassCard.svelte';
  import ProgressLine from '$lib/components/ProgressLine.svelte';
  import { Upload, Loader2, Download } from 'lucide-svelte';

  // ── Provider ────────────────────────────────────────────
  let provider = $state<string>('');
  let providersData = $state<MetadataProvidersResponse | null>(null);

  (async () => {
    try {
      providersData = await providersApi.list();
      const userPref = get(defaultProvider);
      provider = userPref || providersData.default;
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
  let csvLoadMoreBusy = $state(false);
  let csvExportBusy = $state(false);
  let csvExportProgress = $state<{ loaded: number; total: number } | null>(null);
  const PAGE_SIZE = 200;

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
          csvResult = await importApi.result(csvJobId, 0, PAGE_SIZE);
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
        location: $defaultLocation,
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

  async function loadMoreUnmatched() {
    if (!csvJobId || !csvResult) return;
    csvLoadMoreBusy = true;
    try {
      const next = await importApi.result(csvJobId, csvResult.unmatched.length, PAGE_SIZE);
      csvResult = {
        ...csvResult,
        unmatched: [...csvResult.unmatched, ...next.unmatched]
      };
    } catch {
      /* noop */
    } finally {
      csvLoadMoreBusy = false;
    }
  }

  function csvEscape(s: string): string {
    if (s == null) return '';
    const v = String(s);
    if (/[",\n;\r]/.test(v)) return `"${v.replace(/"/g, '""')}"`;
    return v;
  }

  async function exportUnmatched() {
    if (!csvJobId || !csvResult) return;
    csvExportBusy = true;
    csvExportProgress = { loaded: csvResult.unmatched.length, total: csvResult.not_found };
    try {
      const collected = [...csvResult.unmatched];
      const target = csvResult.not_found;
      const PAGE = 500;
      while (collected.length < target) {
        const r = await importApi.result(csvJobId, collected.length, PAGE);
        if (!r.unmatched.length) break;
        collected.push(...r.unmatched);
        csvExportProgress = { loaded: collected.length, total: target };
      }

      const header = 'artist;title;reason';
      const lines = collected.map((u) =>
        [
          csvEscape(u.artist || u.query || ''),
          csvEscape(u.title || ''),
          csvEscape(u.reason || '')
        ].join(';')
      );
      const csv = '﻿' + [header, ...lines].join('\n');
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `tonus-unmatched-${csvJobId}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch {
      /* User can retry */
    } finally {
      csvExportBusy = false;
      csvExportProgress = null;
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
    csvExportProgress = null;
  }

  async function onCsvFile(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const f = input.files?.[0];
    if (!f) return;
    csvText = await f.text();
    input.value = '';
  }

  const csvProgress = $derived(
    csvStatus && csvStatus.total
      ? Math.round((csvStatus.processed / csvStatus.total) * 100)
      : 0
  );
</script>

<section class="space-y-8">
  <header class="space-y-2">
    <h1 class="text-4xl font-semibold tracking-tight" style="color: var(--color-fg-primary);">
      Import
    </h1>
    <p class="text-sm" style="color: var(--color-fg-secondary);">
      Bulk-Import größerer CSV-Listen — eine Zeile pro Track.
      <span style="color: var(--color-fg-tertiary);"
        >Einzelne URLs und Reverse-Lookups laufen auf <a
          href="/"
          style="color: var(--color-accent); text-decoration: underline;">Bibliothek</a
        >.</span
      >
    </p>
  </header>

  {#if providersData}
    <div class="flex items-center gap-2">
      <span class="text-[12px]" style="color: var(--color-fg-tertiary);">Provider</span>
      <select
        bind:value={provider}
        class="text-[12px] px-2 py-1 rounded-md outline-none"
        style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-secondary);"
      >
        {#each providersData.providers.filter((p) => p.configured) as p}
          <option value={p.id}>{p.label}</option>
        {/each}
      </select>
    </div>
  {/if}

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
            <input
              type="file"
              accept=".csv,.tsv,.txt,text/*"
              onchange={onCsvFile}
              class="hidden"
            />
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
          <span style="color: var(--color-fg-primary);" class="font-medium"> CSV-Import läuft </span>
          <span class="tabular-nums" style="color: var(--color-fg-secondary);">
            {csvStatus.processed.toLocaleString('de-DE')} / {csvStatus.total.toLocaleString(
              'de-DE'
            )}
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

    {#if csvResult.not_found > 0}
      <details class="space-y-3" open>
        <summary
          class="cursor-pointer text-[13px] font-medium select-none flex items-center gap-3 flex-wrap"
          style="color: var(--color-fg-primary);"
        >
          <span>{csvResult.not_found.toLocaleString('de-DE')} nicht gefundene Zeilen</span>
          <span class="text-[11px] font-normal" style="color: var(--color-fg-tertiary);">
            · zeige {csvResult.unmatched.length.toLocaleString('de-DE')}
          </span>
          <span class="ml-auto flex items-center gap-2">
            <button
              type="button"
              onclick={(e) => {
                e.preventDefault();
                exportUnmatched();
              }}
              disabled={csvExportBusy}
              class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-medium transition-colors disabled:opacity-50"
              style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-secondary);"
            >
              {#if csvExportBusy && csvExportProgress}
                <Loader2 size={11} class="animate-spin" />
                CSV {csvExportProgress.loaded.toLocaleString('de-DE')} / {csvExportProgress.total.toLocaleString(
                  'de-DE'
                )}
              {:else}
                <Download size={11} strokeWidth={1.8} />
                Als CSV exportieren
              {/if}
            </button>
          </span>
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
        {#if csvResult.unmatched.length < csvResult.not_found}
          <button
            onclick={loadMoreUnmatched}
            disabled={csvLoadMoreBusy}
            class="w-full inline-flex items-center justify-center gap-2 px-3 py-2 rounded-md text-[12px] transition-colors disabled:opacity-50"
            style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-secondary);"
          >
            {#if csvLoadMoreBusy}
              <Loader2 size={13} class="animate-spin" />
              lade …
            {:else}
              Mehr laden
              <span class="text-[11px]" style="color: var(--color-fg-tertiary);">
                · noch {(csvResult.not_found - csvResult.unmatched.length).toLocaleString('de-DE')}
              </span>
            {/if}
          </button>
        {/if}
      </details>
    {/if}
  {/if}
</section>
