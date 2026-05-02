<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { queueApi, ApiError, type QueueJob, type QueueResponse } from '$lib/api';
  import { tint, extractHue, DEFAULT_HUE } from '$lib/accent';
  import CinemaBackdrop from '$lib/components/CinemaBackdrop.svelte';
  import CoverArt from '$lib/components/CoverArt.svelte';
  import VinylWithCover from '$lib/components/VinylWithCover.svelte';
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
    const p = j.payload ?? {};
    if (p.plugin_sync_navidrome_user) {
      return { icon: Puzzle, label: 'Plugin', detail: p.plugin_sync_navidrome_user };
    }
    if (p.kind === 'url') {
      return { icon: Link2, label: 'URL' };
    }
    if (p.album_id || p.album_name) {
      return { icon: Disc, label: 'Album', detail: p.album_name };
    }
    return { icon: Search, label: 'Suche' };
  }

  function jobDest(j: QueueJob): DestInfo {
    const p = j.payload ?? {};
    if (p.plugin_sync_playlist_name) {
      return { icon: ListMusic, label: 'Playlist', detail: p.plugin_sync_playlist_name };
    }
    if (p.location === 'navidrome') {
      const lib = p.navidrome_library_path;
      if (lib) {
        const last = lib.split('/').filter(Boolean).pop() ?? lib;
        return { icon: Music, label: 'Navidrome', detail: last };
      }
      return { icon: Music, label: 'Navidrome' };
    }
    return { icon: HardDrive, label: 'Local' };
  }

  type Filter = 'all' | 'queued' | 'processing' | 'completed' | 'error';

  let activeFilter = $state<Filter>('all');
  let data = $state<QueueResponse | null>(null);
  let loadError = $state<string | null>(null);
  let loading = $state(false);
  let filterText = $state('');
  let featuredHue: number = $state(DEFAULT_HUE);

  let busy = $state<{
    retryAll: boolean;
    cleanup: boolean;
    clearAll: boolean;
    feedback?: string;
  }>({ retryAll: false, cleanup: false, clearAll: false });

  const POLL_MS = 3000;
  let timer: ReturnType<typeof setInterval> | null = null;

  async function fetchQueue() {
    loading = true;
    try {
      const status = activeFilter === 'all' ? undefined : activeFilter;
      data = await queueApi.list(status);
      loadError = null;
    } catch (err) {
      if (!(err instanceof ApiError && (err.status === 401 || err.status === 403))) {
        loadError = err instanceof Error ? err.message : 'Queue konnte nicht geladen werden';
      }
    } finally {
      loading = false;
    }
  }

  function startPolling() {
    if (timer) clearInterval(timer);
    timer = setInterval(fetchQueue, POLL_MS);
  }

  function stopPolling() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  onMount(async () => {
    await fetchQueue();
    startPolling();
  });

  onDestroy(stopPolling);

  $effect(() => {
    activeFilter; // dependency
    fetchQueue();
  });

  // Featured-Job = der processing-Job mit höchstem progress.
  // Treibt Cinema-Backdrop-Hue + Hero-Card.
  const featuredJob = $derived.by<QueueJob | null>(() => {
    if (!data?.items) return null;
    const processing = data.items.filter((j) => j.status === 'processing');
    if (processing.length === 0) return null;
    return processing.reduce((best, j) =>
      (j.progress ?? 0) > (best.progress ?? 0) ? j : best
    );
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
    if (!confirm('Wirklich die komplette Queue leeren? Laufende Downloads bleiben erhalten.'))
      return;
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

  type FilterDef = {
    id: Filter;
    label: string;
    count: number;
    color: string;
  };

  const filterDefs = $derived.by<FilterDef[]>(() => {
    return [
      { id: 'all', label: 'Alle', count: total, color: accent },
      { id: 'processing', label: 'Aktiv', count: counts.processing ?? 0, color: accent },
      { id: 'queued', label: 'Wartend', count: counts.queued ?? 0, color: 'var(--color-fg-tertiary)' },
      { id: 'completed', label: 'Fertig', count: counts.completed ?? 0, color: 'var(--color-status-done)' },
      { id: 'error', label: 'Fehler', count: counts.error ?? 0, color: 'var(--color-status-error)' }
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
        {#if loading}
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
        Live · {processingCount} in Bewegung
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
        Warteschlange
      </h1>
      {#if data}
        <div
          class="mt-2 tabular-nums"
          style="font-size: 12px; color: var(--color-fg-secondary); font-family: var(--font-mono); letter-spacing: 0.02em;"
        >
          {total.toLocaleString('de-DE')} Jobs gesamt
          {#if shown < total}
            · {shown.toLocaleString('de-DE')} sichtbar
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
        Fehler retry
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
        Aufräumen
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
        Leeren
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
        spinning
      />
      <div class="flex-1 min-w-0">
        <div
          class="font-semibold uppercase"
          style="
            font-size: 11px;
            letter-spacing: 0.18em;
            color: {accent};
            margin-bottom: 4px;
          "
        >
          {featuredJob.stage ?? 'Wird verarbeitet'}
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

        <!-- Pareto bar 5px featured + percentage -->
        <div class="mt-3 flex items-center gap-3">
          <div
            class="relative flex-1 overflow-hidden"
            style="height: 5px; border-radius: 5px; background: rgba(255, 255, 255, 0.06);"
          >
            <div
              style="
                position: absolute;
                inset: 0;
                width: {Math.max(2, featuredJob.progress ?? 0)}%;
                background: {accent};
                border-radius: 5px;
                transition: width 0.4s cubic-bezier(0.2, 0.7, 0.3, 1);
                box-shadow: 0 0 12px {accentSoft};
              "
            >
              <div
                style="
                  position: absolute;
                  inset: 0;
                  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.4), transparent);
                  transform: translateX(-100%);
                  animation: tonus-progress-flow 1.6s linear infinite;
                  width: 40%;
                "
              ></div>
            </div>
          </div>
          <span
            class="tabular-nums"
            style="font-size: 13px; color: {accent}; font-weight: 600; min-width: 40px; text-align: right;"
          >
            {featuredJob.progress ?? 0}%
          </span>
        </div>
      </div>
    </div>
  {/if}

  <!-- Job list -->
  {#if loadError}
    <div class="text-sm" style="color: var(--color-status-error);">{loadError}</div>
  {:else if filtered.length === 0 && !featuredJob}
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
              <div
                class="relative overflow-hidden"
                style="height: 5px; border-radius: 5px; background: rgba(255, 255, 255, 0.06);"
              >
                <div
                  style="
                    position: absolute;
                    inset: 0;
                    width: {Math.max(2, job.progress ?? 0)}%;
                    background: {accent};
                    border-radius: 5px;
                    transition: width 0.4s cubic-bezier(0.2, 0.7, 0.3, 1);
                  "
                >
                  <div
                    style="
                      position: absolute;
                      inset: 0;
                      background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.4), transparent);
                      transform: translateX(-100%);
                      animation: tonus-progress-flow 1.6s linear infinite;
                      width: 40%;
                    "
                  ></div>
                </div>
              </div>
              <div
                class="tabular-nums text-right"
                style="font-size: 10.5px; color: var(--color-fg-tertiary); font-family: var(--font-mono);"
              >
                {job.progress ?? 0}%
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
