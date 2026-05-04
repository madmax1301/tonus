<script lang="ts">
  /**
   * Login + Setup-Wizard für Tonus. Drei Modi:
   *
   *   1. Setup-Mode (auth_required=false UND setup_required=true):
   *      Username + Password + optional "TOTP gleich aktivieren"
   *      → POST /api/auth/setup → JWT direkt im Response
   *
   *   2. Login-Mode (Standard nach Setup):
   *      Username + Password (+ TOTP wenn aktiv)
   *      → POST /api/auth/login → JWT pair
   *
   *   3. TOTP-Required-Mode (Login antwortete mit X-Auth-Required-2FA-Header
   *      bzw. ApiError-Body "totp"): zeigt zusätzliches Code-Feld + Re-Submit.
   *
   *   4. TOTP-QR-Mode (Setup mit enable_totp=true erfolgreich, Server hat
   *      totp_secret + totp_uri zurückgegeben): zeigt QR-Code + Continue-CTA.
   */
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { authApi, providersConfigApi, ApiError, type ProviderConfig } from '$lib/api';
  import { setJwtPair, currentUser } from '$lib/auth';
  import { tint, DEFAULT_HUE } from '$lib/accent';
  import CinemaBackdrop from '$lib/components/CinemaBackdrop.svelte';
  import VinylWithCover from '$lib/components/VinylWithCover.svelte';
  import { t } from '$lib/i18n';
  import { Loader2, KeyRound, ShieldCheck, User, Plug, AlertTriangle, Check } from 'lucide-svelte';

  // Modi:
  //   login       → Standard-Eingabe Username/Password
  //   setup       → First-Run Wizard (kein User in DB)
  //   totp-qr     → nach Setup mit enable_totp=true: QR + Verify
  //   onboarding  → nach Setup (oder TOTP-Confirm): Schritt 2/2 Provider
  //                 verbinden. Kann übersprungen werden.
  let mode = $state<'login' | 'setup' | 'totp-qr' | 'onboarding'>('login');

  // Form state
  let username = $state('');
  let password = $state('');
  let totpCode = $state('');
  let enableTotp = $state(false);
  let needsTotp = $state(false);

  // After-Setup-QR state. qr_data_url ist eine server-rendered PNG-data-URL
  // (siehe /api/auth/setup) — bevorzugt vor totpUri, weil der otpauth-URI
  // das Klartext-Secret enthält. Server-Render = Secret bleibt im Backend.
  let totpSecret = $state<string | null>(null);
  let totpUri = $state<string | null>(null);
  let totpQrDataUrl = $state<string | null>(null);
  let totpConfirmCode = $state('');

  let busy = $state(false);
  let errorMsg = $state<string | null>(null);

  const accent = tint(DEFAULT_HUE);

  onMount(async () => {
    try {
      const status = await authApi.setupStatus();
      if (status.setup_required) {
        mode = 'setup';
      }
    } catch {
      // Backend nicht erreichbar — bleibt im Login-Mode, der User sieht die
      // Fehlermeldung beim Submit-Versuch.
    }
  });

  function mapError(err: unknown): string {
    if (err instanceof ApiError) {
      if (err.status === 401) {
        // Backend setzt einen Detail-Text; "2FA-Code" hint zur TOTP-Erkennung
        const detail =
          err.body && typeof err.body === 'object' && 'detail' in err.body
            ? String((err.body as { detail: unknown }).detail)
            : '';
        if (detail.toLowerCase().includes('2fa') || detail.toLowerCase().includes('totp')) {
          needsTotp = true;
          return $t('auth.login.error_totp');
        }
        return $t('auth.login.error_invalid');
      }
      if (err.status === 429) return $t('auth.login.error_rate');
    }
    return $t('auth.login.error_generic');
  }

  async function confirmTotp() {
    if (!totpSecret) {
      // Kein QR-Step lief — direkt zum Provider-Onboarding.
      mode = 'onboarding';
      void loadOnboardingProviders();
      return;
    }
    if (!/^\d{6}$/.test(totpConfirmCode.replace(/\s/g, ''))) {
      errorMsg = $t('auth.login.error_totp');
      return;
    }
    busy = true;
    errorMsg = null;
    try {
      await authApi.totpConfirm(totpSecret, totpConfirmCode.replace(/\s/g, ''));
      // TOTP erfolgreich aktiv → weiter zu Schritt 2/2 (Provider verbinden).
      currentUser.update((u) => (u ? { ...u, totp_enabled: true } : u));
      mode = 'onboarding';
      void loadOnboardingProviders();
    } catch (err) {
      errorMsg =
        err instanceof ApiError && err.status === 401
          ? $t('auth.login.error_totp')
          : $t('auth.login.error_generic');
    } finally {
      busy = false;
    }
  }

  // ── Onboarding Step 2/2: Provider verbinden ────────────────────
  let onboardingProviders = $state<ProviderConfig[]>([]);
  let onboardingLoaded = $state(false);
  let onboardingError = $state<string | null>(null);
  let onboardingForm = $state<Record<string, Record<string, string>>>({});
  let onboardingSavingName = $state<string | null>(null);
  let onboardingSavedNames = $state<Set<string>>(new Set());
  // null ⇒ Provider-Liste; gesetzt ⇒ Detail-Form für diesen Provider.
  let selectedProvider = $state<string | null>(null);

  // Provider-Decor, am Mock orientiert: Avatar-Color + Type-Tag. Tag
  // beschreibt was der User für den Setup hinterlegen muss (API Key,
  // Cookies, Login-Daten). Apple Music = "Soon", non-clickable.
  const PROVIDER_DECOR: Record<
    string,
    { color: string; tag: string; primary?: boolean }
  > = {
    spotify: { color: '#1ed760', tag: 'API Key', primary: true },
    navidrome: { color: '#22d3ee', tag: 'URL + Login' },
    youtube: { color: '#ff4040', tag: 'Cookies' }
  };
  // Coming-soon-Stub-Cards (visuelle Konsistenz mit dem Mock).
  const COMING_SOON_PROVIDERS = [
    { name: 'apple', label: 'Apple Music', tag: 'Soon' }
  ];

  async function loadOnboardingProviders() {
    onboardingError = null;
    try {
      const res = await providersConfigApi.list();
      onboardingProviders = res.providers;
      const buf: Record<string, Record<string, string>> = {};
      for (const p of res.providers) {
        buf[p.name] = {};
        for (const f of p.fields) {
          buf[p.name][f.key] = f.secret ? '' : f.value;
        }
      }
      onboardingForm = buf;
      onboardingLoaded = true;
    } catch {
      // 403 dürfte nicht passieren weil Setup-User automatisch Admin ist;
      // aber falls doch (z.B. legacy-Auth-Pfad): Onboarding stillschweigend
      // skippen — der User kann's später über Settings nachholen.
      onboardingLoaded = true;
      onboardingError = $t('auth.onboarding.error_load');
    }
  }

  async function saveOnboardingProvider(p: ProviderConfig) {
    onboardingSavingName = p.name;
    onboardingError = null;
    try {
      const fields: Record<string, string> = {};
      for (const f of p.fields) {
        const buf = onboardingForm[p.name]?.[f.key] ?? '';
        if (f.secret) {
          if (buf !== '') fields[f.key] = buf;
        } else {
          fields[f.key] = buf;
        }
      }
      await providersConfigApi.update(p.name, fields);
      onboardingSavedNames = new Set([...onboardingSavedNames, p.name]);
      // Reload damit is_set-Flags + masked-Placeholder neu greifen.
      await loadOnboardingProviders();
    } catch {
      onboardingError = $t('auth.onboarding.error_save');
    } finally {
      onboardingSavingName = null;
    }
  }

  async function finishOnboarding() {
    await goto(`${base}/`);
  }

  async function submit() {
    busy = true;
    errorMsg = null;
    try {
      if (mode === 'setup') {
        if (password.length < 8) {
          errorMsg = $t('auth.setup.password_min');
          busy = false;
          return;
        }
        const r = await authApi.setup(username.trim(), password, enableTotp);
        setJwtPair(r.tokens);
        currentUser.set({
          id: r.user.id,
          username: r.user.username,
          is_admin: r.user.is_admin,
          totp_enabled: !!r.totp_secret
        });
        if (r.totp_secret && r.totp_uri) {
          // Show QR-step before continuing.
          totpSecret = r.totp_secret;
          totpUri = r.totp_uri;
          totpQrDataUrl = r.totp_qr_data_url ?? null;
          mode = 'totp-qr';
        } else {
          // Kein TOTP gewählt → direkt zu Schritt 2/2 (Provider verbinden).
          mode = 'onboarding';
          void loadOnboardingProviders();
        }
      } else {
        const r = await authApi.login(username.trim(), password, totpCode.trim() || undefined);
        setJwtPair(r.tokens);
        currentUser.set({
          id: r.user.id,
          username: r.user.username,
          is_admin: r.user.is_admin,
          totp_enabled: r.user.totp_enabled
        });
        await goto(`${base}/`);
      }
    } catch (err) {
      errorMsg = mapError(err);
    } finally {
      busy = false;
    }
  }

  // QR-Code-Image: bevorzugt server-rendered (qr_data_url aus /api/auth/setup),
  // weil der otpauth-URI das Klartext-Secret enthält und ein externer
  // QR-Service ihn mitloggen würde. Falls der Server das Feld noch nicht
  // liefert (z.B. ältere Backend-Version), gibt's keinen Fallback —
  // dann eben kein QR, der User tippt das Manual-Secret ab. Sicherheit > Komfort.
  const qrImgSrc = $derived(totpQrDataUrl);
