<script lang="ts">
  import '../app.css';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { apiToken, challengeAuth } from '$lib/auth';
  import TokenSheet from '$lib/components/TokenSheet.svelte';
  import { KeyRound } from 'lucide-svelte';

  type Tab = { href: string; label: string };
  const tabs: Tab[] = [
    { href: '/', label: 'Bibliothek' },
    { href: '/queue', label: 'Warteschlange' },
    { href: '/import', label: 'Import' },
    { href: '/settings', label: 'Einstellungen' }
  ];

  function isActive(href: string, current: string): boolean {
    const path = current.replace(base, '') || '/';
    if (href === '/') return path === '/';
    return path === href || path.startsWith(href + '/');
  }

  let { children } = $props();

  const hasToken = $derived(!!$apiToken);
</script>

<div class="min-h-screen flex flex-col relative">
  <header
    class="sticky top-0 z-30"
    style="
      backdrop-filter: blur(40px) saturate(1.2);
      -webkit-backdrop-filter: blur(40px) saturate(1.2);
      background: rgba(8, 8, 10, 0.55);
      border-bottom: 1px solid var(--color-border-soft);
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
            {tab.label}
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
        {hasToken ? 'Token aktiv' : 'Token fehlt'}
      </button>
    </div>
  </header>

  <main class="flex-1 relative">
    {@render children()}
  </main>

  <TokenSheet />
</div>
