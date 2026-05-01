<script lang="ts">
  import { apiToken } from '$lib/auth';

  let value = $state($apiToken);

  function save() {
    apiToken.set(value.trim());
  }
</script>

<section class="space-y-6">
  <header>
    <h1 class="text-4xl font-semibold tracking-tight" style="color: var(--color-fg-primary);">
      Einstellungen
    </h1>
    <p class="text-sm" style="color: var(--color-fg-secondary);">
      Token, Provider, Audio-Format. Mehr kommt in Phase B/C.
    </p>
  </header>

  <div
    class="rounded-[var(--radius-lg)] p-6 backdrop-blur-[var(--blur-card)] space-y-4"
    style="background: var(--color-surface-1); border: 1px solid var(--color-border-soft);"
  >
    <label class="block space-y-2">
      <span class="text-[13px] font-medium" style="color: var(--color-fg-secondary);">
        API-Token
      </span>
      <input
        type="password"
        bind:value
        class="w-full px-3 py-2 rounded-md text-sm font-mono"
        style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-primary);"
        placeholder="Bearer-Token aus dem Backend"
      />
    </label>
    <button
      onclick={save}
      class="px-4 py-2 rounded-md text-sm font-medium transition-colors"
      style="background: var(--color-accent); color: #1a1410;"
    >
      Speichern
    </button>
  </div>
</section>
