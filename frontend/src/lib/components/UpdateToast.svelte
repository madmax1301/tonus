<script lang="ts">
  /**
   * Meldet eine wartende Service-Worker-Version und lädt auf Tap neu.
   *
   * Ohne das friert die App auf dem Stand ein, der beim ersten Öffnen aktiv
   * war: ein aktiver Worker behält die Kontrolle, bis alle Tabs geschlossen
   * sind — bei einer Homescreen-App passiert das praktisch nie.
   */
  import { onMount } from 'svelte';
  import { t } from '$lib/i18n';

  let waiting = $state<ServiceWorker | null>(null);

  onMount(() => {
    if (!('serviceWorker' in navigator)) return;

    (async () => {
      const reg = await navigator.serviceWorker.getRegistration();
      if (!reg) return;

      if (reg.waiting) waiting = reg.waiting;

      reg.addEventListener('updatefound', () => {
        const incoming = reg.installing;
        if (!incoming) return;
        incoming.addEventListener('statechange', () => {
          // controller vorhanden = es lief bereits eine Version, also ein
          // echtes Update und keine Erstinstallation.
          if (incoming.state === 'installed' && navigator.serviceWorker.controller) {
            waiting = incoming;
          }
        });
      });
    })();
  });

  function applyUpdate() {
    navigator.serviceWorker.addEventListener('controllerchange', () => location.reload(), {
      once: true
    });
    waiting?.postMessage({ type: 'SKIP_WAITING' });
  }
</script>

{#if waiting}
  <div class="tonus-update-toast" role="status">
    <span>{$t('pwa.update_available')}</span>
    <button type="button" onclick={applyUpdate}>{$t('pwa.update_reload')}</button>
  </div>
{/if}

<style>
  .tonus-update-toast {
    position: fixed;
    left: 50%;
    transform: translateX(-50%);
    bottom: calc(var(--safe-bottom) + 16px);
    z-index: 60;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 16px;
    border-radius: var(--radius-lg);
    background: var(--color-surface-2);
    border: 1px solid var(--color-border-firm);
    backdrop-filter: blur(var(--blur-modal));
    -webkit-backdrop-filter: blur(var(--blur-modal));
    color: var(--color-fg-primary);
    font-size: 13px;
  }

  .tonus-update-toast button {
    color: var(--color-accent);
    font-weight: 500;
  }
</style>
