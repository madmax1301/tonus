<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { queueApi, ApiError, type QueueJob, type QueueResponse } from '$lib/api';
  import GlassCard from '$lib/components/GlassCard.svelte';
  import AlbumArt from '$lib/components/AlbumArt.svelte';
  import StatusPill from '$lib/components/StatusPill.svelte';
  import ProgressLine from '$lib/components/ProgressLine.svelte';
  import { RotateCw, Trash2, Eraser, X, Loader2 } from 'lucide-svelte';

  type Filter = 'all' | 'queued' | 'processing' | 'completed' | 'error';

  let activeFilter = $state<Filter>('all');
  let data = $state<QueueResponse | null>(null);
  let loadError = $state<string | null>(null);
  let loading = $state(false);
  let filterText = $state('');

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

  const filtered = $derived.by(() => {
    if (!data) return [] as QueueJob[];
    const q = filterText.trim().toLowerCase();
    if (!q) return data.items;
    return data.items.filter((j) => {
      const t = j.payload?.track ?? {};
      const blob = `${t.name ?? ''} ${t.artist ?? ''} ${t.album ?? ''}`.toLowerCase();
      return blob.includes(q);
    });
  });

  const counts = $derived(data?.status_counts ?? {});
  const total = $derived(data?.total ?? 0);
  const shown = $derived(data?.shown ?? 0);

  const filters: { id: Filter; label: string }[] = [
    { id: 'all', label: 'Alle' },
    { id: 'queued', label: 'Wartend' },
    { id: 'processing', label: 'Läuft' },
    { id: 'completed', label: 'Fertig' },
    { id: 'error', label: 'Fehler' }
  ];
</script>

