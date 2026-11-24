import { resolve } from '$app/paths';

// SvelteKit 3 hat `base` aus $app/paths entfernt. resolve('/') liefert
// `${base}/`, also ist das hier genau der alte Wert (leer oder z. B. `/tonus`).
export const base = resolve('/').slice(0, -1);
