<script lang="ts">
  import '../app.css';
  import { onMount, onDestroy } from 'svelte';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import {
    apiToken,
    accessToken,
    refreshToken,
    currentUser,
    challengeAuth,
    logoutLocal
  } from '$lib/auth';
  import TokenSheet from '$lib/components/TokenSheet.svelte';
  import VinylPuck from '$lib/components/VinylPuck.svelte';
  import FlyingCover from '$lib/components/FlyingCover.svelte';
  import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
  import { flyingCovers, setQueueCount } from '$lib/fly-to-queue';
  import { queueApi, authApi, ApiError } from '$lib/api';
  import { t } from '$lib/i18n';
  import type { StringKey } from '$lib/i18n/strings';
  import { KeyRound, LogOut } from 'lucide-svelte';

  type Tab = { href: string; labelKey: StringKey };
  const tabs: Tab[] = [
    { href: '/', labelKey: 'nav.library' },
    { href: '/queue', labelKey: 'nav.queue' },
    { href: '/import', labelKey: 'nav.import' },
    { href: '/settings', labelKey: 'nav.settings' }
  ];

  function isActive(href: string, current: string): boolean {
    const path = current.replace(base, '') || '/';
    if (href === '/') return path === '/';
    return path === href || path.startsWith(href + '/');
  }

  let { children } = $props();

  const hasToken = $derived(!!$apiToken || !!$accessToken);
  const onLoginRoute = $derived(
    ($page.url.pathname.replace(base, '') || '/') === '/login'
  );

  // Routing-Guard: nach Mount prüfen ob die Session valid ist. Bei 401
  // / Setup-Required → goto /login. Auf der Login-Seite selbst tun wir
  // nix (sonst Redirect-Loop). Bei aktivem Legacy-API-Token überspringen
  // wir den /api/auth/me-Call — der würde nur 401en und unnötig zur
  // Login-Seite redirecten.
  async function guardSession() {
    const path = $page.url.pathname.replace(base, '') || '/';
    if (path === '/login') return;
    // Nur wenn es eigentlich eine Session sein sollte
    if (!$accessToken && $apiToken) return; // Legacy/PAT-Pfad — keine /me-Probe
    try {
      const status = await authApi.setupStatus();
      if (status.setup_required) {
        await goto(`${base}/login`);
        return;
      }
      if (!status.auth_active) return;
      const me = await authApi.me();
      currentUser.set({
        id: me.id,
        username: me.username,
        is_admin: me.is_admin,
        totp_enabled: me.totp_enabled,
        auth_method: me.auth_method
      });
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        logoutLocal();
        await goto(`${base}/login`);
      }
    }
  }

  async function handleLogout() {
    try {
      await authApi.logout($refreshToken || undefined);
    } catch {
      /* network err — local logout always succeeds */
    }
    logoutLocal();
    await goto(`${base}/login`);
  }

  // Queue-Count im Vinyl-Puck unten rechts synchron mit dem Backend halten.
  // Polling-Intervall: 5 s — der Puck ist Status-Indikator, nicht der
  // Queue-Page mit Live-Updates. Längeres Intervall spart Roundtrips.
  // Bei Token-Fehler: Polling stoppen statt 401-Loop.
  let queuePollTimer: ReturnType<typeof setInterval> | null = null;
  async function refreshQueueCount() {
    try {
      const r = await queueApi.list();
      // Aktive Jobs = noch nicht durch (queued/processing) + error (User
      // sieht im Puck "noch nicht erledigt"). Completed werden NICHT
      // gezählt, sonst würde der Counter unendlich wachsen.
      const total =
        (r.status_counts?.queued ?? 0) +
        (r.status_counts?.processing ?? 0) +
        (r.status_counts?.error ?? 0);
      setQueueCount(total);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        if (queuePollTimer) clearInterval(queuePollTimer);
        queuePollTimer = null;
      }
    }
  }
  onMount(() => {
    guardSession();
    refreshQueueCount();
    queuePollTimer = setInterval(refreshQueueCount, 5000);
  });
  onDestroy(() => {
    if (queuePollTimer) clearInterval(queuePollTimer);
  });
</script>

