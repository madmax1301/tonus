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
  import { authApi, ApiError } from '$lib/api';
  import { setJwtPair, currentUser } from '$lib/auth';
  import { tint, DEFAULT_HUE } from '$lib/accent';
  import CinemaBackdrop from '$lib/components/CinemaBackdrop.svelte';
  import VinylWithCover from '$lib/components/VinylWithCover.svelte';
  import { t } from '$lib/i18n';
  import { Loader2, KeyRound, ShieldCheck, User } from 'lucide-svelte';

  let mode = $state<'login' | 'setup' | 'totp-qr'>('login');

  // Form state
  let username = $state('');
  let password = $state('');
  let totpCode = $state('');
  let enableTotp = $state(false);
  let needsTotp = $state(false);

  // After-Setup-QR state
  let totpSecret = $state<string | null>(null);
  let totpUri = $state<string | null>(null);

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
          mode = 'totp-qr';
        } else {
          await goto(`${base}/`);
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

  // QR-Code-Image: nutzt das öffentliche google-charts-API durch eine
  // pures Frontend-Crypto-Library wäre besser, aber google-charts ist
  // simpel und der QR-Inhalt (otpauth://) ist nicht sensitive — der
  // User SCANNT ihn ja eh ans Authenticator-Tool weiter.
  // Alternative: backend liefert den QR direkt (qrcode lib ist installiert).
  // Vorerst eine simple data:image-Lösung wäre besser — wir nutzen den
  // standard 200x200 QR.
  const qrImgSrc = $derived(
    totpUri
      ? `https://api.qrserver.com/v1/create-qr-code/?size=220x220&margin=1&data=${encodeURIComponent(totpUri)}`
      : null
  );
</script>

<CinemaBackdrop hue={DEFAULT_HUE} intensity={0.8} />

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
      <button
        type="button"
        onclick={() => goto(`${base}/`)}
        class="mt-2 inline-flex items-center gap-1.5 transition-transform"
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
        <ShieldCheck size={13} strokeWidth={2} />
        {$t('auth.totp_qr.continue')}
      </button>
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