</script>

<CinemaBackdrop hue={DEFAULT_HUE} intensity={0.8} />

{#if mode === 'onboarding'}
  <!-- ─── Onboarding 2-Column-Layout (Brand-Stage links, Card rechts) ─── -->
  <section
    class="relative z-10 mx-auto"
    style="max-width: 1180px; width: calc(100% - 48px); padding: 60px 0;"
  >
    <div
      class="grid"
      style="grid-template-columns: 1.1fr 1fr; gap: 48px; align-items: center; min-height: calc(100vh - 120px);"
    >
      <!-- Left: Brand stage -->
      <div
        class="flex flex-col"
        style="justify-content: space-between; min-height: 540px; padding: 12px 0;"
      >
        <div class="flex items-center" style="gap: 11px;">
          <div
            style="width: 28px; height: 28px; border-radius: 8px; background: linear-gradient(135deg, {accent}, oklch(35% 0.15 30)); position: relative;"
          >
            <div
              style="position: absolute; inset: 32%; border-radius: 50%; background: #0a0a0c;"
            ></div>
          </div>
          <span
            style="font-family: var(--font-display); font-size: 22px; font-weight: 600; letter-spacing: -0.02em;"
            >Tonus</span
          >
        </div>

        <div>
          <div
            class="font-semibold uppercase"
            style="font-size: 11px; letter-spacing: 0.24em; color: {accent}; font-weight: 600; margin-bottom: 14px;"
          >
            Discovery → Library
          </div>
          <h1
            class="m-0"
            style="font-family: var(--font-display); font-size: 64px; font-weight: 600; line-height: 0.95; letter-spacing: -0.04em;"
          >
            Sammeln, was<br />du <span style="color: {accent};">hörst.</span>
          </h1>
          <p
            style="font-size: 16px; color: var(--color-fg-secondary); max-width: 480px; margin-top: 18px; line-height: 1.55; font-weight: 300;"
          >
            Verbinde Spotify und Tonus zieht jeden Track, den du likest, automatisch in deine
            eigene Bibliothek. Lokal. Verlustfrei. Für immer dein.
          </p>
        </div>

        <div
          class="flex flex-wrap"
          style="gap: 14px; font-size: 11px; color: var(--color-fg-tertiary); font-family: var(--font-mono); letter-spacing: 0.1em; text-transform: uppercase;"
        >
          <span>● Self-hosted</span>
          <span>● No telemetry</span>
          <span>● FLAC default</span>
        </div>
      </div>

      <!-- Right: Auth card -->
      <div class="flex" style="align-items: center;">
        <div
          class="w-full"
          style="max-width: 420px; background: rgba(20, 20, 24, 0.6); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 20px; padding: 32px 32px 28px; box-shadow: 0 30px 80px rgba(0, 0, 0, 0.5);"
        >
          {#if selectedProvider}
            {@const p = onboardingProviders.find((x) => x.name === selectedProvider)}
            {#if p}
              {@const decor = PROVIDER_DECOR[p.name] ?? { color: 'rgba(255,255,255,0.08)', tag: '' }}
              <button
                type="button"
                onclick={() => (selectedProvider = null)}
                class="inline-flex items-center transition-opacity"
                style="background: none; border: none; padding: 0; color: var(--color-fg-secondary); font-size: 12px; cursor: pointer; margin-bottom: 14px; gap: 6px;"
              >
                ← Zurück zur Liste
              </button>
              <div class="flex items-center" style="gap: 12px; margin-bottom: 14px;">
                <div
                  class="flex items-center justify-center"
                  style="width: 30px; height: 30px; border-radius: 7px; background: {decor.color}; font-family: var(--font-display); font-weight: 700; font-size: 14px; color: #0a0a0c;"
                >
                  {p.label[0]}
                </div>
                <div
                  style="font-family: var(--font-display); font-size: 22px; font-weight: 600; letter-spacing: -0.02em;"
                >
                  {p.label}
                </div>
              </div>
              {#if p.name === 'spotify'}
                <details
                  style="background: rgba(30, 215, 96, 0.04); border: 1px solid rgba(30, 215, 96, 0.18); border-radius: 10px; padding: 10px 12px; margin-bottom: 12px;"
                >
                  <summary
                    class="cursor-pointer uppercase"
                    style="font-size: 10px; letter-spacing: 0.18em; color: rgba(134, 239, 172, 0.95); list-style: none; font-weight: 600;"
                  >
                    {$t('settings.connections.spotify.help_title')}
                  </summary>
                  <ol
                    style="margin: 10px 0 0 18px; padding: 0; font-size: 12px; color: var(--color-fg-secondary); line-height: 1.55;"
                  >
                    <li style="margin-bottom: 5px;">
                      {$t('settings.connections.spotify.help_step1')}
                    </li>
                    <li style="margin-bottom: 5px;">
                      {$t('settings.connections.spotify.help_step2')}
                    </li>
                    <li style="margin-bottom: 5px;">
                      {$t('settings.connections.spotify.help_step3')}
                    </li>
                    <li style="margin-bottom: 5px;">
                      {$t('settings.connections.spotify.help_step4')}
                    </li>
                    <li>{$t('settings.connections.spotify.help_step5')}</li>
                  </ol>
                  <a
                    href="https://developer.spotify.com/dashboard"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="inline-flex items-center"
                    style="margin-top: 8px; font-size: 11.5px; color: rgba(134, 239, 172, 0.95); text-decoration: underline; text-underline-offset: 3px;"
                  >
                    {$t('settings.connections.spotify.help_link')}
                  </a>
                </details>
              {/if}
              <div class="flex flex-col" style="gap: 10px;">
                {#each p.fields as f (f.key)}
                  <label class="flex flex-col gap-1.5" for="onb-{p.name}-{f.key}">
                    <span
                      class="uppercase"
                      style="font-size: 10px; letter-spacing: 0.18em; color: var(--color-fg-tertiary);"
                    >
                      {f.label}
                    </span>
                    <input
                      id="onb-{p.name}-{f.key}"
                      type={f.secret ? 'password' : 'text'}
                      autocomplete={f.secret ? 'new-password' : 'off'}
                      spellcheck="false"
                      bind:value={onboardingForm[p.name][f.key]}
                      placeholder={f.secret && f.is_set
                        ? $t('settings.connections.secret_placeholder')
                        : ''}
                      class="outline-none"
                      style="background: rgba(0, 0, 0, 0.3); border: 1px solid var(--color-border-soft); border-radius: 12px; color: var(--color-fg-primary); font-family: var(--font-mono); font-size: 12.5px; padding: 10px 14px; letter-spacing: 0.04em;"
                    />
                  </label>
                {/each}
                <div class="flex items-center" style="gap: 10px; margin-top: 6px;">
                  <button
                    type="button"
                    disabled={onboardingSavingName === p.name}
                    onclick={async () => {
                      await saveOnboardingProvider(p);
                      selectedProvider = null;
                    }}
                    class="inline-flex items-center justify-center transition-opacity"
                    style="background: {accent}; color: #0a0a0c; padding: 10px 18px; border-radius: 999px; font-size: 11.5px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; box-shadow: 0 6px 16px {accent}30; opacity: {onboardingSavingName === p.name ? 0.6 : 1}; cursor: {onboardingSavingName === p.name ? 'wait' : 'pointer'};"
                  >
                    {onboardingSavingName === p.name
                      ? $t('settings.connections.saving')
                      : $t('settings.connections.save')}
                  </button>
                </div>
              </div>
            {/if}
          {:else}
            <div
              class="font-semibold uppercase"
              style="font-size: 11px; color: var(--color-fg-tertiary); letter-spacing: 0.16em; font-weight: 600;"
            >
              {$t('auth.onboarding.eyebrow')}
            </div>
            <div
              style="font-family: var(--font-display); font-size: 26px; font-weight: 600; margin-top: 8px; letter-spacing: -0.025em;"
            >
              Provider auswählen
            </div>
            <div
              style="font-size: 13px; color: var(--color-fg-secondary); margin-top: 6px; line-height: 1.55;"
            >
              {$t('auth.onboarding.body')}
            </div>

            {#if !onboardingLoaded}
              <p
                style="margin-top: 22px; font-size: 12px; color: var(--color-fg-tertiary);"
              >…</p>
            {:else}
              <div class="flex flex-col" style="gap: 8px; margin-top: 22px;">
                {#each onboardingProviders as p (p.name)}
                  {@const decor = PROVIDER_DECOR[p.name] ?? { color: 'rgba(255,255,255,0.08)', tag: '', primary: false }}
                  {@const saved = onboardingSavedNames.has(p.name)}
                  <button
                    type="button"
                    onclick={() => (selectedProvider = p.name)}
                    class="flex items-center transition-colors text-left"
                    style="gap: 12px; padding: 14px 16px; border-radius: 12px; background: {decor.primary ? `${accent}1a` : 'rgba(255,255,255,0.04)'}; border: 1px solid {decor.primary ? `${accent}55` : 'var(--color-border-soft)'}; color: var(--color-fg-primary); cursor: pointer;"
                  >
                    <div
                      class="flex items-center justify-center flex-shrink-0"
                      style="width: 30px; height: 30px; border-radius: 7px; background: {decor.color}; font-family: var(--font-display); font-weight: 700; font-size: 14px; color: #0a0a0c;"
                    >
                      {p.label[0]}
                    </div>
                    <div style="flex: 1; min-width: 0;">
                      <div
                        style="font-size: 14px; font-weight: 600; letter-spacing: -0.01em;"
                      >{p.label}</div>
                      <div
                        class="uppercase"
                        style="font-size: 10.5px; color: var(--color-fg-tertiary); margin-top: 2px; font-family: var(--font-mono); letter-spacing: 0.08em;"
                      >{decor.tag}</div>
                    </div>
                    {#if saved}
                      <Check size={14} strokeWidth={2.4} style="color: rgba(134, 239, 172, 0.95); flex-shrink: 0;" />
                    {:else}
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={decor.primary ? accent : 'var(--color-fg-secondary)'} stroke-width="2" style="flex-shrink: 0;">
                        <path d="M9 6l6 6-6 6" />
                      </svg>
                    {/if}
                  </button>
                {/each}
                {#each COMING_SOON_PROVIDERS as cs}
                  <div
                    class="flex items-center"
                    style="gap: 12px; padding: 14px 16px; border-radius: 12px; background: rgba(255,255,255,0.02); border: 1px solid var(--color-border-soft); opacity: 0.5; cursor: not-allowed;"
                  >
                    <div
                      class="flex items-center justify-center flex-shrink-0"
                      style="width: 30px; height: 30px; border-radius: 7px; background: rgba(255,255,255,0.08); font-family: var(--font-display); font-weight: 700; font-size: 14px; color: var(--color-fg-secondary);"
                    >
                      {cs.label[0]}
                    </div>
                    <div style="flex: 1;">
                      <div style="font-size: 14px; font-weight: 600; letter-spacing: -0.01em; color: var(--color-fg-secondary);">{cs.label}</div>
                      <div class="uppercase" style="font-size: 10.5px; color: var(--color-fg-tertiary); margin-top: 2px; font-family: var(--font-mono); letter-spacing: 0.08em;">{cs.tag}</div>
                    </div>
                  </div>
                {/each}
              </div>

              {#if onboardingError}
                <p style="margin-top: 14px; font-size: 11px; color: #f87171;">{onboardingError}</p>
              {/if}

              {#if onboardingSavedNames.size > 0}
                <div
                  class="inline-flex items-center"
                  style="margin-top: 18px; padding: 10px 14px; border: 1px solid var(--color-border-soft); border-radius: 8px; font-size: 11.5px; color: var(--color-fg-secondary); line-height: 1.5; gap: 8px;"
                >
                  <AlertTriangle size={12} strokeWidth={2} style="color: rgba(248, 195, 113, 0.95); flex-shrink: 0;" />
                  <span>{$t('auth.onboarding.restart_required')}</span>
                </div>
              {/if}

              <div class="flex items-center flex-wrap" style="gap: 10px; margin-top: 20px;">
                <button
                  type="button"
                  onclick={finishOnboarding}
                  class="inline-flex items-center transition-opacity"
                  style="padding: 11px 22px; border-radius: 999px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; background: {accent}; color: #0a0a0c; border: none; box-shadow: 0 8px 24px {accent}40; cursor: pointer;"
                >
                  {$t('auth.onboarding.continue')}
                </button>
                <button
                  type="button"
                  onclick={finishOnboarding}
                  class="inline-flex items-center transition-colors"
                  style="padding: 11px 20px; border-radius: 999px; font-size: 12px; font-weight: 500; letter-spacing: 0.02em; background: transparent; color: var(--color-fg-secondary); border: 1px solid var(--color-border-soft);"
                >
                  {$t('auth.onboarding.skip')}
                </button>
              </div>
            {/if}
          {/if}
        </div>
      </div>
    </div>
  </section>
{:else}
<section
  class="relative z-10 mx-auto"
  style="max-width: 460px; width: calc(100% - 48px); padding: 80px 0 60px;"
>
  <div class="flex flex-col items-center" style="gap: 24px; text-align: center;">
    <VinylWithCover
      src={null}
      alt=""
      artist="Tonus"
      year={mode === 'setup' ? 'Setup' : ''}
      size={140}
      spinning={busy}
    />

    {#if mode === 'totp-qr'}
      <!-- 2FA-QR step (after successful setup with enable_totp=true) -->
      <div
        class="font-semibold uppercase"
        style="font-size: 11px; letter-spacing: 0.24em; color: {accent}; font-weight: 600;"
      >
        {$t('auth.totp_qr.eyebrow')}
      </div>
      <h1
        class="m-0"
        style="
          font-family: var(--font-display);
          font-size: 36px;
          font-weight: 600;
          letter-spacing: -0.03em;
          line-height: 1;
          color: var(--color-fg-primary);
        "
      >
        {$t('auth.totp_qr.title')}
      </h1>
      <p style="font-size: 14px; color: var(--color-fg-secondary); line-height: 1.55; max-width: 400px;">
        {$t('auth.totp_qr.body')}
      </p>
      {#if qrImgSrc}
        <div
          style="
            background: rgba(255, 255, 255, 0.95);
            padding: 14px;
            border-radius: 14px;
            box-shadow: 0 16px 40px rgba(0, 0, 0, 0.4);
          "
        >
          <img src={qrImgSrc} alt="TOTP QR" width="220" height="220" />
        </div>
      {/if}
      {#if totpSecret}
        <div
          class="w-full"
          style="
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--color-border-soft);
            border-radius: 12px;
            padding: 14px 16px;
          "
        >
          <div
            class="uppercase"
            style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary); margin-bottom: 6px;"
          >
            {$t('auth.totp_qr.secret_label')}
          </div>
          <code
            style="font-family: var(--font-mono); font-size: 13px; color: var(--color-fg-primary); letter-spacing: 0.04em; word-break: break-all;"
          >
            {totpSecret}
          </code>
        </div>
      {/if}
      <!-- Verify-Code-Eingabe: User muss den ersten Code aus seiner App
           eingeben, sonst wird TOTP nicht scharf geschaltet. -->
      <form
        onsubmit={(e) => {
          e.preventDefault();
          confirmTotp();
        }}
        class="w-full"
        style="display: flex; flex-direction: column; gap: 12px;"
      >
        <input
          type="text"
          bind:value={totpConfirmCode}
          inputmode="numeric"
          pattern="[0-9]*"
          maxlength="6"
          autocomplete="one-time-code"
          required
          placeholder="000 000"
          class="w-full outline-none"
          style="
            background: rgba(0, 0, 0, 0.3);
            border: 1px solid var(--color-border-soft);
            border-radius: 14px;
            color: var(--color-fg-primary);
            font-family: var(--font-mono);
            font-size: 22px;
            padding: 14px 16px;
            letter-spacing: 0.4em;
            text-align: center;
          "
        />
        {#if errorMsg}
          <div
            style="
              padding: 10px 14px;
              background: rgba(255, 69, 58, 0.08);
              border: 1px solid var(--color-status-error);
              border-radius: 10px;
              color: var(--color-status-error);
              font-size: 12px;
              text-align: left;
            "
          >
            {errorMsg}
          </div>
        {/if}
        <button
          type="submit"
          disabled={busy}
          class="inline-flex items-center justify-center gap-1.5 transition-transform disabled:opacity-50"
          style="
            padding: 12px 24px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            background: {accent};
            color: #0a0a0c;
            border: none;
            box-shadow: 0 8px 24px {accent}40;
            cursor: pointer;
          "
        >
          {#if busy}
            <Loader2 size={13} class="animate-spin" />
          {:else}
            <ShieldCheck size={13} strokeWidth={2} />
          {/if}
          {$t('auth.totp_qr.continue')}
        </button>
      </form>
    {:else}
      <div
        class="font-semibold uppercase"
        style="font-size: 11px; letter-spacing: 0.24em; color: {accent}; font-weight: 600;"
      >
        {mode === 'setup' ? $t('auth.setup.eyebrow') : $t('auth.login.eyebrow')}
      </div>
      <h1
        class="m-0"
        style="
          font-family: var(--font-display);
          font-size: 44px;
          font-weight: 600;
          letter-spacing: -0.035em;
          line-height: 1;
          color: var(--color-fg-primary);
        "
      >
        {mode === 'setup' ? $t('auth.setup.title.before') : $t('auth.login.title.before')}
        <em style="color: {accent}; font-weight: 400; font-style: italic;">
          {mode === 'setup' ? $t('auth.setup.title.italic') : $t('auth.login.title.italic')}
        </em>
      </h1>
      <p style="font-size: 14px; color: var(--color-fg-secondary); line-height: 1.55; max-width: 400px;">
        {mode === 'setup' ? $t('auth.setup.subtitle') : $t('auth.login.subtitle')}
      </p>

      <form
        onsubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        class="w-full mt-2"
        style="display: flex; flex-direction: column; gap: 14px;"
      >
        <label class="block text-left">
          <span
            class="block uppercase"
            style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary); margin-bottom: 8px;"
          >
            {$t('auth.login.username')}
          </span>
          <div
            class="flex items-center"
            style="
              background: rgba(0, 0, 0, 0.3);
              border: 1px solid var(--color-border-soft);
              border-radius: 14px;
              padding: 0 14px;
            "
          >
            <User size={14} strokeWidth={1.5} style="color: var(--color-fg-tertiary); flex-shrink: 0;" />
            <input
              type="text"
              bind:value={username}
              autocomplete="username"
              spellcheck="false"
              required
              class="flex-1 bg-transparent outline-none"
              style="font-size: 14px; color: var(--color-fg-primary); padding: 12px;"
            />
          </div>
        </label>

        <label class="block text-left">
          <span
            class="block uppercase"
            style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary); margin-bottom: 8px;"
          >
            {$t('auth.login.password')}
          </span>
          <div
            class="flex items-center"
            style="
              background: rgba(0, 0, 0, 0.3);
              border: 1px solid var(--color-border-soft);
              border-radius: 14px;
              padding: 0 14px;
            "
          >
            <KeyRound size={14} strokeWidth={1.5} style="color: var(--color-fg-tertiary); flex-shrink: 0;" />
            <input
              type="password"
              bind:value={password}
              autocomplete={mode === 'setup' ? 'new-password' : 'current-password'}
              required
              class="flex-1 bg-transparent outline-none"
              style="font-family: var(--font-mono); font-size: 14px; color: var(--color-fg-primary); padding: 12px; letter-spacing: 0.04em;"
            />
          </div>
          {#if mode === 'setup'}
            <span
              class="block mt-1.5"
              style="font-size: 11px; color: var(--color-fg-tertiary);"
            >
              {$t('auth.setup.password_min')}
            </span>
          {/if}
        </label>

        {#if mode === 'login' && needsTotp}
          <label class="block text-left">
            <span
              class="block uppercase"
              style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary); margin-bottom: 8px;"
            >
              {$t('auth.login.totp')}
            </span>
            <input
              type="text"
              bind:value={totpCode}
              inputmode="numeric"
              pattern="[0-9]*"
              maxlength="6"
              autocomplete="one-time-code"
              class="w-full outline-none"
              style="
                background: rgba(0, 0, 0, 0.3);
                border: 1px solid var(--color-border-soft);
                border-radius: 14px;
                color: var(--color-fg-primary);
                font-family: var(--font-mono);
                font-size: 18px;
                padding: 12px 16px;
                letter-spacing: 0.4em;
                text-align: center;
              "
            />
            <span
              class="block mt-1.5"
              style="font-size: 11px; color: var(--color-fg-tertiary);"
            >
              {$t('auth.login.totp_hint')}
            </span>
          </label>
        {/if}

        {#if mode === 'setup'}
          <label
            class="flex items-start gap-2.5"
            style="
              padding: 12px 14px;
              background: rgba(255, 255, 255, 0.03);
              border: 1px solid var(--color-border-soft);
              border-radius: 12px;
              cursor: pointer;
            "
          >
            <input
              type="checkbox"
              bind:checked={enableTotp}
              style="margin-top: 2px; accent-color: {accent};"
            />
            <span style="font-size: 13px; line-height: 1.4; color: var(--color-fg-primary); text-align: left;">
              <strong style="font-weight: 500;">{$t('auth.setup.totp_label')}</strong><br />
              <span style="font-size: 11.5px; color: var(--color-fg-tertiary);">
                {$t('auth.setup.totp_hint')}
              </span>
            </span>
          </label>
        {/if}

        {#if errorMsg}
          <div
            style="
              padding: 10px 14px;
              background: rgba(255, 69, 58, 0.08);
              border: 1px solid var(--color-status-error);
              border-radius: 10px;
              color: var(--color-status-error);
              font-size: 12px;
              text-align: left;
            "
          >
            {errorMsg}
          </div>
        {/if}

        <button
          type="submit"
          disabled={busy}
          class="inline-flex items-center justify-center gap-2 transition-transform disabled:opacity-50"
          style="
            padding: 13px 24px;
            border-radius: 999px;
            font-size: 12.5px;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            background: {accent};
            color: #0a0a0c;
            border: none;
            box-shadow: 0 8px 28px {accent}40;
            cursor: pointer;
            margin-top: 4px;
          "
        >
          {#if busy}
            <Loader2 size={14} class="animate-spin" />
            {mode === 'setup' ? $t('auth.setup.submitting') : $t('auth.login.submitting')}
          {:else}
            {mode === 'setup' ? $t('auth.setup.submit') : $t('auth.login.submit')}
          {/if}
        </button>
      </form>
    {/if}
  </div>
</section>
{/if}
