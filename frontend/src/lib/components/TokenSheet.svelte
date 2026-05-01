<script lang="ts">
  import { Dialog } from 'bits-ui';
  import { apiToken, authChallengeOpen, dismissChallenge } from '$lib/auth';
  import { X } from 'lucide-svelte';

  let value = $state('');
  let saved = $state(false);

  $effect(() => {
    if ($authChallengeOpen) {
      // Vor-befüllen mit aktuellem Token, wenn vorhanden
      value = $apiToken;
      saved = false;
    }
  });

  function save() {
    apiToken.set(value.trim());
    saved = true;
    setTimeout(() => {
      dismissChallenge();
      saved = false;
    }, 600);
  }

  function close() {
    dismissChallenge();
  }
</script>

<Dialog.Root bind:open={$authChallengeOpen}>
  <Dialog.Portal>
    <Dialog.Overlay
      class="fixed inset-0 z-40"
      style="background: rgba(0,0,0,0.55); backdrop-filter: blur(8px);"
    />
    <Dialog.Content
      class="fixed inset-x-0 bottom-0 z-50 mx-auto max-w-xl px-6 pb-6 sheet-anim"
    >
      <div
        class="rounded-[var(--radius-xl)] p-6"
        style="background: var(--color-surface-2); border: 1px solid var(--color-border-firm); backdrop-filter: blur(var(--blur-modal));"
      >
        <header class="flex items-start justify-between mb-5">
          <div>
            <Dialog.Title
              class="text-lg font-semibold tracking-tight"
              style="color: var(--color-fg-primary);"
            >
              Authentifizieren
            </Dialog.Title>
            <Dialog.Description
              class="text-[13px] mt-1"
              style="color: var(--color-fg-secondary);"
            >
              Bearer-Token aus <code style="color: var(--color-accent);">backend/.env</code>
            </Dialog.Description>
          </div>
          <button
            onclick={close}
            aria-label="Schließen"
            class="rounded-md p-1.5 transition-colors"
            style="color: var(--color-fg-secondary);"
            onmouseenter={(e) => (e.currentTarget.style.background = 'var(--color-surface-3)')}
            onmouseleave={(e) => (e.currentTarget.style.background = 'transparent')}
          >
            <X size={16} strokeWidth={1.5} />
          </button>
        </header>

        <label class="block space-y-2">
          <span
            class="text-[12px] font-medium"
            style="color: var(--color-fg-secondary);"
          >
            API-Token
          </span>
          <input
            type="password"
            bind:value
            autocomplete="off"
            spellcheck="false"
            onkeydown={(e) => e.key === 'Enter' && save()}
            class="w-full px-3 py-2.5 rounded-md text-sm font-mono outline-none focus:border-[var(--color-accent)] transition-colors"
            style="background: var(--color-surface-3); border: 1px solid var(--color-border-soft); color: var(--color-fg-primary);"
            placeholder="ttkn_•••••••••••••••••••••••••"
          />
        </label>

        <div class="flex items-center gap-3 mt-5">
          <button
            onclick={save}
            disabled={!value.trim()}
            class="px-4 py-2 rounded-md text-sm font-medium transition-opacity disabled:opacity-40"
            style="background: var(--color-accent); color: #1a1410;"
          >
            {saved ? '✓ Gespeichert' : 'Speichern'}
          </button>
          <span class="text-[12px]" style="color: var(--color-fg-tertiary);">
            ↵ zum Speichern · Esc zum Schließen
          </span>
        </div>
      </div>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>

<style>
  :global(.sheet-anim[data-state='open']) {
    animation: slide-up 0.25s cubic-bezier(0.16, 1, 0.3, 1);
  }
  :global(.sheet-anim[data-state='closed']) {
    animation: slide-down 0.2s ease-in;
  }
  @keyframes slide-up {
    from {
      transform: translateY(100%);
      opacity: 0;
    }
    to {
      transform: translateY(0);
      opacity: 1;
    }
  }
  @keyframes slide-down {
    from {
      transform: translateY(0);
      opacity: 1;
    }
    to {
      transform: translateY(100%);
      opacity: 0;
    }
  }
</style>
