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
  import CinemaBackdrop from '$lib/components/CinemaBackdrop.svelte';
  import VinylWithCover from '$lib/components/VinylWithCover.svelte';
  import ProgressLine from '$lib/components/ProgressLine.svelte';
  import { tint, DEFAULT_HUE } from '$lib/accent';
  import { Upload, Loader2, Download, FileText, X } from 'lucide-svelte';

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

  // ── Drag & Drop ──────────────────────────────────────────
  let dragOver = $state(false);
  let dragDepth = 0;

  function onDragEnter(e: DragEvent) {
    if (!e.dataTransfer?.types.includes('Files')) return;
    e.preventDefault();
    dragDepth++;
    dragOver = true;
  }
  function onDragOver(e: DragEvent) {
    if (!e.dataTransfer?.types.includes('Files')) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  }
  function onDragLeave(e: DragEvent) {
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dragOver = false;
  }
  async function onDrop(e: DragEvent) {
    e.preventDefault();
    dragDepth = 0;
    dragOver = false;
    const f = e.dataTransfer?.files?.[0];
    if (!f) return;
    csvText = await f.text();
  }

  const csvProgress = $derived(
    csvStatus && csvStatus.total
      ? Math.round((csvStatus.processed / csvStatus.total) * 100)
      : 0
  );

  // Accent — Import hat keinen Cover-Hue, daher konstant Default-Gold.
  const accent = $derived(tint(DEFAULT_HUE));
  const accentSoft = $derived(tint(DEFAULT_HUE, 0.95));

  const lineCount = $derived(csvText.split(/\r?\n/).filter((l) => l.trim()).length);
</script>

<CinemaBackdrop hue={DEFAULT_HUE} />

