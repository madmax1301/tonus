<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { base } from '$app/paths';
  import {
    queueApi,
    ApiError,
    type QueueJob,
    type QueueResponse,
    type LaneStatusResponse
  } from '$lib/api';
  import { tint, extractHue, DEFAULT_HUE } from '$lib/accent';
  import { t } from '$lib/i18n';
  import { showConfirm } from '$lib/confirm';
  import { get } from 'svelte/store';
  import CinemaBackdrop from '$lib/components/CinemaBackdrop.svelte';
  import CoverArt from '$lib/components/CoverArt.svelte';
  import VinylWithCover from '$lib/components/VinylWithCover.svelte';
  import EmptyState from '$lib/components/EmptyState.svelte';
  import ProgressLine from '$lib/components/ProgressLine.svelte';
  import {
    RotateCw,
    Trash2,
    Eraser,
    X,
    Loader2,
    Search,
    Disc,
    Link2,
    Puzzle,
    HardDrive,
    Music,
    ListMusic
  } from 'lucide-svelte';

  type OriginInfo = { icon: typeof Search; label: string; detail?: string };
  type DestInfo = { icon: typeof HardDrive; label: string; detail?: string };

  function jobOrigin(j: QueueJob): OriginInfo {
    const tt = get(t);
    const p = j.payload ?? {};
    if (p.plugin_sync_navidrome_user) {
      return {
        icon: Puzzle,
        label: tt('queue.origin.plugin'),
        detail: p.plugin_sync_navidrome_user
      };
    }
    if (p.kind === 'url') {
      return { icon: Link2, label: tt('queue.origin.url') };
    }
    if (p.album_id || p.album_name) {
      return { icon: Disc, label: tt('queue.origin.album'), detail: p.album_name };
    }
    return { icon: Search, label: tt('queue.origin.search') };
  }

  function jobDest(j: QueueJob): DestInfo {
    const tt = get(t);
    const p = j.payload ?? {};
    if (p.plugin_sync_playlist_name) {
      return {
        icon: ListMusic,
        label: tt('queue.dest.playlist'),
        detail: p.plugin_sync_playlist_name
      };
    }
    if (p.location === 'navidrome') {
      const lib = p.navidrome_library_path;
      if (lib) {
        const last = lib.split('/').filter(Boolean).pop() ?? lib;
        return { icon: Music, label: tt('queue.dest.navidrome'), detail: last };
      }
      return { icon: Music, label: tt('queue.dest.navidrome') };
    }
    return { icon: HardDrive, label: tt('queue.dest.local') };
  }

  type Filter = 'all' | 'queued' | 'processing' | 'completed' | 'error';

  let activeFilter = $state<Filter>('all');
  let data = $state<QueueResponse | null>(null);
  let lanes = $state<LaneStatusResponse | null>(null);
  let loadError = $state<string | null>(null);
  /** Spinner zeigen wir nur beim **ersten** Load + bei Filter-Wechsel.
   *  Während des Pollings nicht — sonst flackert die Page alle 3 s. */
  let initialLoading = $state(true);
  let filterText = $state('');
  let featuredHue: number = $state(DEFAULT_HUE);
  /** "Tick" im 1 s-Takt damit der Lane-Countdown live runterzählt ohne
   *  einen Fetch pro Sekunde zu brauchen. */
  let nowMs = $state<number>(Date.now());

  let busy = $state<{
    retryAll: boolean;
    cleanup: boolean;
    clearAll: boolean;
    feedback?: string;
  }>({ retryAll: false, cleanup: false, clearAll: false });

  const POLL_MS = 3000;
  const TICK_MS = 1000;
  let timer: ReturnType<typeof setInterval> | null = null;
  let tickTimer: ReturnType<typeof setInterval> | null = null;

  async function fetchQueue(showSpinner = false) {
    if (showSpinner) initialLoading = true;
    try {
      const status = activeFilter === 'all' ? undefined : activeFilter;
      const [q, l] = await Promise.all([
        queueApi.list(status),
        queueApi.lanes().catch(() => null) // optional — älteres backend hat den endpoint nicht
      ]);
      data = q;
      if (l) lanes = l;
      loadError = null;
    } catch (err) {
      if (!(err instanceof ApiError && (err.status === 401 || err.status === 403))) {
        loadError = err instanceof Error ? err.message : 'Queue konnte nicht geladen werden';
      }
    } finally {
      initialLoading = false;
    }
  }

  function startPolling() {
    if (timer) clearInterval(timer);
    timer = setInterval(() => fetchQueue(false), POLL_MS);
  }

  function stopPolling() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    if (tickTimer) {
      clearInterval(tickTimer);
      tickTimer = null;
    }
  }

  onMount(async () => {
    await fetchQueue(true);
    startPolling();
    tickTimer = setInterval(() => (nowMs = Date.now()), TICK_MS);
  });

  onDestroy(stopPolling);

  $effect(() => {
    activeFilter; // dependency
    fetchQueue(true);
  });

  /** Featured-Job-Pick:
   *   1) Wenn ≥1 processing → der mit dem **niedrigsten** Progress (gerade
   *      gestartet → Vinyl spinnt für die ganze Verarbeitung sichtbar).
   *   2) Sonst wenn ≥1 queued → der nächste queued Job (für Lane-Countdown).
   *   3) Sonst null (Featured-Card ausgeblendet).
   */
  const featuredJob = $derived.by<QueueJob | null>(() => {
    if (!data?.items) return null;
    const processing = data.items.filter((j) => j.status === 'processing');
    if (processing.length > 0) {
      return processing.reduce((best, j) =>
        (j.progress ?? 0) < (best.progress ?? 0) ? j : best
      );
    }
    const queued = data.items.filter((j) => j.status === 'queued');
    return queued[0] ?? null;
  });

  $effect(() => {
    const art = featuredJob?.payload?.track?.album_art;
    if (!art) {
      featuredHue = DEFAULT_HUE;
      return;
    }
    let cancelled = false;
    extractHue(art).then((h) => {
      if (!cancelled) featuredHue = h;
    });
    return () => {
      cancelled = true;
    };
  });

  async function retryAll() {
    busy = { ...busy, retryAll: true };
    try {
      const r = await queueApi.retryAll();
      busy = { ...busy, retryAll: false, feedback: `${r.retried} Fehler retried` };
      await fetchQueue();
    } catch {
      busy = { ...busy, retryAll: false, feedback: 'Retry fehlgeschlagen' };
    }
    setTimeout(() => (busy = { ...busy, feedback: undefined }), 2500);
  }

  async function cleanupCompleted() {
    busy = { ...busy, cleanup: true };
    try {
      const r = await queueApi.clear('completed');
      busy = { ...busy, cleanup: false, feedback: `${r.deleted} fertige entfernt` };
      await fetchQueue();
    } catch {
      busy = { ...busy, cleanup: false, feedback: 'Aufräumen fehlgeschlagen' };
    }
    setTimeout(() => (busy = { ...busy, feedback: undefined }), 2500);
  }

  async function clearAll() {
    const ok = await showConfirm({
      title: 'Queue leeren',
      message:
        'Wirklich die komplette Queue leeren? Laufende Downloads bleiben erhalten — nur queued + completed + error werden entfernt.',
      confirmLabel: 'Leeren',
      destructive: true
    });
    if (!ok) return;
    busy = { ...busy, clearAll: true };
    try {
      const r = await queueApi.clear('all');
      busy = { ...busy, clearAll: false, feedback: `${r.deleted} entfernt` };
      await fetchQueue();
    } catch {
      busy = { ...busy, clearAll: false, feedback: 'Leeren fehlgeschlagen' };
    }
    setTimeout(() => (busy = { ...busy, feedback: undefined }), 2500);
  }

  async function cancelOne(job: QueueJob) {
    try {
      await queueApi.cancel(job.job_id);
      await fetchQueue();
    } catch {
      /* noop */
    }
  }

  async function retryOne(job: QueueJob) {
    try {
      await queueApi.retry(job.job_id);
      await fetchQueue();
    } catch {
      /* noop */
    }
  }

  // Liste ohne den featured-Job (der landet oben als Hero-Card)
  const filtered = $derived.by<QueueJob[]>(() => {
    if (!data) return [];
    const q = filterText.trim().toLowerCase();
    let items = data.items;
    if (featuredJob) items = items.filter((j) => j.job_id !== featuredJob.job_id);
    if (q) {
      items = items.filter((j) => {
        const t = j.payload?.track ?? {};
        const blob = `${t.name ?? ''} ${t.artist ?? ''} ${t.album ?? ''}`.toLowerCase();
        return blob.includes(q);
      });
    }
    return items;
  });

  const counts = $derived(data?.status_counts ?? {});
  const total = $derived(data?.total ?? 0);
  const shown = $derived(data?.shown ?? 0);
  const accent = $derived(tint(featuredHue));
  const accentSoft = $derived(tint(featuredHue, 0.5));
  const processingCount = $derived(counts.processing ?? 0);

  /**
   * Live-Countdown: lanes.next_ready_in_ms wird zum Polling-Zeitpunkt geliefert,
   * wir rechnen pro Sekunde lokal runter (nowMs ticked alle 1 s).
   * `lanesAnchor` = Wand-Zeit als die Lanes-Antwort kam.
   */
  let lanesAnchor = $state<number>(Date.now());
  $effect(() => {
    if (lanes) lanesAnchor = Date.now();
  });

  type LaneLive = {
    name: string;
    remaining_ms: number;
  };
  const liveLanes = $derived.by<LaneLive[]>(() => {
    if (!lanes) return [];
    const elapsed = nowMs - lanesAnchor;
    return lanes.lanes.map((l) => ({
      name: l.name,
      remaining_ms: Math.max(0, l.remaining_ms - elapsed)
    }));
  });
  const nextLaneReadyMs = $derived(
    liveLanes.length === 0 ? 0 : Math.min(...liveLanes.map((l) => l.remaining_ms))
  );

  function fmtMs(ms: number): string {
    const totalSec = Math.max(0, Math.round(ms / 1000));
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  type FilterDef = {
    id: Filter;
    label: string;
    count: number;
    color: string;
  };

  const filterDefs = $derived.by<FilterDef[]>(() => {
    const tt = $t;
    return [
      { id: 'all', label: tt('queue.filter.all'), count: total, color: accent },
      { id: 'processing', label: tt('queue.filter.processing'), count: counts.processing ?? 0, color: accent },
      { id: 'queued', label: tt('queue.filter.queued'), count: counts.queued ?? 0, color: 'var(--color-fg-tertiary)' },
      { id: 'completed', label: tt('queue.filter.completed'), count: counts.completed ?? 0, color: 'var(--color-status-done)' },
      { id: 'error', label: tt('queue.filter.error'), count: counts.error ?? 0, color: 'var(--color-status-error)' }
    ];
  });

  function colorByStatus(status: QueueJob['status']): string {
    if (status === 'error') return 'var(--color-status-error)';
    if (status === 'completed') return 'var(--color-status-done)';
    if (status === 'queued') return 'var(--color-fg-tertiary)';
    return accent;
  }
</script>

<CinemaBackdrop hue={featuredHue} intensity={0.7} />

<section class="relative z-10 mx-auto max-w-[1180px] w-full" style="padding: 40px 36px 50px;">
  <!-- Hero header -->
  <div class="flex items-end justify-between flex-wrap gap-4" style="margin-bottom: 28px;">
    <div>
      <div
        class="font-semibold uppercase inline-flex items-center gap-2"
        style="
          font-size: 11px;
          letter-spacing: 0.24em;
          color: {processingCount > 0 ? accent : 'var(--color-fg-secondary)'};
          margin-bottom: 10px;
        "
      >
        {#if initialLoading}
          <Loader2 size={11} strokeWidth={2} class="animate-spin" />
        {:else}
          <span
            class="inline-block rounded-full"
            style="
              width: 6px;
              height: 6px;
              background: {processingCount > 0 ? accent : 'var(--color-status-done)'};
              animation: {processingCount > 0 ? 'tonus-pulse-soft 1.4s ease-in-out infinite' : 'none'};
            "
          ></span>
        {/if}
        {#if processingCount > 0}
          {$t('queue.eyebrow.live')}
        {:else if nextLaneReadyMs >= 1000}
          {$t('queue.eyebrow.waiting')}
          <span style="color: var(--color-fg-tertiary); letter-spacing: 0.04em; font-family: var(--font-mono); margin-left: 6px;">
            · {$t('queue.featured.lane_in', { time: fmtMs(nextLaneReadyMs) })}
          </span>
        {:else}
          {$t('queue.eyebrow.ready')}
        {/if}
      </div>
      <h1
        class="font-semibold m-0"
        style="
          font-family: var(--font-display);
          font-size: 48px;
          letter-spacing: -0.035em;
          line-height: 1;
        "
      >
        {$t('queue.title')}
      </h1>
      {#if data}
        <div
          class="mt-2 tabular-nums"
          style="font-size: 12px; color: var(--color-fg-secondary); font-family: var(--font-mono); letter-spacing: 0.02em;"
        >
          {$t('queue.total_jobs', { count: total.toLocaleString('de-DE') })}
          {#if shown < total}
            · {$t('queue.shown', { count: shown.toLocaleString('de-DE') })}
          {/if}
        </div>
      {/if}
    </div>

    <!-- Bulk-Actions -->
    <div class="flex items-center gap-2 flex-wrap">
      <button
        onclick={retryAll}
        disabled={busy.retryAll || !counts.error}
        class="inline-flex items-center gap-2 transition-opacity disabled:opacity-40"
        style="
          font-size: 12px;
          padding: 9px 16px;
          background: rgba(255, 255, 255, 0.06);
          color: var(--color-fg-primary);
          border: 1px solid var(--color-border-soft);
          border-radius: 999px;
          backdrop-filter: blur(20px);
          -webkit-backdrop-filter: blur(20px);
        "
      >
        {#if busy.retryAll}
          <Loader2 size={12} strokeWidth={2} class="animate-spin" />
        {:else}
          <RotateCw size={12} strokeWidth={1.8} />
        {/if}
        {$t('queue.bulk.retry_errors')}
        {#if counts.error}
          <span style="color: var(--color-status-error); font-weight: 600;">
            {counts.error}
          </span>
        {/if}
      </button>

      <button
        onclick={cleanupCompleted}
        disabled={busy.cleanup || !counts.completed}
        class="inline-flex items-center gap-2 transition-opacity disabled:opacity-40"
        style="
          font-size: 12px;
          padding: 9px 16px;
          background: rgba(255, 255, 255, 0.06);
          color: var(--color-fg-primary);
          border: 1px solid var(--color-border-soft);
          border-radius: 999px;
          backdrop-filter: blur(20px);
          -webkit-backdrop-filter: blur(20px);
        "
      >
        {#if busy.cleanup}
          <Loader2 size={12} strokeWidth={2} class="animate-spin" />
        {:else}
          <Eraser size={12} strokeWidth={1.5} />
        {/if}
        {$t('queue.bulk.cleanup')}
      </button>

      <button
        onclick={clearAll}
        disabled={busy.clearAll || total === 0}
        class="inline-flex items-center gap-2 transition-colors disabled:opacity-40"
        style="
          font-size: 12px;
          padding: 9px 16px;
          background: transparent;
          color: var(--color-status-error);
          border: 1px solid color-mix(in oklab, var(--color-status-error) 50%, transparent);
          border-radius: 999px;
        "
      >
        {#if busy.clearAll}
          <Loader2 size={12} strokeWidth={2} class="animate-spin" />
        {:else}
          <Trash2 size={12} strokeWidth={1.5} />
        {/if}
        {$t('queue.bulk.clear_all')}
      </button>

      {#if busy.feedback}
        <span class="text-[12px]" style="color: var(--color-fg-secondary);">
          {busy.feedback}
        </span>
      {/if}
    </div>
  </div>

  <!-- Filter pills -->
  <div class="flex flex-wrap gap-1.5" style="margin-bottom: 18px;">
    {#each filterDefs as f}
      {@const on = activeFilter === f.id}
      <button
        onclick={() => (activeFilter = f.id)}
        class="inline-flex items-center gap-2 transition-colors"
        style="
          padding: 6px 14px;
          border-radius: 999px;
          font-size: 11.5px;
          font-weight: {on ? 600 : 500};
          background: {on ? `color-mix(in oklab, ${f.color} 13%, transparent)` : 'rgba(255,255,255,0.04)'};
          color: {on ? f.color : 'var(--color-fg-secondary)'};
          border: 1px solid {on ? `color-mix(in oklab, ${f.color} 33%, transparent)` : 'var(--color-border-soft)'};
          backdrop-filter: blur(20px);
          -webkit-backdrop-filter: blur(20px);
        "
      >
        {f.label}
        <span
          class="tabular-nums"
          style="
            font-size: 10.5px;
            color: {on ? f.color : 'var(--color-fg-tertiary)'};
            font-weight: 600;
            opacity: {on ? 1 : 0.7};
          "
        >
          {f.count.toLocaleString('de-DE')}
        </span>
      </button>
    {/each}

    <input
      type="text"
      bind:value={filterText}
      placeholder="filtern …"
      class="ml-auto outline-none"
      style="
        font-size: 12px;
        padding: 6px 14px;
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.04);
        border: 1px solid var(--color-border-soft);
        color: var(--color-fg-primary);
        min-width: 200px;
      "
    />
  </div>

  <!-- Featured job card -->
  {#if featuredJob}
    {@const t = featuredJob.payload?.track ?? {}}
    {@const origin = jobOrigin(featuredJob)}
    {@const dest = jobDest(featuredJob)}
    {@const isFeaturedQueued = featuredJob.status === 'queued'}
    {@const isFeaturedProcessing = featuredJob.status === 'processing'}
    <div
      class="flex items-center gap-6 mb-6"
      style="
        background: linear-gradient(135deg, {tint(featuredHue, 0.18)}, rgba(15, 15, 18, 0.7));
        backdrop-filter: blur(40px) saturate(1.2);
        -webkit-backdrop-filter: blur(40px) saturate(1.2);
        border: 1px solid {tint(featuredHue, 0.3)};
        border-radius: 18px;
        padding: 22px;
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.5);
      "
    >
      <VinylWithCover
        src={t.album_art}
        alt={t.album}
        artist={t.artist ?? ''}
        size={92}
        spinning={isFeaturedProcessing}
      />
      <div class="flex-1 min-w-0">
        <div
          class="font-semibold uppercase"
          style="
            font-size: 11px;
            letter-spacing: 0.18em;
            color: {isFeaturedQueued ? 'var(--color-fg-tertiary)' : accent};
            margin-bottom: 4px;
          "
        >
          {#if isFeaturedQueued}
            Wartet auf Lane{nextLaneReadyMs >= 1000 ? ` · ${fmtMs(nextLaneReadyMs)}` : ' · Slot frei'}
          {:else}
            {featuredJob.stage ?? 'Wird verarbeitet'}
          {/if}
        </div>
        <div
          class="font-semibold truncate"
          style="
            font-family: var(--font-display);
            font-size: 22px;
            letter-spacing: -0.025em;
            line-height: 1.1;
          "
          title={t.name ?? ''}
        >
          {t.name ?? featuredJob.job_id}
        </div>
        <div
          class="truncate"
          style="font-size: 13px; color: var(--color-fg-secondary); margin-top: 2px;"
        >
          {t.artist ?? ''}
        </div>

        <!-- Origin → Dest -->
        <div class="mt-2.5 flex items-center gap-2.5 flex-wrap" style="font-size: 11px;">
          <span
            class="inline-flex items-center gap-1.5"
            style="
              padding: 3px 10px;
              border-radius: 999px;
              background: rgba(255, 255, 255, 0.06);
              border: 1px solid var(--color-border-soft);
              color: var(--color-fg-secondary);
            "
            title="Quelle"
          >
            <svelte:component this={origin.icon} size={11} strokeWidth={1.6} />
            {origin.label}
            {#if origin.detail}
              <span style="color: var(--color-fg-tertiary); font-family: var(--font-mono);">· {origin.detail}</span>
            {/if}
          </span>
          <span style="color: var(--color-fg-tertiary); font-size: 10px;">→</span>
          <span
            class="inline-flex items-center gap-1.5"
            style="
              padding: 3px 10px;
              border-radius: 999px;
              background: color-mix(in oklab, {accent} 10%, transparent);
              border: 1px solid color-mix(in oklab, {accent} 33%, transparent);
              color: {accent};
            "
            title="Ziel"
          >
            <svelte:component this={dest.icon} size={11} strokeWidth={1.6} />
            {dest.label}
            {#if dest.detail}
              <span style="color: var(--color-fg-tertiary); font-family: var(--font-mono);">· {dest.detail}</span>
            {/if}
          </span>
        </div>

        <!-- Pareto bar 5px featured. Smooth Animation 0→95% via CSS-keyframe;
             snap auf 100% bei done. Kein visueller Sprung mehr von 30%→100%. -->
        <div class="mt-3 flex items-center gap-3">
          <div class="flex-1">
            <ProgressLine
              pareto={isFeaturedProcessing}
              done={featuredJob.status === 'completed'}
              color={accent}
              height={5}
              glow
            />
          </div>
          {#if isFeaturedQueued}
            <span
              class="tabular-nums"
              style="font-size: 13px; color: var(--color-fg-tertiary); font-weight: 500; min-width: 60px; text-align: right; font-family: var(--font-mono);"
            >
              {nextLaneReadyMs >= 1000 ? fmtMs(nextLaneReadyMs) : 'frei'}
            </span>
          {:else}
            <span
              class="tabular-nums"
              style="font-size: 13px; color: {accent}; font-weight: 600; min-width: 60px; text-align: right;"
            >
              {featuredJob.status === 'completed' ? '100%' : 'läuft'}
            </span>
          {/if}
        </div>
      </div>
    </div>
  {/if}

  <!-- Job list -->
  {#if loadError}
    <div class="text-sm" style="color: var(--color-status-error);">{loadError}</div>
  {:else if total === 0 && !initialLoading}
    <!-- Echter Empty-State: gar keine Jobs in der DB. Cinematic-Glyph
         + Editorial-Copy + Action-Row zur Library. -->
    <EmptyState
      glyph="queue"
      eyebrow={$t('empty.queue.eyebrow')}
      title={$t('empty.queue.title')}
      body={$t('empty.queue.body')}
    >
      {#snippet actions()}
        <a
          href="{base}/"
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
            text-decoration: none;
            box-shadow: 0 8px 24px {accent}40;
          "
        >
          {$t('empty.queue.cta_search')}
        </a>
      {/snippet}
    </EmptyState>
  {:else if filtered.length === 0 && !featuredJob}
    <!-- Filter-empty (es gibt Jobs, nur nicht im aktiven Filter). Kompakter
         Hinweis statt full-page EmptyState — der User soll den Filter
         wechseln können, nicht zur Library wandern. -->
    <div
      class="text-center"
      style="
        padding: 60px 24px;
        background: rgba(15, 15, 18, 0.4);
        backdrop-filter: blur(30px);
        -webkit-backdrop-filter: blur(30px);
        border: 1px solid var(--color-border-soft);
        border-radius: 14px;
        font-size: 13px;
        color: var(--color-fg-tertiary);
      "
    >
      {filterText
        ? 'Keine Treffer für diesen Filter.'
        : activeFilter !== 'all'
          ? 'Keine Jobs in diesem Status.'
          : 'Queue ist leer.'}
    </div>
  {:else}
    <div class="flex flex-col" style="gap: 6px;">
      {#each filtered as job (job.job_id)}
        {@const t = job.payload?.track ?? {}}
        {@const isRunning = job.status === 'processing'}
        {@const origin = jobOrigin(job)}
        {@const dest = jobDest(job)}
        {@const statusColor = colorByStatus(job.status)}
        <div
          class="flex items-center gap-4"
          style="
            background: rgba(15, 15, 18, 0.45);
            backdrop-filter: blur(30px);
            -webkit-backdrop-filter: blur(30px);
            border: 1px solid var(--color-border-soft);
            border-radius: 12px;
            padding: 14px 18px;
            contain: layout paint;
          "
        >
          <div style="width: 48px; height: 48px;">
            <CoverArt src={t.album_art} alt={t.album ?? ''} artist={t.artist ?? ''} fluid radius={6} />
          </div>

          <div class="flex-1 min-w-0">
            <div class="flex items-baseline gap-2.5 min-w-0">
              <div
                class="truncate"
                style="font-size: 14px; font-weight: 500; letter-spacing: -0.005em;"
                title={t.name ?? ''}
              >
                {t.name ?? job.job_id}
              </div>
              {#if t.artist}
                <div
                  class="truncate whitespace-nowrap"
                  style="font-size: 11.5px; color: var(--color-fg-secondary);"
                >
                  · {t.artist}
                </div>
              {/if}
            </div>

            <!-- Origin → Dest + Stage -->
            <div
              class="mt-1.5 flex items-center gap-2 flex-wrap"
              style="font-size: 10.5px; color: var(--color-fg-secondary);"
            >
              <span
                class="inline-flex items-center gap-1"
                style="
                  padding: 2px 8px;
                  border-radius: 999px;
                  background: rgba(255, 255, 255, 0.05);
                  border: 1px solid var(--color-border-soft);
                "
              >
                <svelte:component this={origin.icon} size={10} strokeWidth={1.6} />
                {origin.label}
                {#if origin.detail}
                  <span style="color: var(--color-fg-tertiary); font-family: var(--font-mono);">· {origin.detail}</span>
                {/if}
              </span>
              <span style="color: var(--color-fg-tertiary); font-size: 9px;">→</span>
              <span
                class="inline-flex items-center gap-1"
                style="
                  padding: 2px 8px;
                  border-radius: 999px;
                  background: rgba(255, 255, 255, 0.05);
                  border: 1px solid var(--color-border-soft);
                "
              >
                <svelte:component this={dest.icon} size={10} strokeWidth={1.6} />
                {dest.label}
                {#if dest.detail}
                  <span style="color: var(--color-fg-tertiary); font-family: var(--font-mono);">· {dest.detail}</span>
                {/if}
              </span>
              {#if job.stage}
                <span style="color: var(--color-fg-tertiary);">· {job.stage}</span>
              {/if}
            </div>
          </div>

          {#if isRunning}
            <div class="flex flex-col gap-1.5" style="width: 200px;">
              <ProgressLine pareto color={accent} height={5} />
              <div
                class="text-right"
                style="font-size: 10.5px; color: var(--color-fg-tertiary); font-family: var(--font-mono);"
              >
                läuft
              </div>
            </div>
          {/if}

          <span
            class="uppercase tabular-nums text-center"
            style="
              font-size: 10.5px;
              padding: 4px 12px;
              border-radius: 999px;
              font-weight: 600;
              letter-spacing: 0.06em;
              background: color-mix(in oklab, {statusColor} 10%, transparent);
              color: {statusColor};
              border: 1px solid color-mix(in oklab, {statusColor} 25%, transparent);
              min-width: 90px;
            "
          >
            {job.status}
          </span>

          <div class="flex items-center gap-1">
            {#if job.status === 'error'}
              <button
                onclick={() => retryOne(job)}
                aria-label="Retry"
                class="p-1.5 rounded-md transition-colors"
                style="color: var(--color-fg-secondary);"
                onmouseenter={(e) =>
                  (e.currentTarget.style.background = 'rgba(255,255,255,0.08)')}
                onmouseleave={(e) => (e.currentTarget.style.background = 'transparent')}
              >
                <RotateCw size={13} strokeWidth={1.5} />
              </button>
            {/if}
            <button
              onclick={() => cancelOne(job)}
              aria-label="Entfernen"
              class="p-1.5 rounded-md transition-colors"
              style="color: var(--color-fg-secondary);"
              onmouseenter={(e) => {
                e.currentTarget.style.background = 'rgba(255,255,255,0.08)';
                e.currentTarget.style.color = 'var(--color-status-error)';
              }}
              onmouseleave={(e) => {
                e.currentTarget.style.background = 'transparent';
                e.currentTarget.style.color = 'var(--color-fg-secondary)';
              }}
            >
              <X size={13} strokeWidth={1.5} />
            </button>
          </div>
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