<div class="min-h-screen flex flex-col relative">
  {#if !onLoginRoute}
  <header
    class="sticky top-0 z-30"
    style="
      backdrop-filter: blur(40px) saturate(1.2);
      -webkit-backdrop-filter: blur(40px) saturate(1.2);
      background: rgba(8, 8, 10, 0.55);
      border-bottom: 1px solid var(--color-border-soft);
      transform: translateZ(0);
      will-change: transform;
      contain: layout paint;
    "
  >
    <div class="tonus-header-inner mx-auto max-w-[1180px] h-[54px] flex items-center">
      <!-- Tonus mark + wordmark -->
      <a
        href="{base}/"
        class="tonus-brand flex items-center gap-[9px]"
        style="color: var(--color-fg-primary);"
        aria-label="Tonus — Startseite"
      >
        <span
          class="relative inline-block"
          style="
            width: 22px;
            height: 22px;
            border-radius: 6px;
            background: linear-gradient(135deg, var(--color-accent), oklch(35% 0.15 30));
            flex-shrink: 0;
          "
        >
          <span
            class="absolute"
            style="inset: 30%; border-radius: 50%; background: var(--color-surface-0);"
          ></span>
        </span>
        <span
          class="tonus-wordmark font-semibold"
          style="font-family: var(--font-display); font-size: 18px; letter-spacing: -0.02em;"
        >
          Tonus
        </span>
      </a>

      <!-- Underline tabs -->
      <nav class="tonus-top-nav flex items-center text-[12.5px]">
        {#each tabs as tab}
          {@const active = isActive(tab.href, $page.url.pathname)}
          <a
            href="{base}{tab.href}"
            class="relative transition-colors"
            style="
              padding: 8px 14px;
              color: {active ? 'var(--color-fg-primary)' : 'var(--color-fg-secondary)'};
              letter-spacing: 0.01em;
            "
          >
            {$t(tab.labelKey)}
            {#if active}
              <span
                class="absolute"
                style="
                  left: 14px;
                  right: 14px;
                  bottom: -1px;
                  height: 2px;
                  background: var(--color-accent);
                  border-radius: 2px;
                "
              ></span>
            {/if}
          </a>
        {/each}
      </nav>

      <div class="tonus-header-actions ml-auto flex items-center gap-2">
        {#if $currentUser}
          <span
            class="tonus-user-pill inline-flex items-center gap-1.5"
            style="
              padding: 4px 10px;
              font-size: 11px;
              border-radius: 999px;
              border: 1px solid var(--color-border-soft);
              background: rgba(255, 255, 255, 0.02);
              color: var(--color-fg-secondary);
              flex-shrink: 0;
            "
            title={$t('auth.user_menu.signed_in_as') + ' ' + $currentUser.username}
          >
            <KeyRound size={11} strokeWidth={1.5} />
            <span class="tonus-username">{$currentUser.username}{$currentUser.is_admin ? ' · admin' : ''}</span>
          </span>
          <button
            onclick={handleLogout}
            class="inline-flex items-center gap-1.5 transition-colors"
            style="
              padding: 4px 10px;
              font-size: 11px;
              border-radius: 999px;
              border: 1px solid var(--color-border-soft);
              background: transparent;
              color: var(--color-fg-tertiary);
            "
            aria-label={$t('auth.logout.button')}
          >
            <LogOut size={11} strokeWidth={1.5} />
            {$t('auth.logout.button')}
          </button>
        {:else}
          <!-- Legacy/PAT-Pfad: TokenSheet öffnen für manuelle Eingabe -->
          <button
            onclick={challengeAuth}
            class="inline-flex items-center gap-1.5 transition-colors"
            style="
              padding: 4px 10px;
              font-size: 11px;
              border-radius: 999px;
              border: 1px solid var(--color-border-soft);
              background: rgba(255, 255, 255, 0.02);
              color: {hasToken ? 'var(--color-fg-secondary)' : 'var(--color-status-error)'};
            "
            aria-label="API-Token verwalten"
          >
            <KeyRound size={11} strokeWidth={1.5} />
            {hasToken ? $t('nav.token_active') : $t('nav.token_inactive')}
          </button>
        {/if}
      </div>
    </div>
  </header>
  {/if}

  <main class="flex-1 relative">
    {@render children()}
  </main>

  <!-- Fly-to-Queue: Klone der gerade animierenden Cover. Werden vom
       fly-to-queue.ts-Store via flyingCovers gefüttert; offset-Math nimmt
       document.body als Anchor, deshalb können sie hier ohne eigenen
       relative-Container mounten. -->
  {#each $flyingCovers as cover (cover.id)}
    <FlyingCover
      src={cover.src}
      accent={cover.accent}
      size={cover.size}
      from={cover.from}
      to={cover.to}
    />
  {/each}

  <!-- Vinyl-Puck = bottom-right Floating-Indicator + Queue-Shortcut.
       Hat data-vinyl-puck-Attribut, das fly-to-queue.ts via querySelector
       findet, um die Ziel-Position der Cover-Klone zu berechnen. -->
  {#if hasToken && !onLoginRoute}
    <VinylPuck />
  {/if}

  <TokenSheet />
  <ConfirmDialog />
</div>

<style>
  /* Header-Layout: drei Zonen — Brand, scrollbarer Tab-Bereich, Actions.
     Tabs sollen niemals overflow-versteckt werden, sonst ist Settings
     auf Phone unerreichbar (Issue: User konnte Settings-Tab nicht klicken). */
  .tonus-header-inner {
    padding-left: 28px;
    padding-right: 28px;
    gap: 28px;
  }
  .tonus-top-nav {
    margin-left: 20px;
    flex-shrink: 1;
    min-width: 0;
    overflow-x: auto;
    scrollbar-width: none; /* Firefox */
    -ms-overflow-style: none; /* IE/Edge */
  }
  .tonus-top-nav::-webkit-scrollbar {
    display: none; /* Chrome/Safari */
  }
  .tonus-top-nav > a {
    flex-shrink: 0;
    white-space: nowrap;
  }
  .tonus-header-actions {
    flex-shrink: 0;
  }

  @media (max-width: 640px) {
    .tonus-header-inner {
      padding-left: 12px;
      padding-right: 12px;
      gap: 10px;
    }
    .tonus-top-nav {
      margin-left: 0;
    }
    /* Wordmark-Text auf Phone weglassen — nur Logo-Pille reicht.
       Der ganze a-Tag bleibt klickbar und führt zur Library. */
    .tonus-wordmark {
      display: none;
    }
    /* Username-Text aus User-Pill ausblenden — Icon + KeyRound bleibt
       sichtbar als Indikator, dass eingeloggt. Logout-Button daneben
       weiterhin tappbar. */
    .tonus-username {
      display: none;
    }
  }
</style>