<section class="relative z-10 mx-auto max-w-[1180px] w-full" style="padding: 40px 36px 50px;">
  <!-- ─── Editorial Hero ───────────────────────────────────── -->
  <div
    class="grid items-center"
    style="grid-template-columns: 1.3fr 1fr; gap: 48px; margin-bottom: 48px;"
  >
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
        Bulk Import
      </div>
      <h1
        class="font-semibold m-0"
        style="
          font-family: var(--font-display);
          font-size: 48px;
          font-weight: 600;
          line-height: 0.95;
          letter-spacing: -0.035em;
        "
      >
        Hunderte Tracks.<br />
        <em style="color: {accent}; font-weight: 400; font-style: italic;">Eine Liste.</em>
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
        CSV oder Freitext rein — Tonus matcht jede Zeile gegen Deezer/Spotify und queued
        sauber, was zu finden war. Was nicht passt, kannst du als CSV exportieren.
      </p>
    </div>
    <div class="flex justify-center">
      <VinylWithCover
        src={null}
        alt=""
        artist="Liste"
        year={lineCount > 0 ? lineCount : ''}
        size={260}
        spinning={!!csvJobId && csvStatus?.status !== 'completed'}
      />
    </div>
  </div>

  <!-- ─── Mode strip — Provider als underline-Pill (analog Library) ─── -->
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
    <div
      class="relative inline-flex items-center"
      style="
        color: var(--color-fg-primary);
        font-weight: 500;
        padding-bottom: 14px;
        margin-bottom: -14px;
        border-bottom: 2px solid {accent};
      "
    >
      CSV
    </div>
    {#if csvJobId}
      <div class="text-[12px]" style="color: var(--color-fg-tertiary);">
        Job · <span class="font-mono" style="font-family: var(--font-mono);">{csvJobId.slice(0, 8)}</span>
      </div>
    {/if}
    {#if providersData}
      <select
        bind:value={provider}
        disabled={!!csvJobId}
        class="ml-auto text-[11px] px-2 py-1 rounded-md outline-none disabled:opacity-60"
        style="
          background: rgba(255, 255, 255, 0.02);
          border: 1px solid var(--color-border-soft);
          color: var(--color-fg-tertiary);
        "
      >
        {#each providersData.providers.filter((p) => p.configured) as p}
          <option value={p.id}>{p.label} · Provider</option>
        {/each}
      </select>
    {/if}
  </div>

  {#if !csvJobId}
    <!-- ─── Glass Drop-Zone ─────────────────────────────────── -->
    <div
      role="region"
      aria-label="CSV-Eingabe"
      class="relative overflow-hidden tonus-fadein"
      style="
        background: rgba(20, 20, 24, 0.5);
        backdrop-filter: blur(40px) saturate(1.2);
        -webkit-backdrop-filter: blur(40px) saturate(1.2);
        border: 1px solid {dragOver ? accent : 'var(--color-border-soft)'};
        border-radius: 22px;
        padding: 28px;
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);
        transition: border-color 0.18s ease;
      "
      ondragenter={onDragEnter}
      ondragover={onDragOver}
      ondragleave={onDragLeave}
      ondrop={onDrop}
    >
      <!-- Drag overlay -->
      {#if dragOver}
        <div
          class="absolute inset-0 z-10 flex items-center justify-center pointer-events-none"
          style="
            background: rgba(20, 20, 24, 0.85);
            border: 2px dashed {accent};
            border-radius: 22px;
            backdrop-filter: blur(8px);
          "
        >
          <div class="text-center">
            <Upload size={48} strokeWidth={1.2} style="color: {accent}; margin-inline: auto;" />
            <div
              class="font-semibold uppercase mt-3"
              style="font-size: 11px; letter-spacing: 0.24em; color: {accent};"
            >
              Drop to import
            </div>
          </div>
        </div>
      {/if}

      <!-- Header row: title + actions -->
      <div class="flex items-center justify-between flex-wrap gap-3 mb-4">
        <div>
          <div
            class="font-semibold uppercase"
            style="
              font-size: 11px;
              letter-spacing: 0.2em;
              color: var(--color-fg-tertiary);
            "
          >
            Eingabe
          </div>
          <div
            class="mt-1"
            style="
              font-family: var(--font-display);
              font-size: 22px;
              font-weight: 500;
              letter-spacing: -0.015em;
              color: var(--color-fg-primary);
            "
          >
            Datei droppen oder Liste einfügen
          </div>
        </div>
        <div class="flex items-center gap-2">
          <label
            class="inline-flex items-center gap-1.5 cursor-pointer transition-colors"
            style="
              background: rgba(255, 255, 255, 0.04);
              border: 1px solid var(--color-border-soft);
              color: var(--color-fg-secondary);
              padding: 6px 14px;
              border-radius: 999px;
              font-size: 11.5px;
            "
          >
            <Upload size={12} strokeWidth={1.5} />
            CSV-Datei wählen
            <input
              type="file"
              accept=".csv,.tsv,.txt,text/*"
              onchange={onCsvFile}
              class="hidden"
            />
          </label>
          {#if csvText}
            <button
              type="button"
              onclick={() => (csvText = '')}
              class="inline-flex items-center gap-1.5 transition-colors"
              style="
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid var(--color-border-soft);
                color: var(--color-fg-tertiary);
                padding: 6px 12px;
                border-radius: 999px;
                font-size: 11px;
              "
              aria-label="Zurücksetzen"
            >
              <X size={11} strokeWidth={1.5} />
              Leeren
            </button>
          {/if}
        </div>
      </div>

      <textarea
        bind:value={csvText}
        rows="10"
        spellcheck="false"
        placeholder={'Künstler;Titel\nDaft Punk;Get Lucky\nQueen;Bohemian Rhapsody\n\noder eine Zeile pro Track als Freitext'}
        class="w-full px-4 py-3 outline-none resize-y"
        style="
          background: rgba(0, 0, 0, 0.25);
          border: 1px solid var(--color-border-soft);
          border-radius: 14px;
          color: var(--color-fg-primary);
          font-family: var(--font-mono);
          font-size: 12.5px;
          line-height: 1.55;
        "
      ></textarea>

      <!-- Footer row: action -->
      <div class="flex items-center justify-between flex-wrap gap-3 mt-4">
        <div class="text-[12px]" style="color: var(--color-fg-tertiary);">
          {#if lineCount > 0}
            <span style="color: {accent}; font-weight: 500;">{lineCount.toLocaleString('de-DE')}</span>
            <span> Zeile{lineCount === 1 ? '' : 'n'} bereit</span>
          {:else}
            Liste einfügen oder Datei droppen — Format: <code style="font-family: var(--font-mono); color: var(--color-fg-secondary);">Künstler;Titel</code>
          {/if}
        </div>
        <button
          onclick={startCsv}
          disabled={csvBusy || !csvText.trim()}
          class="inline-flex items-center gap-2 transition-opacity disabled:opacity-40"
          style="
            background: {accent};
            color: #1a1410;
            padding: 10px 22px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            box-shadow: 0 8px 20px rgba(200, 169, 106, 0.25);
          "
        >
          {#if csvBusy}
            <Loader2 size={13} class="animate-spin" />
            Lade …
          {:else}
            <FileText size={13} strokeWidth={2} />
            Import starten
          {/if}
        </button>
      </div>
      {#if csvError}
        <div
          class="mt-3 text-[12px]"
          style="color: var(--color-status-error);"
        >
          {csvError}
        </div>
      {/if}
    </div>
  {:else if csvStatus && csvStatus.status !== 'completed'}
    <!-- ─── Live-Card during import ─────────────────────────── -->
    <div
      class="relative overflow-hidden tonus-fadein"
      style="
        background: rgba(20, 20, 24, 0.55);
        backdrop-filter: blur(40px) saturate(1.2);
        -webkit-backdrop-filter: blur(40px) saturate(1.2);
        border: 1px solid var(--color-border-soft);
        border-radius: 22px;
        padding: 32px;
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);
      "
    >
      <div class="flex items-center gap-3 mb-3">
        <span
          class="inline-block rounded-full"
          style="width: 8px; height: 8px; background: {accent}; box-shadow: 0 0 12px {accent};"
        ></span>
        <div
          class="font-semibold uppercase"
          style="
            font-size: 11px;
            letter-spacing: 0.24em;
            color: {accent};
          "
        >
          Live · matching against {provider || 'provider'}
        </div>
      </div>

      <div
        class="font-medium mb-4"
        style="
          font-family: var(--font-display);
          font-size: 36px;
          font-weight: 500;
          letter-spacing: -0.025em;
          line-height: 1.05;
          color: var(--color-fg-primary);
        "
      >
        <span class="tabular-nums">{csvStatus.processed.toLocaleString('de-DE')}</span>
        <span style="color: var(--color-fg-tertiary); font-weight: 300;"> / </span>
        <span class="tabular-nums" style="color: var(--color-fg-secondary);">{csvStatus.total.toLocaleString('de-DE')}</span>
        <span
          style="
            font-size: 14px;
            color: var(--color-fg-tertiary);
            font-weight: 400;
            margin-left: 12px;
            letter-spacing: 0;
          "
        >
          Tracks verarbeitet
        </span>
      </div>

      <ProgressLine
        value={csvProgress > 0 ? csvProgress : undefined}
        pareto={csvProgress === 0}
        height={3}
        color={accent}
        glow
      />

      <div class="flex items-center gap-6 mt-4 text-[12px] tabular-nums">
        <div>
          <span style="color: var(--color-fg-tertiary);">matched</span>
          <span class="ml-1.5 font-medium" style="color: var(--color-status-done);"
            >{csvStatus.found.toLocaleString('de-DE')}</span
          >
        </div>
        <div>
          <span style="color: var(--color-fg-tertiary);">nicht gefunden</span>
          <span class="ml-1.5 font-medium" style="color: var(--color-status-error);"
            >{csvStatus.not_found.toLocaleString('de-DE')}</span
          >
        </div>
        {#if csvStatus.message}
          <div class="ml-auto text-[11px]" style="color: var(--color-fg-tertiary);">
            {csvStatus.message}
          </div>
        {/if}
      </div>
    </div>
  {:else if csvResult}
    <!-- ─── Result Hero ─────────────────────────────────────── -->
    <div
      class="relative overflow-hidden tonus-fadein"
      style="
        background: rgba(20, 20, 24, 0.55);
        backdrop-filter: blur(40px) saturate(1.2);
        -webkit-backdrop-filter: blur(40px) saturate(1.2);
        border: 1px solid var(--color-border-soft);
        border-radius: 22px;
        padding: 32px;
        margin-bottom: 22px;
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);
      "
    >
      <div class="flex items-end justify-between flex-wrap gap-4">
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
            Import abgeschlossen
          </div>
          <div
            class="font-medium m-0"
            style="
              font-family: var(--font-display);
              font-size: 56px;
              font-weight: 500;
              line-height: 0.95;
              letter-spacing: -0.035em;
              color: var(--color-fg-primary);
            "
          >
            <span class="tabular-nums">{csvResult.found.toLocaleString('de-DE')}</span>
            <span style="font-size: 22px; color: var(--color-fg-tertiary); font-weight: 300; letter-spacing: 0;">
              matched
            </span>
          </div>
          <div class="mt-3 text-[14px]" style="color: var(--color-fg-secondary);">
            {#if csvResult.not_found > 0}
              <span style="color: var(--color-status-error); font-weight: 500;"
                >{csvResult.not_found.toLocaleString('de-DE')}</span
              >
              <span style="color: var(--color-fg-tertiary);"> nicht gefunden</span>
            {:else}
              Alles gefunden — saubere Liste
            {/if}
          </div>
        </div>
        <div class="flex items-center gap-3">
          <button
            onclick={resetCsv}
            class="inline-flex items-center gap-1.5 transition-colors"
            style="
              background: rgba(255, 255, 255, 0.04);
              border: 1px solid var(--color-border-soft);
              color: var(--color-fg-secondary);
              padding: 8px 16px;
              border-radius: 999px;
              font-size: 12px;
            "
          >
            Neuer Import
          </button>
          <button
            onclick={queueAllMatched}
            disabled={csvQueueAllBusy || csvResult.found === 0}
            class="inline-flex items-center gap-2 transition-opacity disabled:opacity-40"
            style="
              background: {accent};
              color: #1a1410;
              padding: 10px 22px;
              border-radius: 999px;
              font-size: 12px;
              font-weight: 600;
              letter-spacing: 0.04em;
              text-transform: uppercase;
              box-shadow: 0 8px 20px rgba(200, 169, 106, 0.25);
            "
          >
            {#if csvQueueAllBusy}
              <Loader2 size={13} class="animate-spin" />
              Queue …
            {:else}
              <Download size={13} strokeWidth={2} />
              {csvResult.found.toLocaleString('de-DE')} queuen
            {/if}
          </button>
        </div>
      </div>
      {#if csvQueueAllResult}
        <div class="mt-4 text-[12px]" style="color: var(--color-fg-secondary);">
          ✓ {csvQueueAllResult}
        </div>
      {/if}
    </div>

    <!-- ─── Unmatched list ─────────────────────────────────── -->
    {#if csvResult.not_found > 0}
      <div
        class="flex items-center justify-between flex-wrap gap-3"
        style="
          margin-bottom: 14px;
          padding-bottom: 12px;
          border-bottom: 1px solid var(--color-border-soft);
        "
      >
        <div>
          <div
            class="font-semibold uppercase"
            style="
              font-size: 11px;
              letter-spacing: 0.24em;
              color: var(--color-fg-tertiary);
            "
          >
            Nicht gefunden
          </div>
          <div
            class="mt-1 text-[15px]"
            style="color: var(--color-fg-primary); font-weight: 500;"
          >
            <span class="tabular-nums">{csvResult.unmatched.length.toLocaleString('de-DE')}</span>
            <span style="color: var(--color-fg-tertiary); font-weight: 400;">
              von {csvResult.not_found.toLocaleString('de-DE')} angezeigt
            </span>
          </div>
        </div>
        <button
          type="button"
          onclick={exportUnmatched}
          disabled={csvExportBusy}
          class="inline-flex items-center gap-1.5 transition-colors disabled:opacity-50"
          style="
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--color-border-soft);
            color: var(--color-fg-secondary);
            padding: 6px 14px;
            border-radius: 999px;
            font-size: 11.5px;
          "
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
      </div>

      <div class="space-y-1.5">
        {#each csvResult.unmatched as u, i}
          <div
            class="flex items-center justify-between gap-3 px-4 py-2.5 transition-colors"
            style="
              background: rgba(20, 20, 24, 0.4);
              backdrop-filter: blur(20px);
              -webkit-backdrop-filter: blur(20px);
              border: 1px solid var(--color-border-soft);
              border-radius: 12px;
            "
          >
            <div class="flex items-center gap-3 min-w-0 flex-1">
              <span
                class="text-[10.5px] tabular-nums flex-shrink-0"
                style="color: var(--color-fg-tertiary); font-family: var(--font-mono); width: 36px;"
              >
                {String(i + 1).padStart(3, '0')}
              </span>
              <div class="text-[13px] truncate" style="color: var(--color-fg-secondary);">
                <span style="color: var(--color-fg-primary);">{u.artist || u.query || u.raw_line || '?'}</span>
                {#if u.title}
                  <span style="color: var(--color-fg-tertiary);"> · {u.title}</span>
                {/if}
              </div>
            </div>
            {#if u.reason}
              <div
                class="text-[10.5px] flex-shrink-0 uppercase"
                style="color: var(--color-fg-tertiary); letter-spacing: 0.08em;"
              >
                {u.reason}
              </div>
            {/if}
          </div>
        {/each}
      </div>

      {#if csvResult.unmatched.length < csvResult.not_found}
        <button
          onclick={loadMoreUnmatched}
          disabled={csvLoadMoreBusy}
          class="w-full inline-flex items-center justify-center gap-2 transition-colors disabled:opacity-50"
          style="
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--color-border-soft);
            color: var(--color-fg-secondary);
            padding: 10px 14px;
            border-radius: 12px;
            font-size: 12px;
            margin-top: 14px;
          "
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
    {/if}
  {/if}
</section>
