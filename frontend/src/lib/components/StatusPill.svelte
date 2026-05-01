<script lang="ts">
  type Status = 'queued' | 'processing' | 'completed' | 'error';

  type Props = {
    status: Status;
    label?: string;
  };

  let { status, label }: Props = $props();

  const labelMap: Record<Status, string> = {
    queued: 'Wartend',
    processing: 'Läuft',
    completed: 'Fertig',
    error: 'Fehler'
  };

  const colorMap: Record<Status, string> = {
    queued: 'var(--color-status-queued)',
    processing: 'var(--color-status-running)',
    completed: 'var(--color-status-done)',
    error: 'var(--color-status-error)'
  };

  const dotPulse = $derived(status === 'processing');
  const text = $derived(label ?? labelMap[status]);
</script>

<span
  class="inline-flex items-center gap-1.5 text-[11px] font-medium tracking-tight tabular-nums whitespace-nowrap"
  style="color: {colorMap[status]};"
>
  <span
    class="inline-block w-1.5 h-1.5 rounded-full"
    class:pulse={dotPulse}
    style="background: {colorMap[status]};"
  ></span>
  {text}
</span>

<style>
  .pulse {
    animation: pulse 1.6s ease-in-out infinite;
  }
  @keyframes pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.35;
    }
  }
</style>
