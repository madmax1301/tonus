<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';

  let healthStatus: string | null = null;
  let healthError: string | null = null;

  onMount(async () => {
    try {
      const r = await api.health();
      healthStatus = r.status ?? 'unknown';
    } catch (err) {
      healthError = err instanceof Error ? err.message : String(err);
    }
  });
</script>

<section class="space-y-6">
  <header class="space-y-2">
    <h1 class="text-4xl font-semibold tracking-tight" style="color: var(--color-fg-primary);">
      Bibliothek
    </h1>
    <p class="text-sm" style="color: var(--color-fg-secondary);">
      Suche, queue und importiere Musik in deine Sammlung.
    </p>
  </header>

  <div
    class="rounded-[var(--radius-lg)] p-8 backdrop-blur-[var(--blur-card)]"
    style="background: var(--color-surface-1); border: 1px solid var(--color-border-soft);"
  >
    <div class="flex items-center justify-between gap-4 text-sm">
      <span style="color: var(--color-fg-secondary);">Backend</span>
      {#if healthStatus}
        <span
          class="inline-flex items-center gap-2 font-medium"
          style="color: var(--color-status-done);"
        >
          <span
            class="inline-block w-1.5 h-1.5 rounded-full"
            style="background: var(--color-status-done);"
          ></span>
          {healthStatus}
        </span>
      {:else if healthError}
        <span style="color: var(--color-status-error);">{healthError}</span>
      {:else}
        <span style="color: var(--color-fg-tertiary);">prüfe …</span>
      {/if}
    </div>
  </div>
</section>
