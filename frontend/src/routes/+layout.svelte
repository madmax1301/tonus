<script lang="ts">
  import '../app.css';
  import { onMount, onDestroy } from 'svelte';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { apiToken, challengeAuth } from '$lib/auth';
  import TokenSheet from '$lib/components/TokenSheet.svelte';
  import VinylPuck from '$lib/components/VinylPuck.svelte';
  import FlyingCover from '$lib/components/FlyingCover.svelte';
  import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
  import { flyingCovers, setQueueCount } from '$lib/fly-to-queue';
  import { queueApi, ApiError } from '$lib/api';
  import { t } from '$lib/i18n';
  import type { StringKey } from '$lib/i18n/strings';
  import { KeyRound } from 'lucide-svelte';

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

  const hasToken = $derived(!!$apiToken);

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
    refreshQueueCount();
    queuePollTimer = setInterval(refreshQueueCount, 5000);
  });
  onDestroy(() => {
    if (queuePollTimer) clearInterval(queuePollTimer);
  });
</script>

<div class="min-h-screen flex flex-col relative">
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
    <div class="mx-auto max-w-[1180px] px-7 h-[54px] flex items-center gap-7">
      <!-- Tonus mark + wordmark -->
      <a
        href="{base}/"
        class="flex items-center gap-[9px]"
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
          "
        >
          <span
            class="absolute"
            style="inset: 30%; border-radius: 50%; background: var(--color-surface-0);"
          ></span>
        </span>
        <span
          class="font-semibold"
          style="font-family: var(--font-display); font-size: 18px; letter-spacing: -0.02em;"
        >
          Tonus
        </span>
      </a>

      <!-- Underline tabs -->
      <nav class="flex items-center text-[12.5px] ml-5">
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

      <!-- Token-pill (right-aligned) -->
      <button
        onclick={challengeAuth}
        class="ml-auto inline-flex items-center gap-1.5 transition-colors"
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
    </div>
  </header>

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
  {#if hasToken}
    <VinylPuck />
  {/if}

  <TokenSheet />
  <ConfirmDialog />
</div>
