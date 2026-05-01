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

<div class="min-h-screen flex flex-col">
  <header
    class="sticky top-0 z-30 backdrop-blur-[24px]"
    style="background: var(--color-surface-1); border-bottom: 1px solid var(--color-border-soft);"
  >
    <div class="mx-auto max-w-7xl px-6 h-14 flex items-center gap-8">
      <a
        href="{base}/"
        class="font-medium tracking-tight text-[15px]"
        style="color: var(--color-fg-primary);"
        aria-label="Tonus — Startseite"
      >
        Tonus
      </a>
      <nav class="flex items-center gap-1 text-[13px]">
        {#each tabs as tab}
          <a
            href="{base}{tab.href}"
            class="px-3 py-1.5 rounded-md transition-colors"
            style="color: {isActive(tab.href, $page.url.pathname)
              ? 'var(--color-fg-primary)'
              : 'var(--color-fg-secondary)'}; background: {isActive(tab.href, $page.url.pathname)
              ? 'var(--color-surface-3)'
              : 'transparent'};"
          >
            {tab.label}
          </a>
        {/each}
      </nav>
      <div class="ml-auto flex items-center gap-3">
        <button
          onclick={challengeAuth}
          class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[12px] transition-colors"
          style="color: {hasToken
            ? 'var(--color-fg-secondary)'
            : 'var(--color-status-error)'}; background: var(--color-surface-3);"
          aria-label="API-Token verwalten"
        >
          <KeyRound size={13} strokeWidth={1.5} />
          {hasToken ? 'Token' : 'Token fehlt'}
        </button>
        <span class="text-[12px]" style="color: var(--color-fg-tertiary);">v0.1</span>
      </div>
    </div>
  </header>

  <main class="flex-1 mx-auto max-w-7xl w-full px-6 py-10">
    {@render children()}
  </main>

  <TokenSheet />
</div>