<section class="space-y-6">
  <header class="flex items-end justify-between gap-6 flex-wrap">
    <div>
      <h1 class="text-4xl font-semibold tracking-tight" style="color: var(--color-fg-primary);">
        Warteschlange
      </h1>
      {#if data}
        <p class="text-[13px] mt-2 tabular-nums" style="color: var(--color-fg-secondary);">
          <span style="color: var(--color-fg-primary);">{total.toLocaleString('de-DE')}</span> Jobs
          {#if shown < total}
            <span style="color: var(--color-fg-tertiary);"> · zeige {shown.toLocaleString('de-DE')}</span>
          {/if}
          {#if counts.queued}
            · {counts.queued.toLocaleString('de-DE')} wartend
          {/if}
          {#if counts.processing}
            · {counts.processing.toLocaleString('de-DE')} läuft
          {/if}
          {#if counts.completed}
            · {counts.completed.toLocaleString('de-DE')} fertig
          {/if}
          {#if counts.error}
            · <span style="color: var(--color-status-error);"
              >{counts.error.toLocaleString('de-DE')} Fehler</span
            >
          {/if}
        </p>
      {/if}
    </div>
    <div class="flex items-center gap-1.5 text-[12px]" style="color: var(--color-fg-tertiary);">
      {#if loading}
        <Loader2 size={12} class="animate-spin" />
      {:else}
        <span class="inline-block w-1.5 h-1.5 rounded-full" style="background: var(--color-status-done);"></span>
      {/if}
      Live · alle 3 s
    </div>
  </header>

  <!-- Bulk-Actions (über der Liste) -->
  <div class="flex items-center gap-3 flex-wrap">
    <button
      onclick={retryAll}
      disabled={busy.retryAll || !counts.error}
      class="inline-flex items-center gap-2 px-4 py-2 rounded-md text-[13px] font-medium transition-opacity disabled:opacity-40"
      style="background: var(--color-accent); color: #1a1410;"
    >
      {#if busy.retryAll}
        <Loader2 size={14} class="animate-spin" />
      {:else}
        <RotateCw size={14} strokeWidth={1.8} />
      {/if}
      Alle Fehler retry
      {#if counts.error}
        <span class="text-[11px] opacity-70">·&nbsp;{counts.error.toLocaleString('de-DE')}</span>
      {/if}
    </button>

    <button
      onclick={cleanupCompleted}
      disabled={busy.cleanup || !counts.completed}
      class="inline-flex items-center gap-2 px-4 py-2 rounded-md text-[13px] transition-colors disabled:opacity-40"
      style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-secondary);"
    >
      {#if busy.cleanup}
        <Loader2 size={14} class="animate-spin" />
      {:else}
        <Eraser size={14} strokeWidth={1.5} />
      {/if}
      Fertige aufräumen
    </button>

    <button
      onclick={clearAll}
      disabled={busy.clearAll || total === 0}
      class="inline-flex items-center gap-2 px-4 py-2 rounded-md text-[13px] transition-colors disabled:opacity-40"
      style="background: transparent; border: 1px solid var(--color-status-error); color: var(--color-status-error);"
    >
      {#if busy.clearAll}
        <Loader2 size={14} class="animate-spin" />
      {:else}
        <Trash2 size={14} strokeWidth={1.5} />
      {/if}
      Queue leeren
    </button>

    {#if busy.feedback}
      <span class="ml-auto text-[12px]" style="color: var(--color-fg-secondary);">
        {busy.feedback}
      </span>
    {/if}
  </div>

  <!-- Filter + Suche -->
  <div class="flex items-center gap-3 flex-wrap">
    <div class="flex items-center gap-1">
      {#each filters as f}
        <button
          onclick={() => (activeFilter = f.id)}
          class="px-3 py-1.5 rounded-full text-[12px] transition-colors"
          style="background: {activeFilter === f.id
            ? 'var(--color-accent)'
            : 'transparent'}; color: {activeFilter === f.id
            ? '#1a1410'
            : 'var(--color-fg-secondary)'}; border: 1px solid {activeFilter === f.id
            ? 'transparent'
            : 'var(--color-border-soft)'};"
        >
          {f.label}
        </button>
      {/each}
    </div>
    <input
      type="text"
      bind:value={filterText}
      placeholder="In Queue filtern …"
      class="flex-1 min-w-[220px] px-3 py-2 rounded-md text-sm outline-none"
      style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-primary);"
    />
  </div>

  <!-- Queue-Liste -->
  {#if loadError}
    <div class="text-sm" style="color: var(--color-status-error);">{loadError}</div>
  {:else if filtered.length === 0}
    <GlassCard padding="lg">
      <div class="text-center text-sm" style="color: var(--color-fg-tertiary);">
        {filterText
          ? 'Keine Treffer für diesen Filter.'
          : activeFilter !== 'all'
            ? 'Keine Jobs in diesem Status.'
            : 'Queue ist leer.'}
      </div>
    </GlassCard>
  {:else}
    <div class="space-y-2">
      {#each filtered as job (job.job_id)}
        {@const t = job.payload?.track ?? {}}
        {@const isRunning = job.status === 'processing'}
        <div class="relative" class:skeleton-card={isRunning}>
          <GlassCard padding="sm">
            <div class="flex items-center gap-4">
              <AlbumArt src={t.album_art} alt={t.album ?? ''} size="sm" />
              <div class="flex-1 min-w-0 space-y-1.5">
                <div
                  class="font-medium text-[14px] truncate"
                  style="color: var(--color-fg-primary);"
                >
                  {t.name ?? job.job_id}
                </div>
                <div
                  class="text-[12px] truncate"
                  style="color: var(--color-fg-secondary);"
                >
                  {t.artist ?? ''}
                  {#if job.message}
                    <span style="color: var(--color-fg-tertiary);"> · {job.message}</span>
                  {/if}
                </div>
                {#if isRunning}
                  <ProgressLine
                    value={job.progress && job.progress > 0 ? job.progress : undefined}
                    pareto={!job.progress || job.progress === 0}
                  />
                {/if}
              </div>
              <StatusPill status={job.status} />
              <div class="flex items-center gap-1">
              {#if job.status === 'error'}
                <button
                  onclick={() => retryOne(job)}
                  aria-label="Retry"
                  class="p-1.5 rounded-md transition-colors"
                  style="color: var(--color-fg-secondary);"
                  onmouseenter={(e) =>
                    (e.currentTarget.style.background = 'var(--color-surface-3)')}
                  onmouseleave={(e) => (e.currentTarget.style.background = 'transparent')}
                >
                  <RotateCw size={14} strokeWidth={1.5} />
                </button>
              {/if}
              <button
                onclick={() => cancelOne(job)}
                aria-label="Entfernen"
                class="p-1.5 rounded-md transition-colors"
                style="color: var(--color-fg-secondary);"
                onmouseenter={(e) => {
                  e.currentTarget.style.background = 'var(--color-surface-3)';
                  e.currentTarget.style.color = 'var(--color-status-error)';
                }}
                onmouseleave={(e) => {
                  e.currentTarget.style.background = 'transparent';
                  e.currentTarget.style.color = 'var(--color-fg-secondary)';
                }}
              >
                <X size={14} strokeWidth={1.5} />
              </button>
              </div>
            </div>
          </GlassCard>
        </div>
      {/each}
    </div>
  {/if}

</section>
