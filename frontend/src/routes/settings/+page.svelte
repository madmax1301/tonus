<script lang="ts">
  import { onMount } from 'svelte';
  import { apiToken } from '$lib/auth';
  import { defaultProvider, defaultLocation } from '$lib/preferences';
  import {
    providersApi,
    systemApi,
    ApiError,
    type MetadataProvidersResponse,
    type FormatsInfo,
    type HealthResponse
  } from '$lib/api';
  import GlassCard from '$lib/components/GlassCard.svelte';
  import { KeyRound, Server, Library, Trash2, Check } from 'lucide-svelte';

  // Token
  let tokenValue = $state($apiToken);
  let tokenSaved = $state(false);

  function saveToken() {
    apiToken.set(tokenValue.trim());
    tokenSaved = true;
    setTimeout(() => (tokenSaved = false), 1500);
  }

  // Backend-Info (read-only)
  let providers = $state<MetadataProvidersResponse | null>(null);
  let formats = $state<FormatsInfo | null>(null);
  let health = $state<HealthResponse | null>(null);
  let infoError = $state<string | null>(null);

  onMount(async () => {
    try {
      [providers, formats, health] = await Promise.all([
        providersApi.list().catch(() => null),
        systemApi.formats().catch(() => null),
        systemApi.health().catch(() => null)
      ]);
    } catch (err) {
      if (!(err instanceof ApiError && (err.status === 401 || err.status === 403))) {
        infoError = err instanceof Error ? err.message : 'Backend nicht erreichbar';
      }
    }
  });

  // Cache-Reset
  let cacheCleared = $state(false);
  function clearLocalCache() {
    if (!confirm('Alle lokalen Tonus-Einstellungen löschen? Token, Provider, Location, etc.'))
      return;
    const keys: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith('tonus_')) keys.push(k);
    }
    keys.forEach((k) => localStorage.removeItem(k));
    cacheCleared = true;
    setTimeout(() => location.reload(), 800);
  }

  const effectiveProvider = $derived(
    $defaultProvider || providers?.default || ''
  );
</script>

