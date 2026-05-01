<script lang="ts">
  import '../app.css';
  import { page } from '$app/stores';
  import { base } from '$app/paths';

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
            class:active={isActive(tab.href, $page.url.pathname)}
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
      <div class="ml-auto text-[12px]" style="color: var(--color-fg-tertiary);">v0.1</div>
    </div>
  </header>

  <main class="flex-1 mx-auto max-w-7xl w-full px-6 py-10">
    <slot />
  </main>
</div>
