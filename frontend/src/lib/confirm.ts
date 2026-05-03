/**
 * Cinematic Confirm-Dialog — ersetzt native browser-confirm().
 *
 * Verwendung:
 *
 *   import { showConfirm } from '$lib/confirm';
 *   const ok = await showConfirm({
 *     title: 'Queue leeren',
 *     message: 'Wirklich die komplette Queue leeren? Laufende Downloads bleiben erhalten.',
 *     confirmLabel: 'Leeren',
 *     destructive: true,
 *   });
 *   if (ok) doIt();
 *
 * Dialog wird im Layout via ConfirmDialog-Komponente gerendert. Promise
 * resolved bei Bestätigen (true) oder Abbrechen / Esc / Backdrop-Click
 * (false). Nur ein Dialog gleichzeitig — neue Aufrufe ersetzen den
 * vorherigen.
 */
import { writable } from 'svelte/store';

export type ConfirmOptions = {
  title: string;
  message?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Confirm-Button auf rot statt gold (Lösch-/Reset-Aktionen). */
  destructive?: boolean;
};

type DialogState = {
  options: ConfirmOptions;
  resolve: (ok: boolean) => void;
} | null;

export const dialogState = writable<DialogState>(null);

export function showConfirm(options: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    dialogState.update((current) => {
      // Falls noch ein offener Dialog existiert: sauber als false
      // resolven, damit der vorherige Aufrufer nicht hängen bleibt.
      if (current) current.resolve(false);
      return { options, resolve };
    });
  });
}