<section class="space-y-8">
  <header class="space-y-2">
    <h1 class="text-4xl font-semibold tracking-tight" style="color: var(--color-fg-primary);">
      Einstellungen
    </h1>
    <p class="text-sm" style="color: var(--color-fg-secondary);">
      Lokale Defaults für deinen Browser, Backend-Konfiguration zur Übersicht.
    </p>
  </header>

  <!-- ─── Authentifizierung ─── -->
  <GlassCard padding="md">
    <div class="space-y-4">
      <div class="flex items-center gap-2">
        <KeyRound size={16} strokeWidth={1.5} style="color: var(--color-accent);" />
        <h2 class="text-[15px] font-medium" style="color: var(--color-fg-primary);">
          Authentifizierung
        </h2>
      </div>
      <label class="block space-y-2">
        <span class="text-[12px]" style="color: var(--color-fg-secondary);">
          API-Token (matcht <code style="color: var(--color-accent);">TONUS_API_TOKEN</code> in
          backend/.env)
        </span>
        <div class="flex items-center gap-2">
          <input
            type="password"
            bind:value={tokenValue}
            spellcheck="false"
            autocomplete="off"
            onkeydown={(e) => e.key === 'Enter' && saveToken()}
            class="flex-1 px-3 py-2 rounded-md text-sm font-mono outline-none focus:border-[var(--color-accent)]"
            style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-primary);"
            placeholder="ttkn_•••••••••••••••••••••••••"
          />
          <button
            onclick={saveToken}
            class="inline-flex items-center gap-1.5 px-4 py-2 rounded-md text-[13px] font-medium transition-opacity"
            style="background: var(--color-accent); color: #1a1410;"
          >
            {#if tokenSaved}
              <Check size={13} strokeWidth={2} />
              gespeichert
            {:else}
              speichern
            {/if}
          </button>
        </div>
      </label>
    </div>
  </GlassCard>

  <!-- ─── Defaults ─── -->
  <GlassCard padding="md">
    <div class="space-y-5">
      <h2 class="text-[15px] font-medium" style="color: var(--color-fg-primary);">
        Standard-Verhalten
      </h2>

      <div class="space-y-2">
        <span class="text-[12px]" style="color: var(--color-fg-secondary);">
          Standard-Provider für Suche &amp; Reverse-Lookup
        </span>
        <div class="flex items-center gap-1 flex-wrap">
          <button
            onclick={() => defaultProvider.set('')}
            class="px-3 py-1.5 rounded-full text-[12px] transition-colors"
            style="background: {!$defaultProvider
              ? 'var(--color-accent)'
              : 'transparent'}; color: {!$defaultProvider
              ? '#1a1410'
              : 'var(--color-fg-secondary)'}; border: 1px solid {!$defaultProvider
              ? 'transparent'
              : 'var(--color-border-soft)'};"
          >
            Backend-Default {providers?.default ? `(${providers.default})` : ''}
          </button>
          {#if providers}
            {#each providers.providers.filter((p) => p.configured) as p}
              <button
                onclick={() => defaultProvider.set(p.id)}
                class="px-3 py-1.5 rounded-full text-[12px] transition-colors"
                style="background: {$defaultProvider === p.id
                  ? 'var(--color-accent)'
                  : 'transparent'}; color: {$defaultProvider === p.id
                  ? '#1a1410'
                  : 'var(--color-fg-secondary)'}; border: 1px solid {$defaultProvider === p.id
                  ? 'transparent'
                  : 'var(--color-border-soft)'};"
              >
                {p.label}
              </button>
            {/each}
          {/if}
        </div>
        <p class="text-[11px]" style="color: var(--color-fg-tertiary);">
          Aktiv: <span style="color: var(--color-fg-secondary);">{effectiveProvider}</span>
        </p>
      </div>

      <div class="space-y-2">
        <span class="text-[12px]" style="color: var(--color-fg-secondary);">
          Standard-Ziel für Downloads
        </span>
        <div class="flex items-center gap-1">
          {#each [{ id: 'navidrome' as const, label: 'Navidrome (in Bibliothek)' }, { id: 'local' as const, label: 'Local (downloads/)' }] as opt}
            <button
              onclick={() => defaultLocation.set(opt.id)}
              class="px-3 py-1.5 rounded-full text-[12px] transition-colors"
              style="background: {$defaultLocation === opt.id
                ? 'var(--color-accent)'
                : 'transparent'}; color: {$defaultLocation === opt.id
                ? '#1a1410'
                : 'var(--color-fg-secondary)'}; border: 1px solid {$defaultLocation === opt.id
                ? 'transparent'
                : 'var(--color-border-soft)'};"
            >
              {opt.label}
            </button>
          {/each}
        </div>
      </div>
    </div>
  </GlassCard>

  <!-- ─── Backend-Konfiguration (read-only) ─── -->
  <GlassCard padding="md">
    <div class="space-y-4">
      <div class="flex items-center gap-2">
        <Server size={16} strokeWidth={1.5} style="color: var(--color-fg-secondary);" />
        <h2 class="text-[15px] font-medium" style="color: var(--color-fg-primary);">
          Backend-Konfiguration
        </h2>
      </div>
      {#if infoError}
        <div class="text-sm" style="color: var(--color-status-error);">{infoError}</div>
      {:else}
        <dl class="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3 text-[13px]">
          <div>
            <dt class="text-[11px] uppercase tracking-widest" style="color: var(--color-fg-tertiary);">
              Default-Provider
            </dt>
            <dd style="color: var(--color-fg-primary);">{providers?.default ?? '—'}</dd>
          </div>
          <div>
            <dt class="text-[11px] uppercase tracking-widest" style="color: var(--color-fg-tertiary);">
              Konfigurierte Provider
            </dt>
            <dd style="color: var(--color-fg-primary);">
              {providers?.providers
                .filter((p) => p.configured)
                .map((p) => p.label)
                .join(', ') ?? '—'}
              {#if providers && providers.providers.some((p) => !p.configured)}
                <span style="color: var(--color-fg-tertiary);" class="ml-2">
                  · nicht: {providers.providers
                    .filter((p) => !p.configured)
                    .map((p) => p.label)
                    .join(', ')}
                </span>
              {/if}
            </dd>
          </div>
          <div>
            <dt class="text-[11px] uppercase tracking-widest" style="color: var(--color-fg-tertiary);">
              Default-Format
            </dt>
            <dd style="color: var(--color-fg-primary);">
              {formats?.default_format ?? '—'}
              {#if formats?.default_quality}
                <span style="color: var(--color-fg-tertiary);"> · {formats.default_quality}</span>
              {/if}
            </dd>
          </div>
          <div>
            <dt class="text-[11px] uppercase tracking-widest" style="color: var(--color-fg-tertiary);">
              Verfügbare Formate
            </dt>
            <dd style="color: var(--color-fg-primary);">
              {formats?.formats.map((f) => f.label).join(', ') ?? '—'}
            </dd>
          </div>
          {#if health?.navidrome_path}
            <div class="sm:col-span-2">
              <dt
                class="text-[11px] uppercase tracking-widest"
                style="color: var(--color-fg-tertiary);"
              >
                Navidrome-Pfad
              </dt>
              <dd
                style="color: var(--color-fg-primary);"
                class="font-mono text-[12px]"
              >
                {health.navidrome_path}
              </dd>
            </div>
          {/if}
        </dl>
      {/if}
    </div>
  </GlassCard>

  <!-- ─── Navidrome-Bibliotheken ─── -->
  {#if health?.navidrome_libraries && health.navidrome_libraries.length > 0}
    <GlassCard padding="md">
      <div class="space-y-3">
        <div class="flex items-center gap-2">
          <Library size={16} strokeWidth={1.5} style="color: var(--color-fg-secondary);" />
          <h2 class="text-[15px] font-medium" style="color: var(--color-fg-primary);">
            Navidrome-Bibliotheken
          </h2>
        </div>
        <div class="space-y-1.5">
          {#each health.navidrome_libraries as lib}
            <div
              class="flex items-center justify-between text-[13px] px-3 py-2 rounded-md"
              style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft);"
            >
              <span style="color: var(--color-fg-primary);" class="font-medium"
                >{lib.label ?? '—'}</span
              >
              <span class="font-mono text-[12px]" style="color: var(--color-fg-secondary);">
                {lib.path}
              </span>
            </div>
          {/each}
        </div>
      </div>
    </GlassCard>
  {/if}

  <!-- ─── Cache-Reset ─── -->
  <GlassCard padding="md">
    <div class="space-y-3">
      <h2 class="text-[15px] font-medium" style="color: var(--color-fg-primary);">
        Lokale Daten
      </h2>
      <p class="text-[12px]" style="color: var(--color-fg-secondary);">
        Alle <code style="color: var(--color-accent);">tonus_*</code>-Schlüssel im Browser-localStorage
        löschen — Token, Defaults, Queue-Snapshot. Setup setzt sich auf den Backend-Default zurück.
      </p>
      <button
        onclick={clearLocalCache}
        disabled={cacheCleared}
        class="inline-flex items-center gap-1.5 px-3 py-2 rounded-md text-[12px] transition-colors"
        style="background: transparent; border: 1px solid var(--color-status-error); color: var(--color-status-error);"
      >
        {#if cacheCleared}
          <Check size={13} strokeWidth={2} />
          geleert · lade neu
        {:else}
          <Trash2 size={13} strokeWidth={1.5} />
          Lokale Daten löschen
        {/if}
      </button>
    </div>
  </GlassCard>
</section>
