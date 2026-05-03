<script lang="ts">
  import { onMount } from 'svelte';
  import { apiToken } from '$lib/auth';
  import { defaultProvider, defaultLocation, defaultFormat, defaultQuality } from '$lib/preferences';
  import {
    providersApi,
    systemApi,
    ApiError,
    type MetadataProvidersResponse,
    type FormatsInfo,
    type HealthResponse
  } from '$lib/api';
  import CinemaBackdrop from '$lib/components/CinemaBackdrop.svelte';
  import VinylWithCover from '$lib/components/VinylWithCover.svelte';
  import { tint, DEFAULT_HUE } from '$lib/accent';
  import { t, lang, type Lang } from '$lib/i18n';
  import {
    KeyRound,
    Server,
    Library,
    Trash2,
    Check,
    Sliders,
    Database,
    Globe
  } from 'lucide-svelte';

  type Section = 'auth' | 'defaults' | 'backend' | 'local' | 'language';
  let section = $state<Section>('auth');

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
        infoError = err instanceof Error ? err.message : 'Backend not reachable';
      }
    }
  });

  // Cache-Reset
  let cacheCleared = $state(false);
  function clearLocalCache() {
    if (!confirm($t('settings.local.confirm'))) return;
    const keys: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith('tonus_')) keys.push(k);
    }
    keys.forEach((k) => localStorage.removeItem(k));
    cacheCleared = true;
    setTimeout(() => location.reload(), 800);
  }

  const effectiveProvider = $derived($defaultProvider || providers?.default || '');
  const accent = $derived(tint(DEFAULT_HUE));

  function pillStyle(active: boolean): string {
    if (active) {
      return `background: ${accent}; color: #1a1410; border: 1px solid transparent; box-shadow: 0 4px 12px rgba(200, 169, 106, 0.25);`;
    }
    return 'background: rgba(255, 255, 255, 0.03); color: var(--color-fg-secondary); border: 1px solid var(--color-border-soft);';
  }
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
        style="font-size: 11px; letter-spacing: 0.24em; color: {accent}; margin-bottom: 14px;"
      >
        {$t('settings.eyebrow')}
      </div>
      <h1
        class="font-semibold m-0"
        style="font-family: var(--font-display); font-size: 48px; font-weight: 600; line-height: 0.95; letter-spacing: -0.035em;"
      >
        {$t('settings.title.before')}<br />
        <em style="color: {accent}; font-weight: 400; font-style: italic;"
          >{$t('settings.title.italic')}</em
        >{$t('settings.title.after')}
      </h1>
      <p
        style="font-size: 14px; color: var(--color-fg-secondary); max-width: 440px; margin-top: 18px; line-height: 1.6;"
      >
        {$t('settings.description.prefix')}
        <code
          style="font-family: var(--font-mono); color: var(--color-fg-secondary); font-size: 12.5px;"
          >{$t('settings.description.env')}</code
        >
        {$t('settings.description.suffix')}
      </p>
    </div>
    <div class="flex justify-center">
      <VinylWithCover
        src={null}
        alt=""
        artist="Setup"
        year={effectiveProvider || ''}
        size={240}
        spinning={false}
      />
    </div>
  </div>

  <!-- ─── Section-Strip ─────────────────────────────────────── -->
  <div
    class="flex items-center"
    style="gap: 24px; font-size: 13px; margin-bottom: 24px; border-bottom: 1px solid var(--color-border-soft); padding-bottom: 14px; flex-wrap: wrap;"
  >
    {#each [
      { id: 'auth' as Section, key: 'settings.section.auth' as const, icon: KeyRound },
      { id: 'defaults' as Section, key: 'settings.section.defaults' as const, icon: Sliders },
      { id: 'backend' as Section, key: 'settings.section.backend' as const, icon: Server },
      { id: 'local' as Section, key: 'settings.section.local' as const, icon: Database },
      { id: 'language' as Section, key: 'settings.section.language' as const, icon: Globe }
    ] as s}
      {@const active = section === s.id}
      <button
        onclick={() => (section = s.id)}
        class="relative inline-flex items-center gap-1.5 transition-colors"
        style="color: {active ? 'var(--color-fg-primary)' : 'var(--color-fg-secondary)'}; font-weight: {active ? 500 : 400}; padding-bottom: 14px; margin-bottom: -14px; border-bottom: 2px solid {active ? accent : 'transparent'};"
      >
        <svelte:component this={s.icon} size={13} strokeWidth={1.5} />
        {$t(s.key)}
      </button>
    {/each}
  </div>

  <!-- ─── Section: Auth ───────────────────────────────────── -->
  {#if section === 'auth'}
    <div
      class="tonus-fadein"
      style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 32px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
    >
      <div
        class="font-semibold uppercase"
        style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary); margin-bottom: 6px;"
      >
        {$t('settings.auth.eyebrow')}
      </div>
      <div
        style="font-family: var(--font-display); font-size: 22px; font-weight: 500; letter-spacing: -0.015em; color: var(--color-fg-primary); margin-bottom: 18px;"
      >
        {$t('settings.auth.title')}
      </div>
      <p
        style="font-size: 13px; color: var(--color-fg-secondary); margin-bottom: 18px; line-height: 1.55; max-width: 560px;"
      >
        {$t('settings.auth.description.prefix')}
        <code style="font-family: var(--font-mono); color: {accent}; font-size: 12px;"
          >{$t('settings.auth.description.env_var')}</code
        >
        {$t('settings.auth.description.middle')}
        <code
          style="font-family: var(--font-mono); color: var(--color-fg-tertiary); font-size: 12px;"
          >{$t('settings.auth.description.env_file')}</code
        >{$t('settings.auth.description.suffix')}
      </p>

      <form
        onsubmit={(e) => {
          e.preventDefault();
          saveToken();
        }}
        class="flex items-center gap-3"
      >
        <input
          id="tonus-token-input"
          name="tonus-api-token"
          type="password"
          bind:value={tokenValue}
          spellcheck="false"
          autocomplete="current-password"
          class="flex-1 outline-none"
          style="background: rgba(0, 0, 0, 0.3); border: 1px solid var(--color-border-soft); border-radius: 14px; color: var(--color-fg-primary); font-family: var(--font-mono); font-size: 13px; padding: 12px 16px; letter-spacing: 0.04em;"
          placeholder="ttkn_•••••••••••••••••••••••••"
        />
        <button
          type="submit"
          class="inline-flex items-center gap-1.5 transition-opacity"
          style="background: {accent}; color: #1a1410; padding: 12px 24px; border-radius: 999px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; box-shadow: 0 8px 20px rgba(200, 169, 106, 0.25);"
        >
          {#if tokenSaved}
            <Check size={13} strokeWidth={2} />
            {$t('common.saved')}
          {:else}
            {$t('common.save')}
          {/if}
        </button>
      </form>
    </div>
  {/if}

  <!-- ─── Section: Defaults ───────────────────────────────── -->
  {#if section === 'defaults'}
    <div class="tonus-fadein space-y-5">
      <!-- Provider -->
      <div
        style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 28px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
      >
        <div class="flex items-baseline justify-between gap-3 mb-3 flex-wrap">
          <div>
            <div
              class="font-semibold uppercase"
              style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary);"
            >
              {$t('settings.defaults.provider.eyebrow')}
            </div>
            <div
              class="mt-1"
              style="font-family: var(--font-display); font-size: 18px; font-weight: 500; color: var(--color-fg-primary);"
            >
              {$t('settings.defaults.provider.title')}
            </div>
          </div>
          <div
            class="text-[11px] tabular-nums"
            style="color: var(--color-fg-tertiary); font-family: var(--font-mono);"
          >
            {$t('settings.defaults.provider.active')}
            <span style="color: {accent};">{effectiveProvider || '—'}</span>
          </div>
        </div>
        <div class="flex items-center gap-1.5 flex-wrap">
          <button
            onclick={() => defaultProvider.set('')}
            class="px-3 py-1.5 rounded-full text-[12px] transition-all"
            style={pillStyle(!$defaultProvider)}
          >
            {$t('settings.defaults.provider.backend_default')}
            {providers?.default ? `(${providers.default})` : ''}
          </button>
          {#if providers}
            {#each providers.providers.filter((p) => p.configured) as p}
              <button
                onclick={() => defaultProvider.set(p.id)}
                class="px-3 py-1.5 rounded-full text-[12px] transition-all"
                style={pillStyle($defaultProvider === p.id)}
              >
                {p.label}
              </button>
            {/each}
          {/if}
        </div>
      </div>

      <!-- Location -->
      <div
        style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 28px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
      >
        <div
          class="font-semibold uppercase"
          style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary);"
        >
          {$t('settings.defaults.location.eyebrow')}
        </div>
        <div
          class="mt-1 mb-3"
          style="font-family: var(--font-display); font-size: 18px; font-weight: 500; color: var(--color-fg-primary);"
        >
          {$t('settings.defaults.location.title')}
        </div>
        <div class="flex items-center gap-1.5 flex-wrap">
          {#each [{ id: 'navidrome' as const, key: 'settings.defaults.location.navidrome' as const }, { id: 'local' as const, key: 'settings.defaults.location.local' as const }] as opt}
            <button
              onclick={() => defaultLocation.set(opt.id)}
              class="px-3 py-1.5 rounded-full text-[12px] transition-all"
              style={pillStyle($defaultLocation === opt.id)}
            >
              {$t(opt.key)}
            </button>
          {/each}
        </div>
      </div>

      <!-- Format & Quality -->
      <div
        style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 28px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
      >
        <div
          class="font-semibold uppercase"
          style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary);"
        >
          {$t('settings.defaults.audio.eyebrow')}
        </div>
        <div
          class="mt-1 mb-3"
          style="font-family: var(--font-display); font-size: 18px; font-weight: 500; color: var(--color-fg-primary);"
        >
          {$t('settings.defaults.audio.title')}
        </div>

        <div
          class="text-[11px] uppercase mb-2"
          style="color: var(--color-fg-tertiary); letter-spacing: 0.12em;"
        >
          {$t('settings.defaults.audio.format_label')}
        </div>
        <div class="flex items-center gap-1.5 flex-wrap mb-4">
          <button
            onclick={() => defaultFormat.set('')}
            class="px-3 py-1.5 rounded-full text-[12px] transition-all"
            style={pillStyle(!$defaultFormat)}
          >
            {$t('settings.defaults.provider.backend_default')}
            {formats?.default_format ? `(${formats.default_format})` : ''}
          </button>
          {#if formats}
            {#each formats.formats as f}
              <button
                onclick={() => defaultFormat.set(f.value)}
                title={f.description ?? ''}
                class="px-3 py-1.5 rounded-full text-[12px] transition-all"
                style={pillStyle($defaultFormat === f.value)}
              >
                {f.label}
              </button>
            {/each}
          {/if}
        </div>

        <div
          class="text-[11px] uppercase mb-2"
          style="color: var(--color-fg-tertiary); letter-spacing: 0.12em;"
        >
          {$t('settings.defaults.audio.bitrate_label')}
        </div>
        <div class="flex items-center gap-1.5 flex-wrap">
          <button
            onclick={() => defaultQuality.set('')}
            class="px-3 py-1.5 rounded-full text-[12px] transition-all"
            style={pillStyle(!$defaultQuality)}
          >
            {$t('settings.defaults.provider.backend_default')}
            {formats?.default_quality ? `(${formats.default_quality})` : ''}
          </button>
          {#if formats}
            {#each formats.qualities as q}
              <button
                onclick={() => defaultQuality.set(q.value)}
                title={q.description ?? ''}
                class="px-3 py-1.5 rounded-full text-[12px] transition-all"
                style={pillStyle($defaultQuality === q.value)}
              >
                {q.label}
              </button>
            {/each}
          {/if}
        </div>
        <p class="mt-3 text-[11px]" style="color: var(--color-fg-tertiary); line-height: 1.55;">
          {$t('settings.defaults.audio.note')}
        </p>
      </div>
    </div>
  {/if}

  <!-- ─── Section: Backend ────────────────────────────────── -->
  {#if section === 'backend'}
    <div
      class="tonus-fadein"
      style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 32px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
    >
      {#if infoError}
        <div class="text-[13px]" style="color: var(--color-status-error);">{infoError}</div>
      {:else}
        <div
          class="font-semibold uppercase"
          style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary); margin-bottom: 6px;"
        >
          {$t('settings.backend.eyebrow')}
        </div>
        <div
          style="font-family: var(--font-display); font-size: 22px; font-weight: 500; letter-spacing: -0.015em; color: var(--color-fg-primary); margin-bottom: 22px;"
        >
          {$t('settings.backend.title')}
        </div>

        <dl class="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-5 text-[13px]">
          <div>
            <dt
              class="text-[10.5px] uppercase tracking-widest"
              style="color: var(--color-fg-tertiary); letter-spacing: 0.18em;"
            >
              {$t('settings.backend.field.default_provider')}
            </dt>
            <dd
              class="mt-1"
              style="color: var(--color-fg-primary); font-family: var(--font-mono); font-size: 14px;"
            >
              {providers?.default ?? '—'}
            </dd>
          </div>
          <div>
            <dt
              class="text-[10.5px] uppercase tracking-widest"
              style="color: var(--color-fg-tertiary); letter-spacing: 0.18em;"
            >
              {$t('settings.backend.field.configured_providers')}
            </dt>
            <dd class="mt-1" style="color: var(--color-fg-primary);">
              {providers?.providers
                .filter((p) => p.configured)
                .map((p) => p.label)
                .join(', ') ?? '—'}
              {#if providers && providers.providers.some((p) => !p.configured)}
                <div class="mt-1 text-[11px]" style="color: var(--color-fg-tertiary);">
                  {$t('settings.backend.field.missing_providers')}
                  {providers.providers
                    .filter((p) => !p.configured)
                    .map((p) => p.label)
                    .join(', ')}
                </div>
              {/if}
            </dd>
          </div>
          <div>
            <dt
              class="text-[10.5px] uppercase tracking-widest"
              style="color: var(--color-fg-tertiary); letter-spacing: 0.18em;"
            >
              {$t('settings.backend.field.default_format')}
            </dt>
            <dd
              class="mt-1"
              style="color: var(--color-fg-primary); font-family: var(--font-mono); font-size: 14px;"
            >
              {formats?.default_format ?? '—'}
              {#if formats?.default_quality}
                <span style="color: var(--color-fg-tertiary);"> · {formats.default_quality}</span>
              {/if}
            </dd>
          </div>
          <div>
            <dt
              class="text-[10.5px] uppercase tracking-widest"
              style="color: var(--color-fg-tertiary); letter-spacing: 0.18em;"
            >
              {$t('settings.backend.field.available_formats')}
            </dt>
            <dd class="mt-1" style="color: var(--color-fg-primary);">
              {formats?.formats.map((f) => f.label).join(', ') ?? '—'}
            </dd>
          </div>
          {#if health?.navidrome_path}
            <div class="sm:col-span-2">
              <dt
                class="text-[10.5px] uppercase tracking-widest"
                style="color: var(--color-fg-tertiary); letter-spacing: 0.18em;"
              >
                {$t('settings.backend.field.navidrome_path')}
              </dt>
              <dd
                class="mt-1"
                style="color: var(--color-fg-primary); font-family: var(--font-mono); font-size: 12.5px; word-break: break-all;"
              >
                {health.navidrome_path}
              </dd>
            </div>
          {/if}
        </dl>
      {/if}

      {#if health?.navidrome_libraries && health.navidrome_libraries.length > 0}
        <div class="mt-6 pt-6" style="border-top: 1px solid var(--color-border-soft);">
          <div class="flex items-center gap-2 mb-3">
            <Library size={14} strokeWidth={1.5} style="color: {accent};" />
            <div
              class="font-semibold uppercase"
              style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary);"
            >
              {$t('settings.backend.libraries.title', {
                count: health.navidrome_libraries.length
              })}
            </div>
          </div>
          <div class="space-y-1.5">
            {#each health.navidrome_libraries as lib}
              <div
                class="flex items-center justify-between gap-3 px-4 py-2.5 rounded-md"
                style="background: rgba(0, 0, 0, 0.25); border: 1px solid var(--color-border-soft);"
              >
                <span class="font-medium text-[13px]" style="color: var(--color-fg-primary);"
                  >{lib.label ?? '—'}</span
                >
                <span
                  class="text-[11.5px] truncate"
                  style="color: var(--color-fg-tertiary); font-family: var(--font-mono);"
                >
                  {lib.path}
                </span>
              </div>
            {/each}
          </div>
        </div>
      {/if}
    </div>
  {/if}

  <!-- ─── Section: Local ──────────────────────────────────── -->
  {#if section === 'local'}
    <div
      class="tonus-fadein"
      style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 32px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
    >
      <div
        class="font-semibold uppercase"
        style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary); margin-bottom: 6px;"
      >
        {$t('settings.local.eyebrow')}
      </div>
      <div
        style="font-family: var(--font-display); font-size: 22px; font-weight: 500; letter-spacing: -0.015em; color: var(--color-fg-primary); margin-bottom: 18px;"
      >
        {$t('settings.local.title')}
      </div>
      <p
        style="font-size: 13px; color: var(--color-fg-secondary); line-height: 1.55; max-width: 560px; margin-bottom: 22px;"
      >
        {$t('settings.local.description.prefix')}
        <code style="font-family: var(--font-mono); color: {accent}; font-size: 12px;"
          >{$t('settings.local.description.key')}</code
        >{$t('settings.local.description.suffix')}
      </p>
      <button
        onclick={clearLocalCache}
        disabled={cacheCleared}
        class="inline-flex items-center gap-2 transition-colors disabled:opacity-60"
        style="background: rgba(255, 69, 58, 0.08); border: 1px solid var(--color-status-error); color: var(--color-status-error); padding: 10px 22px; border-radius: 999px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;"
      >
        {#if cacheCleared}
          <Check size={13} strokeWidth={2} />
          {$t('settings.local.cleared')}
        {:else}
          <Trash2 size={13} strokeWidth={1.8} />
          {$t('settings.local.button')}
        {/if}
      </button>
    </div>
  {/if}

  <!-- ─── Section: Language ──────────────────────────────── -->
  {#if section === 'language'}
    <div
      class="tonus-fadein"
      style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 32px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
    >
      <div
        class="font-semibold uppercase"
        style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary); margin-bottom: 6px;"
      >
        {$t('settings.language.eyebrow')}
      </div>
      <div
        style="font-family: var(--font-display); font-size: 22px; font-weight: 500; letter-spacing: -0.015em; color: var(--color-fg-primary); margin-bottom: 18px;"
      >
        {$t('settings.language.title')}
      </div>
      <p
        style="font-size: 13px; color: var(--color-fg-secondary); line-height: 1.55; max-width: 560px; margin-bottom: 22px;"
      >
        {$t('settings.language.description')}
      </p>
      <div class="flex items-center gap-1.5 flex-wrap">
        {#each [{ id: 'de' as Lang, key: 'settings.language.de' as const }, { id: 'en' as Lang, key: 'settings.language.en' as const }] as opt}
          <button
            onclick={() => lang.set(opt.id)}
            class="px-4 py-2 rounded-full text-[12px] transition-all"
            style={pillStyle($lang === opt.id)}
          >
            {$t(opt.key)}
          </button>
        {/each}
      </div>
    </div>
  {/if}
</section>
