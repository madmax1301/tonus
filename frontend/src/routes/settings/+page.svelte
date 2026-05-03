<script lang="ts">
  import { onMount } from 'svelte';
  import { defaultProvider, defaultLocation, defaultFormat, defaultQuality } from '$lib/preferences';
  import {
    providersApi,
    systemApi,
    authApi,
    ApiError,
    type MetadataProvidersResponse,
    type FormatsInfo,
    type HealthResponse,
    type AuthUser,
    type Pat,
    type PatCreateResponse,
    type BannedIp,
    type ManagedUser
  } from '$lib/api';
  import CinemaBackdrop from '$lib/components/CinemaBackdrop.svelte';
  import VinylWithCover from '$lib/components/VinylWithCover.svelte';
  import { tint, DEFAULT_HUE } from '$lib/accent';
  import { t, lang, type Lang } from '$lib/i18n';
  import { showConfirm } from '$lib/confirm';
  import {
    Server,
    Library,
    Trash2,
    Check,
    Sliders,
    Globe,
    ShieldCheck,
    KeyRound,
    Copy,
    AlertTriangle,
    Ban,
    Users as UsersIcon,
    UserPlus,
    UserMinus,
    UserCog
  } from 'lucide-svelte';

  type Section = 'defaults' | 'backend' | 'language' | 'pats' | 'security' | 'bans' | 'users';
  let section = $state<Section>('language');

  // Backend-Info (read-only)
  let providers = $state<MetadataProvidersResponse | null>(null);
  let formats = $state<FormatsInfo | null>(null);
  let health = $state<HealthResponse | null>(null);
  let infoError = $state<string | null>(null);

  // Current user — Quelle für totp_enabled und Re-Render nach TOTP-Mutation.
  let me = $state<AuthUser | null>(null);

  // ── PATs (Personal Access Tokens) ──────────────────────────────────
  // Liste lädt einmalig beim Section-Open + nach jedem Create/Revoke.
  let pats = $state<Pat[]>([]);
  let patsLoaded = $state(false);
  let patsError = $state<string | null>(null);

  // Create-Modal-State.
  let createOpen = $state(false);
  let createName = $state('');
  // Expiry-Slider: 7 / 30 / 90 / null (= unbegrenzt). Default 30d ist
  // ein vernünftiger Mittelweg — kurz genug um Stale-Tokens zu verhindern,
  // lang genug um nicht ständig zu re-issuen.
  let createExpiry = $state<7 | 30 | 90 | null>(30);
  let createBusy = $state(false);
  let createError = $state<string | null>(null);

  // Plain-Token Once-Display: nach erfolgreichem Create wird der Plain-
  // Token in shownToken gehalten und EINMALIG gerendert. Schließen ⇒ raus.
  let shownToken = $state<PatCreateResponse | null>(null);
  let shownCopied = $state(false);

  // Revoke-State: ID des Tokens der gerade widerrufen wird (Spinner).
  let revokingId = $state<number | null>(null);

  async function loadPats() {
    patsError = null;
    try {
      const res = await authApi.patsList();
      pats = res.pats;
      patsLoaded = true;
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        // Legacy/Setup-Auth — keine User-bezogenen PATs möglich.
        pats = [];
        patsLoaded = true;
      } else {
        patsError = $t('settings.pats.create.error_generic');
      }
    }
  }

  async function submitCreatePat() {
    const name = createName.trim();
    if (!name || name.length > 64) {
      createError = $t('settings.pats.create.error_name');
      return;
    }
    createBusy = true;
    createError = null;
    try {
      const res = await authApi.patsCreate(name, createExpiry);
      // Plain-Token JETZT zeigen, dann Modal schließen + Liste reloaden.
      shownToken = res;
      shownCopied = false;
      createOpen = false;
      createName = '';
      createExpiry = 30;
      await loadPats();
    } catch (err) {
      createError =
        err instanceof ApiError && err.status === 400
          ? err.message
          : $t('settings.pats.create.error_generic');
    } finally {
      createBusy = false;
    }
  }

  async function copyShownToken() {
    if (!shownToken) return;
    try {
      await navigator.clipboard.writeText(shownToken.token);
      shownCopied = true;
      setTimeout(() => (shownCopied = false), 1500);
    } catch {
      // Clipboard kann in iframes/insecure-contexts blocked sein — kein
      // großes Drama, User kann den Token immer noch manuell selektieren.
    }
  }

  function dismissShownToken() {
    shownToken = null;
    shownCopied = false;
  }

  async function revokePat(pat: Pat) {
    const ok = await showConfirm({
      title: $t('settings.pats.revoke.confirm_title'),
      message: $t('settings.pats.revoke.confirm_message'),
      confirmLabel: $t('settings.pats.revoke.confirm_label'),
      destructive: true
    });
    if (!ok) return;
    revokingId = pat.id;
    try {
      await authApi.patsRevoke(pat.id);
      await loadPats();
    } catch {
      patsError = $t('settings.pats.revoke.error_generic');
    } finally {
      revokingId = null;
    }
  }

  // Relative-Zeit-Formatierung — kompakt, ohne externe Lib. Negative Werte
  // (in der Vergangenheit) zeigen wir als "vor X" via deutscher Lokalisierung
  // im strings-Format ({when} = "3 Tagen"), positive ("läuft in 5 Tagen ab")
  // sind ohne Vorzeichen.
  function formatRelative(diffMs: number): string {
    const abs = Math.abs(diffMs);
    const min = 60 * 1000;
    const hour = 60 * min;
    const day = 24 * hour;
    if (abs < hour) return $lang === 'de' ? `${Math.max(1, Math.round(abs / min))} min` : `${Math.max(1, Math.round(abs / min))} min`;
    if (abs < day) return $lang === 'de' ? `${Math.round(abs / hour)} h` : `${Math.round(abs / hour)} h`;
    const days = Math.round(abs / day);
    return $lang === 'de' ? `${days} Tage${days === 1 ? '' : 'n'}` : `${days} day${days === 1 ? '' : 's'}`;
  }

  // Lazy-Load: PATs/Bans/Users nur fetchen, wenn der User die Section
  // tatsächlich öffnet. Spart Roundtrips bei Settings-Open für 80%-Use-Cases.
  $effect(() => {
    if (section === 'pats' && !patsLoaded) {
      loadPats();
    }
    if (section === 'bans' && !bansLoaded) {
      loadBans();
    }
    if (section === 'users' && !usersLoaded) {
      loadUsers();
    }
  });

  // ── Section-Permissions ─────────────────────────────────────────
  // adminOnly-Sections: nur sichtbar wenn me.is_admin. Frontend-Filter ist
  // UX, der harte Check sitzt im Backend (require_admin Dependency).
  type TabDef = {
    id: Section;
    key:
      | 'settings.section.defaults'
      | 'settings.section.backend'
      | 'settings.section.language'
      | 'settings.section.pats'
      | 'settings.section.security'
      | 'settings.section.bans'
      | 'settings.section.users';
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    icon: any;
    adminOnly: boolean;
  };
  const allTabs: TabDef[] = [
    { id: 'defaults', key: 'settings.section.defaults', icon: Sliders, adminOnly: true },
    { id: 'backend', key: 'settings.section.backend', icon: Server, adminOnly: true },
    { id: 'language', key: 'settings.section.language', icon: Globe, adminOnly: false },
    { id: 'pats', key: 'settings.section.pats', icon: KeyRound, adminOnly: true },
    { id: 'users', key: 'settings.section.users', icon: UsersIcon, adminOnly: true },
    { id: 'security', key: 'settings.section.security', icon: ShieldCheck, adminOnly: false },
    { id: 'bans', key: 'settings.section.bans', icon: Ban, adminOnly: true }
  ];
  const visibleTabs = $derived(
    me === null
      ? allTabs.filter((t) => !t.adminOnly) // bevor me da ist: konservativ User-View
      : me.is_admin
        ? allTabs
        : allTabs.filter((t) => !t.adminOnly)
  );

  // Permission-Redirect: wenn me geladen ist und die aktuelle Section ist
  // admin-only aber User ist kein Admin → switch zur ersten visible Section.
  // Default-Mount ist 'language' (immer visible), aber nach Tab-Wechsel über
  // URL-State / SessionStorage könnte ein User auf einer admin-only Section
  // landen.
  $effect(() => {
    if (me && !me.is_admin) {
      const currentTab = allTabs.find((t) => t.id === section);
      if (currentTab?.adminOnly) {
        section = 'language';
      }
    }
  });

  // ── Banned-IPs (Brute-Force-Schutz, Admin-only) ────────────────────
  let bans = $state<BannedIp[]>([]);
  let bansLoaded = $state(false);
  let bansError = $state<string | null>(null);
  let unbanningIp = $state<string | null>(null);

  async function loadBans() {
    bansError = null;
    try {
      const res = await authApi.bansList();
      bans = res.banned;
      bansLoaded = true;
    } catch (err) {
      // 403 für non-admin: Section ist eh ausgeblendet, also stillschweigend
      // ignorieren. Andere Fehler dem User anzeigen.
      if (!(err instanceof ApiError && err.status === 403)) {
        bansError = $t('settings.bans.error_load');
      }
      bansLoaded = true;
    }
  }

  async function unbanIp(ip: string) {
    const ok = await showConfirm({
      title: $t('settings.bans.unban_confirm_title'),
      message: $t('settings.bans.unban_confirm_message', { ip }),
      confirmLabel: $t('settings.bans.unban_confirm_label'),
      destructive: false
    });
    if (!ok) return;
    unbanningIp = ip;
    try {
      await authApi.bansUnban(ip);
      await loadBans();
    } catch {
      bansError = $t('settings.bans.error_unban');
    } finally {
      unbanningIp = null;
    }
  }

  // ── User-Management (Admin-only) ────────────────────────────────
  let users = $state<ManagedUser[]>([]);
  let usersLoaded = $state(false);
  let usersError = $state<string | null>(null);

  // Create-Modal
  let userCreateOpen = $state(false);
  let userCreateUsername = $state('');
  let userCreatePassword = $state('');
  let userCreateIsAdmin = $state(false);
  let userCreateBusy = $state(false);
  let userCreateError = $state<string | null>(null);

  // Reset-Password-Modal
  let resetTarget = $state<ManagedUser | null>(null);
  let resetPasswordValue = $state('');
  let resetBusy = $state(false);
  let resetError = $state<string | null>(null);

  // Per-row pending actions
  let userActionPending = $state<{ id: number; kind: 'toggle' | 'delete' } | null>(null);

  async function loadUsers() {
    usersError = null;
    try {
      const res = await authApi.usersList();
      users = res.users;
      usersLoaded = true;
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 403)) {
        usersError = $t('settings.users.error_load');
      }
      usersLoaded = true;
    }
  }

  async function submitCreateUser() {
    const name = userCreateUsername.trim();
    if (!name) {
      userCreateError = $t('settings.users.create.error_username');
      return;
    }
    if (userCreatePassword.length < 8) {
      userCreateError = $t('settings.users.create.error_password');
      return;
    }
    userCreateBusy = true;
    userCreateError = null;
    try {
      await authApi.usersCreate(name, userCreatePassword, userCreateIsAdmin);
      userCreateOpen = false;
      userCreateUsername = '';
      userCreatePassword = '';
      userCreateIsAdmin = false;
      await loadUsers();
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        const detail = (err.message || '').toLowerCase();
        userCreateError = detail.includes('password')
          ? $t('settings.users.create.error_password')
          : $t('settings.users.create.error_username');
      } else {
        userCreateError = $t('settings.users.create.error_generic');
      }
    } finally {
      userCreateBusy = false;
    }
  }

  async function toggleUserAdmin(u: ManagedUser) {
    const willBeAdmin = !u.is_admin;
    const ok = await showConfirm({
      title: $t('settings.users.toggle_admin.confirm_title'),
      message: willBeAdmin
        ? $t('settings.users.toggle_admin.confirm_promote', { username: u.username })
        : $t('settings.users.toggle_admin.confirm_demote', { username: u.username }),
      confirmLabel: willBeAdmin
        ? $t('settings.users.actions.toggle_admin_promote')
        : $t('settings.users.actions.toggle_admin_demote'),
      destructive: !willBeAdmin
    });
    if (!ok) return;
    userActionPending = { id: u.id, kind: 'toggle' };
    try {
      await authApi.usersPatch(u.id, { is_admin: willBeAdmin });
      await loadUsers();
      // Wenn der User sich selbst demoted hat, muss me reloaded werden,
      // damit die admin-only Tabs korrekt verschwinden.
      if (me && u.id === me.id) {
        await reloadMe();
      }
    } catch (err) {
      usersError =
        err instanceof ApiError && err.status === 400
          ? $t('settings.users.error_last_admin')
          : $t('settings.users.error_action');
    } finally {
      userActionPending = null;
    }
  }

  async function deleteManagedUser(u: ManagedUser) {
    if (me && u.id === me.id) {
      usersError = $t('settings.users.error_self_delete');
      return;
    }
    const ok = await showConfirm({
      title: $t('settings.users.delete.confirm_title'),
      message: $t('settings.users.delete.confirm_message', { username: u.username }),
      confirmLabel: $t('settings.users.delete.confirm_label'),
      destructive: true
    });
    if (!ok) return;
    userActionPending = { id: u.id, kind: 'delete' };
    try {
      await authApi.usersDelete(u.id);
      await loadUsers();
    } catch (err) {
      usersError =
        err instanceof ApiError && err.status === 400
          ? $t('settings.users.error_last_admin')
          : $t('settings.users.error_action');
    } finally {
      userActionPending = null;
    }
  }

  function openResetPassword(u: ManagedUser) {
    resetTarget = u;
    resetPasswordValue = '';
    resetError = null;
  }

  async function submitResetPassword() {
    if (!resetTarget) return;
    if (resetPasswordValue.length < 8) {
      resetError = $t('settings.users.create.error_password');
      return;
    }
    resetBusy = true;
    resetError = null;
    try {
      await authApi.usersPatch(resetTarget.id, { password: resetPasswordValue });
      resetTarget = null;
      resetPasswordValue = '';
    } catch {
      resetError = $t('settings.users.error_action');
    } finally {
      resetBusy = false;
    }
  }

  // TOTP-Setup-Wizard-State (öffnet inline in der Security-Section).
  // initData != null ⇒ Wizard läuft (QR + Code-Eingabe sichtbar).
  // Verify-First-Pattern: Secret nur in dieser Variable, nicht in DB,
  // bis confirmTotpSetup() den Code erfolgreich gegen das Backend prüft.
  let totpInit = $state<{ secret: string; uri: string; qr_data_url: string } | null>(null);
  let totpInitBusy = $state(false);
  let totpInitCode = $state('');
  let totpInitError = $state<string | null>(null);
  let totpInitConfirming = $state(false);

  // TOTP-Disable-Form-State.
  let totpDisablePassword = $state('');
  let totpDisableCode = $state('');
  let totpDisableBusy = $state(false);
  let totpDisableError = $state<string | null>(null);
  let totpJustDisabled = $state(false);

  async function reloadMe() {
    try {
      me = await authApi.me();
    } catch {
      // 401 wird von api.ts global gehandhabt
    }
  }

  async function startTotpSetup() {
    totpInitBusy = true;
    totpInitError = null;
    totpInitCode = '';
    try {
      totpInit = await authApi.totpInit();
    } catch (err) {
      totpInitError =
        err instanceof ApiError && err.status === 409
          ? err.message
          : $t('settings.security.setup.error_generic');
    } finally {
      totpInitBusy = false;
    }
  }

  function cancelTotpSetup() {
    totpInit = null;
    totpInitCode = '';
    totpInitError = null;
  }

  async function confirmTotpSetup() {
    if (!totpInit) return;
    const code = totpInitCode.replace(/\s/g, '');
    if (!/^\d{6}$/.test(code)) {
      totpInitError = $t('settings.security.setup.error_code');
      return;
    }
    totpInitConfirming = true;
    totpInitError = null;
    try {
      await authApi.totpConfirm(totpInit.secret, code);
      totpInit = null;
      totpInitCode = '';
      await reloadMe();
    } catch (err) {
      totpInitError =
        err instanceof ApiError && err.status === 401
          ? $t('settings.security.setup.error_code')
          : $t('settings.security.setup.error_generic');
    } finally {
      totpInitConfirming = false;
    }
  }

  async function disableTotp() {
    const ok = await showConfirm({
      title: $t('settings.security.disable.confirm_title'),
      message: $t('settings.security.disable.confirm_message'),
      confirmLabel: $t('settings.security.disable.confirm_label'),
      destructive: true
    });
    if (!ok) return;

    if (!totpDisablePassword || (me?.totp_enabled && !/^\d{6}$/.test(totpDisableCode.replace(/\s/g, '')))) {
      totpDisableError = me?.totp_enabled
        ? $t('settings.security.disable.error_code')
        : $t('settings.security.disable.error_password');
      return;
    }

    totpDisableBusy = true;
    totpDisableError = null;
    try {
      await authApi.totpDisable(
        totpDisablePassword,
        me?.totp_enabled ? totpDisableCode.replace(/\s/g, '') : undefined
      );
      totpDisablePassword = '';
      totpDisableCode = '';
      totpJustDisabled = true;
      setTimeout(() => (totpJustDisabled = false), 1800);
      await reloadMe();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        const detail = (err.message || '').toLowerCase();
        totpDisableError = detail.includes('totp') || detail.includes('2fa')
          ? $t('settings.security.disable.error_code')
          : $t('settings.security.disable.error_password');
      } else {
        totpDisableError = $t('settings.security.disable.error_generic');
      }
    } finally {
      totpDisableBusy = false;
    }
  }

  onMount(async () => {
    try {
      [providers, formats, health, me] = await Promise.all([
        providersApi.list().catch(() => null),
        systemApi.formats().catch(() => null),
        systemApi.health().catch(() => null),
        authApi.me().catch(() => null)
      ]);
    } catch (err) {
      if (!(err instanceof ApiError && (err.status === 401 || err.status === 403))) {
        infoError = err instanceof Error ? err.message : 'Backend not reachable';
      }
    }
  });

  const effectiveProvider = $derived($defaultProvider || providers?.default || '');
  const accent = $derived(tint(DEFAULT_HUE));

  function pillStyle(active: boolean): string {
    if (active) {
      return `background: ${accent}; color: #1a1410; border: 1px solid transparent; box-shadow: 0 4px 12px rgba(200, 169, 106, 0.25);`;
    }
    return 'background: rgba(255, 255, 255, 0.03); color: var(--color-fg-secondary); border: 1px solid var(--color-border-soft);';
  }
</script>

<CinemaBackdrop hue={DEFAULT_HUE} />

<section class="relative z-10 mx-auto max-w-[1180px] w-full" style="padding: 40px 36px 50px;">
  <!-- ─── Editorial Hero ───────────────────────────────────── -->
  <div
    class="grid items-center"
    style="grid-template-columns: 1.3fr 1fr; gap: 48px; margin-bottom: 48px;"
  >
    <div>
      <div
        class="font-semibold uppercase"
        style="font-size: 11px; letter-spacing: 0.24em; color: {accent}; margin-bottom: 14px;"
      >
        {$t('settings.eyebrow')}
      </div>
      <h1
        class="font-semibold m-0"
        style="font-family: var(--font-display); font-size: 48px; font-weight: 600; line-height: 0.95; letter-spacing: -0.035em;"
      >
        {$t('settings.title.before')}<br />
        <em style="color: {accent}; font-weight: 400; font-style: italic;"
          >{$t('settings.title.italic')}</em
        >{$t('settings.title.after')}
      </h1>
      <p
        style="font-size: 14px; color: var(--color-fg-secondary); max-width: 440px; margin-top: 18px; line-height: 1.6;"
      >
        {$t('settings.description.prefix')}
        <code
          style="font-family: var(--font-mono); color: var(--color-fg-secondary); font-size: 12.5px;"
          >{$t('settings.description.env')}</code
        >
        {$t('settings.description.suffix')}
      </p>
    </div>
    <div class="flex justify-center">
      <VinylWithCover
        src={null}
        alt=""
        artist="Setup"
        year={effectiveProvider || ''}
        size={240}
        spinning={false}
      />
    </div>
  </div>

  <!-- ─── Section-Strip ─────────────────────────────────────── -->
  <div
    class="flex items-center"
    style="gap: 24px; font-size: 13px; margin-bottom: 24px; border-bottom: 1px solid var(--color-border-soft); padding-bottom: 14px; flex-wrap: wrap;"
  >
    {#each visibleTabs as s}
      {@const active = section === s.id}
      <button
        onclick={() => (section = s.id)}
        class="relative inline-flex items-center gap-1.5 transition-colors"
        style="color: {active ? 'var(--color-fg-primary)' : 'var(--color-fg-secondary)'}; font-weight: {active ? 500 : 400}; padding-bottom: 14px; margin-bottom: -14px; border-bottom: 2px solid {active ? accent : 'transparent'};"
      >
        <svelte:component this={s.icon} size={13} strokeWidth={1.5} />
        {$t(s.key)}
      </button>
    {/each}
  </div>

  <!-- ─── Section: Defaults ───────────────────────────────── -->
  {#if section === 'defaults'}
    <div class="tonus-fadein space-y-5">
      <!-- Provider -->
      <div
        style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 28px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
      >
        <div class="flex items-baseline justify-between gap-3 mb-3 flex-wrap">
          <div>
            <div
              class="font-semibold uppercase"
              style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary);"
            >
              {$t('settings.defaults.provider.eyebrow')}
            </div>
            <div
              class="mt-1"
              style="font-family: var(--font-display); font-size: 18px; font-weight: 500; color: var(--color-fg-primary);"
            >
              {$t('settings.defaults.provider.title')}
            </div>
          </div>
          <div
            class="text-[11px] tabular-nums"
            style="color: var(--color-fg-tertiary); font-family: var(--font-mono);"
          >
            {$t('settings.defaults.provider.active')}
            <span style="color: {accent};">{effectiveProvider || '—'}</span>
          </div>
        </div>
        <div class="flex items-center gap-1.5 flex-wrap">
          <button
            onclick={() => defaultProvider.set('')}
            class="px-3 py-1.5 rounded-full text-[12px] transition-all"
            style={pillStyle(!$defaultProvider)}
          >
            {$t('settings.defaults.provider.backend_default')}
            {providers?.default ? `(${providers.default})` : ''}
          </button>
          {#if providers}
            <!-- Konfigurierte Provider: voll auswählbar als Pill. -->
            {#each providers.providers.filter((p) => p.configured) as p}
              <button
                onclick={() => defaultProvider.set(p.id)}
                class="px-3 py-1.5 rounded-full text-[12px] transition-all"
                style={pillStyle($defaultProvider === p.id)}
              >
                {p.label}
              </button>
            {/each}
            <!-- Nicht-konfigurierte Provider: angedeutete Pill mit
                 dashed-border, disabled, Tooltip erklärt was fehlt.
                 Damit sieht der User auch Spotify auch wenn er noch
                 nicht eingerichtet ist — als Hinweis "geht auch, brauchst
                 nur Client-ID + Secret in backend/.env". -->
            {#each providers.providers.filter((p) => !p.configured) as p}
              <button
                disabled
                title="In backend/.env konfigurieren ({p.id.toUpperCase()}_CLIENT_ID + _CLIENT_SECRET) und Backend neu starten"
                class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[12px] cursor-not-allowed"
                style="
                  background: rgba(255, 255, 255, 0.02);
                  color: var(--color-fg-tertiary);
                  border: 1px dashed rgba(255, 255, 255, 0.18);
                  opacity: 0.7;
                "
              >
                {p.label}
                <span
                  style="
                    font-size: 9.5px;
                    letter-spacing: 0.12em;
                    text-transform: uppercase;
                    color: var(--color-fg-tertiary);
                    border: 1px solid var(--color-border-soft);
                    padding: 1px 6px;
                    border-radius: 999px;
                    margin-left: 2px;
                  "
                >
                  setup
                </span>
              </button>
            {/each}
          {/if}
        </div>
        {#if providers && providers.providers.some((p) => !p.configured)}
          <p
            class="mt-2"
            style="
              font-size: 11px;
              color: var(--color-fg-tertiary);
              line-height: 1.6;
              max-width: 560px;
            "
          >
            Provider mit dashed-Outline brauchen
            <code style="font-family: var(--font-mono); color: {accent}; font-size: 10.5px;"
              >&lt;PROVIDER&gt;_CLIENT_ID</code
            >
            +
            <code style="font-family: var(--font-mono); color: {accent}; font-size: 10.5px;"
              >&lt;PROVIDER&gt;_CLIENT_SECRET</code
            >
            in
            <code style="font-family: var(--font-mono); color: var(--color-fg-secondary); font-size: 10.5px;"
              >backend/.env</code
            > und einen Backend-Restart. Tonus liest die Werte beim Start.
          </p>
        {/if}
      </div>

      <!-- Location -->
      <div
        style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 28px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
      >
        <div
          class="font-semibold uppercase"
          style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary);"
        >
          {$t('settings.defaults.location.eyebrow')}
        </div>
        <div
          class="mt-1 mb-3"
          style="font-family: var(--font-display); font-size: 18px; font-weight: 500; color: var(--color-fg-primary);"
        >
          {$t('settings.defaults.location.title')}
        </div>
        <div class="flex items-center gap-1.5 flex-wrap">
          {#each [{ id: 'navidrome' as const, key: 'settings.defaults.location.navidrome' as const }, { id: 'local' as const, key: 'settings.defaults.location.local' as const }] as opt}
            <button
              onclick={() => defaultLocation.set(opt.id)}
              class="px-3 py-1.5 rounded-full text-[12px] transition-all"
              style={pillStyle($defaultLocation === opt.id)}
            >
              {$t(opt.key)}
            </button>
          {/each}
        </div>
      </div>

      <!-- Format & Quality -->
      <div
        style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 28px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
      >
        <div
          class="font-semibold uppercase"
          style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary);"
        >
          {$t('settings.defaults.audio.eyebrow')}
        </div>
        <div
          class="mt-1 mb-3"
          style="font-family: var(--font-display); font-size: 18px; font-weight: 500; color: var(--color-fg-primary);"
        >
          {$t('settings.defaults.audio.title')}
        </div>

        <div
          class="text-[11px] uppercase mb-2"
          style="color: var(--color-fg-tertiary); letter-spacing: 0.12em;"
        >
          {$t('settings.defaults.audio.format_label')}
        </div>
        <div class="flex items-center gap-1.5 flex-wrap mb-4">
          <button
            onclick={() => defaultFormat.set('')}
            class="px-3 py-1.5 rounded-full text-[12px] transition-all"
            style={pillStyle(!$defaultFormat)}
          >
            {$t('settings.defaults.provider.backend_default')}
            {formats?.default_format ? `(${formats.default_format})` : ''}
          </button>
          {#if formats}
            {#each formats.formats as f}
              <button
                onclick={() => defaultFormat.set(f.value)}
                title={f.description ?? ''}
                class="px-3 py-1.5 rounded-full text-[12px] transition-all"
                style={pillStyle($defaultFormat === f.value)}
              >
                {f.label}
              </button>
            {/each}
          {/if}
        </div>

        <div
          class="text-[11px] uppercase mb-2"
          style="color: var(--color-fg-tertiary); letter-spacing: 0.12em;"
        >
          {$t('settings.defaults.audio.bitrate_label')}
        </div>
        <div class="flex items-center gap-1.5 flex-wrap">
          <button
            onclick={() => defaultQuality.set('')}
            class="px-3 py-1.5 rounded-full text-[12px] transition-all"
            style={pillStyle(!$defaultQuality)}
          >
            {$t('settings.defaults.provider.backend_default')}
            {formats?.default_quality ? `(${formats.default_quality})` : ''}
          </button>
          {#if formats}
            {#each formats.qualities as q}
              <button
                onclick={() => defaultQuality.set(q.value)}
                title={q.description ?? ''}
                class="px-3 py-1.5 rounded-full text-[12px] transition-all"
                style={pillStyle($defaultQuality === q.value)}
              >
                {q.label}
              </button>
            {/each}
          {/if}
        </div>
        <p class="mt-3 text-[11px]" style="color: var(--color-fg-tertiary); line-height: 1.55;">
          {$t('settings.defaults.audio.note')}
        </p>
      </div>
    </div>
  {/if}

  <!-- ─── Section: Backend ────────────────────────────────── -->
  {#if section === 'backend'}
    <div
      class="tonus-fadein"
      style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 32px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
    >
      {#if infoError}
        <div class="text-[13px]" style="color: var(--color-status-error);">{infoError}</div>
      {:else}
        <div
          class="font-semibold uppercase"
          style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary); margin-bottom: 6px;"
        >
          {$t('settings.backend.eyebrow')}
        </div>
        <div
          style="font-family: var(--font-display); font-size: 22px; font-weight: 500; letter-spacing: -0.015em; color: var(--color-fg-primary); margin-bottom: 22px;"
        >
          {$t('settings.backend.title')}
        </div>

        <dl class="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-5 text-[13px]">
          <div>
            <dt
              class="text-[10.5px] uppercase tracking-widest"
              style="color: var(--color-fg-tertiary); letter-spacing: 0.18em;"
            >
              {$t('settings.backend.field.default_provider')}
            </dt>
            <dd
              class="mt-1"
              style="color: var(--color-fg-primary); font-family: var(--font-mono); font-size: 14px;"
            >
              {providers?.default ?? '—'}
            </dd>
          </div>
          <div>
            <dt
              class="text-[10.5px] uppercase tracking-widest"
              style="color: var(--color-fg-tertiary); letter-spacing: 0.18em;"
            >
              {$t('settings.backend.field.configured_providers')}
            </dt>
            <dd class="mt-1" style="color: var(--color-fg-primary);">
              {providers?.providers
                .filter((p) => p.configured)
                .map((p) => p.label)
                .join(', ') ?? '—'}
              {#if providers && providers.providers.some((p) => !p.configured)}
                <div class="mt-1 text-[11px]" style="color: var(--color-fg-tertiary);">
                  {$t('settings.backend.field.missing_providers')}
                  {providers.providers
                    .filter((p) => !p.configured)
                    .map((p) => p.label)
                    .join(', ')}
                </div>
              {/if}
            </dd>
          </div>
          <div>
            <dt
              class="text-[10.5px] uppercase tracking-widest"
              style="color: var(--color-fg-tertiary); letter-spacing: 0.18em;"
            >
              {$t('settings.backend.field.default_format')}
            </dt>
            <dd
              class="mt-1"
              style="color: var(--color-fg-primary); font-family: var(--font-mono); font-size: 14px;"
            >
              {formats?.default_format ?? '—'}
              {#if formats?.default_quality}
                <span style="color: var(--color-fg-tertiary);"> · {formats.default_quality}</span>
              {/if}
            </dd>
          </div>
          <div>
            <dt
              class="text-[10.5px] uppercase tracking-widest"
              style="color: var(--color-fg-tertiary); letter-spacing: 0.18em;"
            >
              {$t('settings.backend.field.available_formats')}
            </dt>
            <dd class="mt-1" style="color: var(--color-fg-primary);">
              {formats?.formats.map((f) => f.label).join(', ') ?? '—'}
            </dd>
          </div>
          {#if health?.navidrome_path}
            <div class="sm:col-span-2">
              <dt
                class="text-[10.5px] uppercase tracking-widest"
                style="color: var(--color-fg-tertiary); letter-spacing: 0.18em;"
              >
                {$t('settings.backend.field.navidrome_path')}
              </dt>
              <dd
                class="mt-1"
                style="color: var(--color-fg-primary); font-family: var(--font-mono); font-size: 12.5px; word-break: break-all;"
              >
                {health.navidrome_path}
              </dd>
            </div>
          {/if}
        </dl>
      {/if}

      {#if health?.navidrome_libraries && health.navidrome_libraries.length > 0}
        <div class="mt-6 pt-6" style="border-top: 1px solid var(--color-border-soft);">
          <div class="flex items-center gap-2 mb-3">
            <Library size={14} strokeWidth={1.5} style="color: {accent};" />
            <div
              class="font-semibold uppercase"
              style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary);"
            >
              {$t('settings.backend.libraries.title', {
                count: health.navidrome_libraries.length
              })}
            </div>
          </div>
          <div class="space-y-1.5">
            {#each health.navidrome_libraries as lib}
              <div
                class="flex items-center justify-between gap-3 px-4 py-2.5 rounded-md"
                style="background: rgba(0, 0, 0, 0.25); border: 1px solid var(--color-border-soft);"
              >
                <span class="font-medium text-[13px]" style="color: var(--color-fg-primary);"
                  >{lib.label ?? '—'}</span
                >
                <span
                  class="text-[11.5px] truncate"
                  style="color: var(--color-fg-tertiary); font-family: var(--font-mono);"
                >
                  {lib.path}
                </span>
              </div>
            {/each}
          </div>
        </div>
      {/if}
    </div>
  {/if}

  <!-- ─── Section: Language ──────────────────────────────── -->
  {#if section === 'language'}
    <div
      class="tonus-fadein"
      style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 32px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
    >
      <div
        class="font-semibold uppercase"
        style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary); margin-bottom: 6px;"
      >
        {$t('settings.language.eyebrow')}
      </div>
      <div
        style="font-family: var(--font-display); font-size: 22px; font-weight: 500; letter-spacing: -0.015em; color: var(--color-fg-primary); margin-bottom: 18px;"
      >
        {$t('settings.language.title')}
      </div>
      <p
        style="font-size: 13px; color: var(--color-fg-secondary); line-height: 1.55; max-width: 560px; margin-bottom: 22px;"
      >
        {$t('settings.language.description')}
      </p>
      <div class="flex items-center gap-1.5 flex-wrap">
        {#each [{ id: 'de' as Lang, key: 'settings.language.de' as const }, { id: 'en' as Lang, key: 'settings.language.en' as const }] as opt}
          <button
            onclick={() => lang.set(opt.id)}
            class="px-4 py-2 rounded-full text-[12px] transition-all"
            style={pillStyle($lang === opt.id)}
          >
            {$t(opt.key)}
          </button>
        {/each}
      </div>
    </div>
  {/if}

  <!-- ─── Section: PATs (API-Tokens) ─────────────────────── -->
  {#if section === 'pats'}
    <div
      class="tonus-fadein"
      style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 32px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
    >
      <div
        class="font-semibold uppercase"
        style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary); margin-bottom: 6px;"
      >
        {$t('settings.pats.eyebrow')}
      </div>
      <div
        class="flex items-baseline justify-between flex-wrap gap-3"
        style="margin-bottom: 18px;"
      >
        <div
          style="font-family: var(--font-display); font-size: 22px; font-weight: 500; letter-spacing: -0.015em; color: var(--color-fg-primary);"
        >
          {$t('settings.pats.title')}
        </div>
        {#if me?.auth_method !== 'legacy' && me?.auth_method !== 'setup'}
          <button
            type="button"
            onclick={() => {
              createOpen = true;
              createError = null;
              createName = '';
              createExpiry = 30;
            }}
            class="inline-flex items-center gap-1.5 transition-opacity"
            style="background: {accent}; color: #1a1410; padding: 10px 18px; border-radius: 999px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; box-shadow: 0 8px 20px rgba(200, 169, 106, 0.25);"
          >
            <KeyRound size={13} strokeWidth={2} />
            {$t('settings.pats.create.button')}
          </button>
        {/if}
      </div>
      <p
        style="font-size: 13px; color: var(--color-fg-secondary); margin-bottom: 22px; line-height: 1.55; max-width: 620px;"
      >
        {$t('settings.pats.body')}
      </p>

      {#if me?.auth_method === 'legacy' || me?.auth_method === 'setup'}
        <!-- Legacy/Setup-Auth hat keinen User-Account → keine PATs möglich.
             Hinweis-Card statt Form. -->
        <div
          style="background: rgba(248, 195, 113, 0.08); border: 1px dashed rgba(248, 195, 113, 0.35); border-radius: 14px; padding: 16px 20px; color: var(--color-fg-secondary); font-size: 13px; line-height: 1.55;"
        >
          <AlertTriangle size={14} strokeWidth={2} style="display: inline; vertical-align: -2px; color: rgba(248, 195, 113, 0.95); margin-right: 6px;" />
          {$t('settings.pats.legacy_note')}
        </div>
      {:else if shownToken}
        <!-- Plain-Token Once-Display: Backend speichert nur Hash, also gibt
             es keinen Weg den Klartext später nochmal zu sehen. Inline-Card
             mit Copy-Button + Warnung. -->
        <div
          style="background: rgba(34, 197, 94, 0.06); border: 1px solid rgba(34, 197, 94, 0.32); border-radius: 16px; padding: 22px 24px; margin-bottom: 22px;"
        >
          <div
            class="font-semibold uppercase"
            style="font-size: 11px; letter-spacing: 0.2em; color: rgba(134, 239, 172, 0.95); margin-bottom: 8px;"
          >
            {$t('settings.pats.shown.title')} · {shownToken.name}
          </div>
          <p
            style="font-size: 12.5px; color: var(--color-fg-secondary); line-height: 1.5; margin: 0 0 12px;"
          >
            <AlertTriangle size={12} strokeWidth={2} style="display: inline; vertical-align: -2px; color: rgba(248, 195, 113, 0.95); margin-right: 4px;" />
            {$t('settings.pats.shown.warning')}
          </p>
          <div
            class="flex items-center gap-3"
            style="background: rgba(0, 0, 0, 0.4); border: 1px solid var(--color-border-soft); border-radius: 12px; padding: 12px 16px; font-family: var(--font-mono); font-size: 13px; color: var(--color-fg-primary); word-break: break-all;"
          >
            <code style="flex: 1; letter-spacing: 0.04em;">{shownToken.token}</code>
            <button
              type="button"
              onclick={copyShownToken}
              class="inline-flex items-center gap-1.5 transition-opacity flex-shrink-0"
              style="background: rgba(255, 255, 255, 0.06); color: var(--color-fg-primary); padding: 6px 14px; border-radius: 999px; font-size: 11px; font-weight: 500; letter-spacing: 0.04em; text-transform: uppercase; border: 1px solid var(--color-border-soft);"
            >
              {#if shownCopied}
                <Check size={12} strokeWidth={2} />
                {$t('settings.pats.shown.copied')}
              {:else}
                <Copy size={12} strokeWidth={2} />
                {$t('settings.pats.shown.copy')}
              {/if}
            </button>
          </div>
          <button
            type="button"
            onclick={dismissShownToken}
            class="inline-flex items-center transition-colors"
            style="margin-top: 14px; background: transparent; color: var(--color-fg-secondary); padding: 8px 16px; border-radius: 999px; font-size: 12px; font-weight: 500; letter-spacing: 0.02em; border: 1px solid var(--color-border-soft);"
          >
            {$t('settings.pats.shown.close')}
          </button>
        </div>
      {/if}

      {#if createOpen && me?.auth_method !== 'legacy' && me?.auth_method !== 'setup'}
        <!-- Create-Form: Name + Expiry. Inline statt Modal — passt zum
             Glass-Card-Stil und erspart einen Layer. -->
        <form
          onsubmit={(e) => {
            e.preventDefault();
            submitCreatePat();
          }}
          class="flex flex-col gap-3"
          style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--color-border-soft); border-radius: 16px; padding: 22px 24px; margin-bottom: 22px; max-width: 560px;"
        >
          <div
            class="font-semibold uppercase"
            style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary); margin-bottom: 4px;"
          >
            {$t('settings.pats.create.modal_title')}
          </div>
          <label class="flex flex-col gap-1.5" for="pat-create-name">
            <span
              class="uppercase"
              style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary);"
            >
              {$t('settings.pats.create.name_label')}
            </span>
            <input
              id="pat-create-name"
              type="text"
              maxlength="64"
              spellcheck="false"
              bind:value={createName}
              placeholder={$t('settings.pats.create.name_placeholder')}
              class="outline-none"
              style="background: rgba(0, 0, 0, 0.3); border: 1px solid var(--color-border-soft); border-radius: 14px; color: var(--color-fg-primary); font-size: 13px; padding: 12px 16px;"
            />
          </label>
          <div class="flex flex-col gap-1.5">
            <span
              class="uppercase"
              style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary);"
            >
              {$t('settings.pats.create.expiry_label')}
            </span>
            <div class="flex items-center gap-1.5 flex-wrap">
              {#each [
                { val: 7 as const, key: 'settings.pats.create.expiry_7d' as const },
                { val: 30 as const, key: 'settings.pats.create.expiry_30d' as const },
                { val: 90 as const, key: 'settings.pats.create.expiry_90d' as const },
                { val: null, key: 'settings.pats.create.expiry_never' as const }
              ] as opt}
                <button
                  type="button"
                  onclick={() => (createExpiry = opt.val)}
                  class="px-4 py-2 rounded-full text-[12px] transition-all"
                  style={pillStyle(createExpiry === opt.val)}
                >
                  {$t(opt.key)}
                </button>
              {/each}
            </div>
          </div>
          {#if createError}
            <p style="font-size: 12px; color: #f87171; margin: 0;">{createError}</p>
          {/if}
          <div class="flex items-center gap-3" style="margin-top: 4px;">
            <button
              type="submit"
              disabled={createBusy}
              class="inline-flex items-center transition-opacity"
              style="background: {accent}; color: #1a1410; padding: 11px 22px; border-radius: 999px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; box-shadow: 0 8px 20px rgba(200, 169, 106, 0.25); opacity: {createBusy ? 0.6 : 1}; cursor: {createBusy ? 'wait' : 'pointer'};"
            >
              {createBusy
                ? $t('settings.pats.create.submitting')
                : $t('settings.pats.create.submit')}
            </button>
            <button
              type="button"
              onclick={() => {
                createOpen = false;
                createError = null;
              }}
              class="inline-flex items-center transition-colors"
              style="background: transparent; color: var(--color-fg-secondary); padding: 11px 22px; border-radius: 999px; font-size: 12px; font-weight: 500; letter-spacing: 0.02em; border: 1px solid var(--color-border-soft);"
            >
              {$t('settings.pats.create.cancel')}
            </button>
          </div>
        </form>
      {/if}

      {#if me?.auth_method !== 'legacy' && me?.auth_method !== 'setup'}
        <!-- Liste der existierenden PATs. Plain-Token gibt's hier nicht mehr —
             nur prefix für Identifikation, plus created/last-used/expires. -->
        {#if patsError}
          <p style="font-size: 12px; color: #f87171; margin: 0 0 12px;">{patsError}</p>
        {/if}
        {#if !patsLoaded}
          <p style="font-size: 12px; color: var(--color-fg-tertiary); margin: 0;">…</p>
        {:else if pats.length === 0}
          <p style="font-size: 13px; color: var(--color-fg-tertiary); margin: 0;">
            {$t('settings.pats.list.empty')}
          </p>
        {:else}
          <div
            class="font-semibold uppercase"
            style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary); margin-bottom: 10px;"
          >
            {$t('settings.pats.list.title')}
          </div>
          <ul class="flex flex-col" style="gap: 10px; margin: 0; padding: 0; list-style: none;">
            {#each pats as p (p.id)}
              {@const now = Date.now()}
              {@const expired = p.expires_at_ms && p.expires_at_ms < now}
              {@const expiresIn = p.expires_at_ms ? p.expires_at_ms - now : null}
              {@const createdAgo = now - p.created_at_ms}
              {@const lastUsedAgo = p.last_used_at_ms ? now - p.last_used_at_ms : null}
              <li
                class="flex items-center justify-between gap-4 flex-wrap"
                style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--color-border-soft); border-radius: 14px; padding: 14px 18px;"
              >
                <div class="flex flex-col" style="gap: 4px; min-width: 0; flex: 1;">
                  <div class="flex items-center gap-2 flex-wrap">
                    <span
                      style="font-size: 14px; font-weight: 500; color: var(--color-fg-primary);"
                      >{p.name}</span
                    >
                    <code
                      style="font-family: var(--font-mono); font-size: 11.5px; color: var(--color-fg-tertiary); letter-spacing: 0.04em;"
                      >{p.prefix}…</code
                    >
                  </div>
                  <div
                    class="flex items-center gap-2 flex-wrap"
                    style="font-size: 11.5px; color: var(--color-fg-secondary);"
                  >
                    <span>
                      {#if expired}
                        <span style="color: #f87171;">{$t('settings.pats.list.expired')}</span>
                      {:else if p.expires_at_ms && expiresIn !== null}
                        {$t('settings.pats.list.expires_in', {
                          when: formatRelative(expiresIn)
                        })}
                      {:else}
                        {$t('settings.pats.list.no_expiry')}
                      {/if}
                    </span>
                    <span style="color: var(--color-fg-tertiary);">·</span>
                    <span>
                      {#if lastUsedAgo !== null}
                        {$t('settings.pats.list.last_used', {
                          when: formatRelative(lastUsedAgo)
                        })}
                      {:else}
                        {$t('settings.pats.list.never_used')}
                      {/if}
                    </span>
                  </div>
                </div>
                <button
                  type="button"
                  disabled={revokingId === p.id}
                  onclick={() => revokePat(p)}
                  class="inline-flex items-center gap-1.5 transition-opacity flex-shrink-0"
                  style="background: rgba(248, 113, 113, 0.12); color: #fca5a5; padding: 8px 16px; border-radius: 999px; font-size: 11px; font-weight: 500; letter-spacing: 0.04em; text-transform: uppercase; border: 1px solid rgba(248, 113, 113, 0.28); opacity: {revokingId === p.id ? 0.6 : 1};"
                >
                  <Trash2 size={12} strokeWidth={2} />
                  {revokingId === p.id
                    ? $t('settings.pats.revoke.busy')
                    : $t('settings.pats.list.revoke')}
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      {/if}
    </div>
  {/if}

  <!-- ─── Section: Users (Admin-only) ─────────────────────── -->
  {#if section === 'users' && me?.is_admin}
    <div
      class="tonus-fadein"
      style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 32px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
    >
      <div
        class="font-semibold uppercase"
        style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary); margin-bottom: 6px;"
      >
        {$t('settings.users.eyebrow')}
      </div>
      <div
        class="flex items-baseline justify-between flex-wrap gap-3"
        style="margin-bottom: 18px;"
      >
        <div
          style="font-family: var(--font-display); font-size: 22px; font-weight: 500; letter-spacing: -0.015em; color: var(--color-fg-primary);"
        >
          {$t('settings.users.title')}
        </div>
        <button
          type="button"
          onclick={() => {
            userCreateOpen = true;
            userCreateError = null;
            userCreateUsername = '';
            userCreatePassword = '';
            userCreateIsAdmin = false;
          }}
          class="inline-flex items-center gap-1.5 transition-opacity"
          style="background: {accent}; color: #1a1410; padding: 10px 18px; border-radius: 999px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; box-shadow: 0 8px 20px rgba(200, 169, 106, 0.25);"
        >
          <UserPlus size={13} strokeWidth={2} />
          {$t('settings.users.create.button')}
        </button>
      </div>
      <p
        style="font-size: 13px; color: var(--color-fg-secondary); margin-bottom: 22px; line-height: 1.55; max-width: 620px;"
      >
        {$t('settings.users.body')}
      </p>

      {#if userCreateOpen}
        <form
          onsubmit={(e) => {
            e.preventDefault();
            submitCreateUser();
          }}
          class="flex flex-col gap-3"
          style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--color-border-soft); border-radius: 16px; padding: 22px 24px; margin-bottom: 22px; max-width: 560px;"
        >
          <div
            class="font-semibold uppercase"
            style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary); margin-bottom: 4px;"
          >
            {$t('settings.users.create.modal_title')}
          </div>
          <label class="flex flex-col gap-1.5" for="usr-create-username">
            <span
              class="uppercase"
              style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary);"
            >
              {$t('settings.users.create.username_label')}
            </span>
            <input
              id="usr-create-username"
              type="text"
              maxlength="64"
              autocomplete="off"
              spellcheck="false"
              bind:value={userCreateUsername}
              placeholder={$t('settings.users.create.username_placeholder')}
              class="outline-none"
              style="background: rgba(0, 0, 0, 0.3); border: 1px solid var(--color-border-soft); border-radius: 14px; color: var(--color-fg-primary); font-family: var(--font-mono); font-size: 13px; padding: 12px 16px;"
            />
          </label>
          <label class="flex flex-col gap-1.5" for="usr-create-password">
            <span
              class="uppercase"
              style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary);"
            >
              {$t('settings.users.create.password_label')}
            </span>
            <input
              id="usr-create-password"
              type="password"
              autocomplete="new-password"
              spellcheck="false"
              bind:value={userCreatePassword}
              class="outline-none"
              style="background: rgba(0, 0, 0, 0.3); border: 1px solid var(--color-border-soft); border-radius: 14px; color: var(--color-fg-primary); font-family: var(--font-mono); font-size: 13px; padding: 12px 16px; letter-spacing: 0.04em;"
            />
            <span style="font-size: 11.5px; color: var(--color-fg-tertiary); line-height: 1.5;">
              {$t('settings.users.create.password_hint')}
            </span>
          </label>
          <label
            class="inline-flex items-center gap-2"
            style="font-size: 13px; color: var(--color-fg-secondary);"
          >
            <input
              type="checkbox"
              bind:checked={userCreateIsAdmin}
              style="accent-color: {accent}; width: 14px; height: 14px;"
            />
            {$t('settings.users.create.is_admin_label')}
          </label>
          {#if userCreateError}
            <p style="font-size: 12px; color: #f87171; margin: 0;">{userCreateError}</p>
          {/if}
          <div class="flex items-center gap-3" style="margin-top: 4px;">
            <button
              type="submit"
              disabled={userCreateBusy}
              class="inline-flex items-center transition-opacity"
              style="background: {accent}; color: #1a1410; padding: 11px 22px; border-radius: 999px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; box-shadow: 0 8px 20px rgba(200, 169, 106, 0.25); opacity: {userCreateBusy ? 0.6 : 1}; cursor: {userCreateBusy ? 'wait' : 'pointer'};"
            >
              {userCreateBusy
                ? $t('settings.users.create.submitting')
                : $t('settings.users.create.submit')}
            </button>
            <button
              type="button"
              onclick={() => (userCreateOpen = false)}
              class="inline-flex items-center transition-colors"
              style="background: transparent; color: var(--color-fg-secondary); padding: 11px 22px; border-radius: 999px; font-size: 12px; font-weight: 500; letter-spacing: 0.02em; border: 1px solid var(--color-border-soft);"
            >
              {$t('settings.users.create.cancel')}
            </button>
          </div>
        </form>
      {/if}

      {#if resetTarget}
        <form
          onsubmit={(e) => {
            e.preventDefault();
            submitResetPassword();
          }}
          class="flex flex-col gap-3"
          style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--color-border-soft); border-radius: 16px; padding: 22px 24px; margin-bottom: 22px; max-width: 560px;"
        >
          <div
            class="font-semibold uppercase"
            style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary); margin-bottom: 4px;"
          >
            {$t('settings.users.reset_password.modal_title', { username: resetTarget.username })}
          </div>
          <label class="flex flex-col gap-1.5" for="usr-reset-pw">
            <span
              class="uppercase"
              style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary);"
            >
              {$t('settings.users.reset_password.label')}
            </span>
            <input
              id="usr-reset-pw"
              type="password"
              autocomplete="new-password"
              spellcheck="false"
              bind:value={resetPasswordValue}
              class="outline-none"
              style="background: rgba(0, 0, 0, 0.3); border: 1px solid var(--color-border-soft); border-radius: 14px; color: var(--color-fg-primary); font-family: var(--font-mono); font-size: 13px; padding: 12px 16px; letter-spacing: 0.04em;"
            />
          </label>
          {#if resetError}
            <p style="font-size: 12px; color: #f87171; margin: 0;">{resetError}</p>
          {/if}
          <div class="flex items-center gap-3" style="margin-top: 4px;">
            <button
              type="submit"
              disabled={resetBusy}
              class="inline-flex items-center transition-opacity"
              style="background: {accent}; color: #1a1410; padding: 11px 22px; border-radius: 999px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; box-shadow: 0 8px 20px rgba(200, 169, 106, 0.25); opacity: {resetBusy ? 0.6 : 1}; cursor: {resetBusy ? 'wait' : 'pointer'};"
            >
              {resetBusy
                ? $t('settings.users.reset_password.submitting')
                : $t('settings.users.reset_password.submit')}
            </button>
            <button
              type="button"
              onclick={() => (resetTarget = null)}
              class="inline-flex items-center transition-colors"
              style="background: transparent; color: var(--color-fg-secondary); padding: 11px 22px; border-radius: 999px; font-size: 12px; font-weight: 500; letter-spacing: 0.02em; border: 1px solid var(--color-border-soft);"
            >
              {$t('settings.users.reset_password.cancel')}
            </button>
          </div>
        </form>
      {/if}

      {#if usersError}
        <p style="font-size: 12px; color: #f87171; margin: 0 0 12px;">{usersError}</p>
      {/if}
      {#if !usersLoaded}
        <p style="font-size: 12px; color: var(--color-fg-tertiary); margin: 0;">…</p>
      {:else if users.length === 0}
        <p style="font-size: 13px; color: var(--color-fg-tertiary); margin: 0;">
          {$t('settings.users.list.empty')}
        </p>
      {:else}
        <div
          class="font-semibold uppercase"
          style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary); margin-bottom: 10px;"
        >
          {$t('settings.users.list.title')}
        </div>
        <ul class="flex flex-col" style="gap: 10px; margin: 0; padding: 0; list-style: none;">
          {#each users as u (u.id)}
            {@const now = Date.now()}
            {@const createdAgo = now - u.created_at_ms}
            {@const lastLoginAgo = u.last_login_at_ms ? now - u.last_login_at_ms : null}
            {@const isSelf = me?.id === u.id}
            <li
              class="flex items-center justify-between gap-4 flex-wrap"
              style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--color-border-soft); border-radius: 14px; padding: 14px 18px;"
            >
              <div class="flex flex-col" style="gap: 4px; min-width: 0; flex: 1;">
                <div class="flex items-center gap-2 flex-wrap">
                  <span
                    style="font-size: 14px; font-weight: 500; color: var(--color-fg-primary);"
                    >{u.username}</span
                  >
                  {#if isSelf}
                    <span
                      class="uppercase"
                      style="font-size: 9.5px; letter-spacing: 0.16em; color: var(--color-fg-tertiary); background: rgba(255,255,255,0.04); border: 1px solid var(--color-border-soft); padding: 2px 7px; border-radius: 999px;"
                      >{$t('settings.users.list.you')}</span
                    >
                  {/if}
                  {#if u.is_admin}
                    <span
                      class="uppercase"
                      style="font-size: 9.5px; letter-spacing: 0.16em; color: {accent}; background: {accent}1a; border: 1px solid {accent}55; padding: 2px 7px; border-radius: 999px; font-weight: 600;"
                      >{$t('settings.users.list.admin_badge')}</span
                    >
                  {/if}
                  {#if u.totp_enabled}
                    <span
                      class="uppercase inline-flex items-center gap-1"
                      style="font-size: 9.5px; letter-spacing: 0.16em; color: rgba(134, 239, 172, 0.95); background: rgba(34, 197, 94, 0.08); border: 1px solid rgba(34, 197, 94, 0.3); padding: 2px 7px; border-radius: 999px;"
                    >
                      <ShieldCheck size={10} strokeWidth={2.2} />
                      {$t('settings.users.list.totp_badge')}
                    </span>
                  {/if}
                </div>
                <div
                  class="flex items-center gap-2 flex-wrap"
                  style="font-size: 11.5px; color: var(--color-fg-secondary);"
                >
                  <span>{$t('settings.users.list.created', { when: formatRelative(createdAgo) })}</span>
                  <span style="color: var(--color-fg-tertiary);">·</span>
                  {#if lastLoginAgo !== null}
                    <span>{$t('settings.users.list.last_login', { when: formatRelative(lastLoginAgo) })}</span>
                  {:else}
                    <span>{$t('settings.users.list.never_logged_in')}</span>
                  {/if}
                </div>
              </div>
              <div class="flex items-center gap-1.5 flex-wrap flex-shrink-0">
                <button
                  type="button"
                  disabled={userActionPending?.id === u.id && userActionPending.kind === 'toggle'}
                  onclick={() => toggleUserAdmin(u)}
                  title={u.is_admin
                    ? $t('settings.users.actions.toggle_admin_demote')
                    : $t('settings.users.actions.toggle_admin_promote')}
                  class="inline-flex items-center gap-1.5 transition-opacity"
                  style="background: rgba(255, 255, 255, 0.06); color: var(--color-fg-primary); padding: 7px 12px; border-radius: 999px; font-size: 11px; font-weight: 500; letter-spacing: 0.04em; text-transform: uppercase; border: 1px solid var(--color-border-soft); opacity: {userActionPending?.id === u.id ? 0.6 : 1};"
                >
                  {#if u.is_admin}
                    <UserMinus size={11} strokeWidth={2} />
                  {:else}
                    <UserCog size={11} strokeWidth={2} />
                  {/if}
                </button>
                <button
                  type="button"
                  onclick={() => openResetPassword(u)}
                  title={$t('settings.users.actions.reset_password')}
                  class="inline-flex items-center gap-1.5 transition-opacity"
                  style="background: rgba(255, 255, 255, 0.06); color: var(--color-fg-primary); padding: 7px 12px; border-radius: 999px; font-size: 11px; font-weight: 500; letter-spacing: 0.04em; text-transform: uppercase; border: 1px solid var(--color-border-soft);"
                >
                  <KeyRound size={11} strokeWidth={2} />
                </button>
                {#if !isSelf}
                  <button
                    type="button"
                    disabled={userActionPending?.id === u.id && userActionPending.kind === 'delete'}
                    onclick={() => deleteManagedUser(u)}
                    title={$t('settings.users.actions.delete')}
                    class="inline-flex items-center gap-1.5 transition-opacity"
                    style="background: rgba(248, 113, 113, 0.12); color: #fca5a5; padding: 7px 12px; border-radius: 999px; font-size: 11px; font-weight: 500; letter-spacing: 0.04em; text-transform: uppercase; border: 1px solid rgba(248, 113, 113, 0.28); opacity: {userActionPending?.id === u.id ? 0.6 : 1};"
                  >
                    <Trash2 size={11} strokeWidth={2} />
                  </button>
                {/if}
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}

  <!-- ─── Section: Security (TOTP) ───────────────────────── -->
  {#if section === 'security'}
    <div
      class="tonus-fadein"
      style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 32px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
    >
      <div
        class="font-semibold uppercase"
        style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary); margin-bottom: 6px;"
      >
        {$t('settings.security.eyebrow')}
      </div>
      <div
        style="font-family: var(--font-display); font-size: 22px; font-weight: 500; letter-spacing: -0.015em; color: var(--color-fg-primary); margin-bottom: 18px;"
      >
        {me?.totp_enabled
          ? $t('settings.security.title.active')
          : $t('settings.security.title.inactive')}
      </div>
      <p
        style="font-size: 13px; color: var(--color-fg-secondary); margin-bottom: 22px; line-height: 1.55; max-width: 560px;"
      >
        {me?.totp_enabled
          ? $t('settings.security.body.active')
          : $t('settings.security.body.inactive')}
      </p>

      {#if me?.totp_enabled}
        <!-- Disable-Form: Password (+ aktueller Code wenn aktiv) als Re-Verify
             gegen Session-Hijack — ein gestohlener Access-Token allein soll
             2FA nicht aushebeln können. -->
        <form
          onsubmit={(e) => {
            e.preventDefault();
            disableTotp();
          }}
          class="flex flex-col gap-3"
          style="max-width: 460px;"
        >
          <label class="flex flex-col gap-1.5" for="totp-disable-password">
            <span
              class="uppercase"
              style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary);"
            >
              {$t('settings.security.disable.password_label')}
            </span>
            <input
              id="totp-disable-password"
              name="current-password"
              type="password"
              autocomplete="current-password"
              spellcheck="false"
              bind:value={totpDisablePassword}
              class="outline-none"
              style="background: rgba(0, 0, 0, 0.3); border: 1px solid var(--color-border-soft); border-radius: 14px; color: var(--color-fg-primary); font-family: var(--font-mono); font-size: 13px; padding: 12px 16px; letter-spacing: 0.04em;"
            />
          </label>
          <label class="flex flex-col gap-1.5" for="totp-disable-code">
            <span
              class="uppercase"
              style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary);"
            >
              {$t('settings.security.disable.code_label')}
            </span>
            <input
              id="totp-disable-code"
              type="text"
              inputmode="numeric"
              pattern="[0-9 ]*"
              autocomplete="one-time-code"
              maxlength="7"
              spellcheck="false"
              bind:value={totpDisableCode}
              class="outline-none"
              style="background: rgba(0, 0, 0, 0.3); border: 1px solid var(--color-border-soft); border-radius: 14px; color: var(--color-fg-primary); font-family: var(--font-mono); font-size: 16px; padding: 12px 16px; letter-spacing: 0.18em; text-align: center;"
              placeholder="000 000"
            />
          </label>
          {#if totpDisableError}
            <p style="font-size: 12px; color: #f87171; margin: 0;">{totpDisableError}</p>
          {/if}
          {#if totpJustDisabled}
            <p style="font-size: 12px; color: var(--color-fg-secondary); margin: 0;">
              <Check size={12} strokeWidth={2} /> {$t('common.saved')}
            </p>
          {/if}
          <div class="flex items-center gap-3" style="margin-top: 4px;">
            <button
              type="submit"
              disabled={totpDisableBusy}
              class="inline-flex items-center gap-1.5 transition-opacity"
              style="background: rgba(248, 113, 113, 0.18); color: #fca5a5; padding: 12px 22px; border-radius: 999px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; border: 1px solid rgba(248, 113, 113, 0.35); opacity: {totpDisableBusy ? 0.6 : 1}; cursor: {totpDisableBusy ? 'wait' : 'pointer'};"
            >
              {totpDisableBusy
                ? $t('settings.security.disable.submitting')
                : $t('settings.security.disable.button')}
            </button>
          </div>
        </form>
      {:else if totpInit}
        <!-- Setup-Wizard: server-rendered QR (data-URL, kein externer Service)
             + Code-Input. Erst nach erfolgreichem Confirm wird Secret persistiert
             — Verify-First-Activate-Second-Pattern. -->
        <div class="flex flex-col items-start" style="gap: 18px; max-width: 460px;">
          <p
            style="font-size: 13px; color: var(--color-fg-secondary); line-height: 1.55; margin: 0;"
          >
            {$t('settings.security.setup.qr_body')}
          </p>
          <div
            style="background: rgba(255, 255, 255, 0.95); padding: 14px; border-radius: 14px; box-shadow: 0 16px 40px rgba(0, 0, 0, 0.4);"
          >
            <img src={totpInit.qr_data_url} alt="TOTP QR" width="220" height="220" />
          </div>
          <div
            class="w-full"
            style="background: rgba(255, 255, 255, 0.04); border: 1px solid var(--color-border-soft); border-radius: 12px; padding: 14px 16px;"
          >
            <div
              class="uppercase"
              style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary); margin-bottom: 6px;"
            >
              {$t('settings.security.setup.secret_label')}
            </div>
            <code
              style="font-family: var(--font-mono); font-size: 13px; color: var(--color-fg-primary); word-break: break-all; letter-spacing: 0.06em;"
              >{totpInit.secret}</code
            >
          </div>
          <form
            onsubmit={(e) => {
              e.preventDefault();
              confirmTotpSetup();
            }}
            class="flex flex-col gap-3 w-full"
          >
            <label class="flex flex-col gap-1.5" for="totp-init-code">
              <span
                class="uppercase"
                style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary);"
              >
                {$t('settings.security.setup.code_label')}
              </span>
              <input
                id="totp-init-code"
                type="text"
                inputmode="numeric"
                pattern="[0-9 ]*"
                autocomplete="one-time-code"
                maxlength="7"
                spellcheck="false"
                bind:value={totpInitCode}
                class="outline-none"
                style="background: rgba(0, 0, 0, 0.3); border: 1px solid var(--color-border-soft); border-radius: 14px; color: var(--color-fg-primary); font-family: var(--font-mono); font-size: 16px; padding: 12px 16px; letter-spacing: 0.18em; text-align: center;"
                placeholder="000 000"
              />
            </label>
            {#if totpInitError}
              <p style="font-size: 12px; color: #f87171; margin: 0;">{totpInitError}</p>
            {/if}
            <div class="flex items-center gap-3">
              <button
                type="submit"
                disabled={totpInitConfirming}
                class="inline-flex items-center gap-1.5 transition-opacity"
                style="background: {accent}; color: #1a1410; padding: 12px 22px; border-radius: 999px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; box-shadow: 0 8px 20px rgba(200, 169, 106, 0.25); opacity: {totpInitConfirming ? 0.6 : 1}; cursor: {totpInitConfirming ? 'wait' : 'pointer'};"
              >
                {totpInitConfirming
                  ? $t('settings.security.setup.confirming')
                  : $t('settings.security.setup.confirm')}
              </button>
              <button
                type="button"
                onclick={cancelTotpSetup}
                class="inline-flex items-center transition-colors"
                style="background: transparent; color: var(--color-fg-secondary); padding: 12px 22px; border-radius: 999px; font-size: 12px; font-weight: 500; letter-spacing: 0.02em; border: 1px solid var(--color-border-soft);"
              >
                {$t('settings.security.setup.cancel')}
              </button>
            </div>
          </form>
        </div>
      {:else}
        <div class="flex flex-col items-start" style="gap: 12px;">
          {#if totpInitError}
            <p style="font-size: 12px; color: #f87171; margin: 0;">{totpInitError}</p>
          {/if}
          <button
            type="button"
            disabled={totpInitBusy}
            onclick={startTotpSetup}
            class="inline-flex items-center gap-1.5 transition-opacity"
            style="background: {accent}; color: #1a1410; padding: 12px 24px; border-radius: 999px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; box-shadow: 0 8px 20px rgba(200, 169, 106, 0.25); opacity: {totpInitBusy ? 0.6 : 1}; cursor: {totpInitBusy ? 'wait' : 'pointer'};"
          >
            <ShieldCheck size={13} strokeWidth={2} />
            {totpInitBusy
              ? $t('settings.security.setup.starting')
              : $t('settings.security.setup.button')}
          </button>
        </div>
      {/if}
    </div>
  {/if}

  <!-- ─── Section: Bans (Brute-Force-Schutz, Admin-only) ─── -->
  {#if section === 'bans' && me?.is_admin}
    <div
      class="tonus-fadein"
      style="background: rgba(20, 20, 24, 0.5); backdrop-filter: blur(40px) saturate(1.2); -webkit-backdrop-filter: blur(40px) saturate(1.2); border: 1px solid var(--color-border-soft); border-radius: 22px; padding: 32px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);"
    >
      <div
        class="font-semibold uppercase"
        style="font-size: 11px; letter-spacing: 0.2em; color: var(--color-fg-tertiary); margin-bottom: 6px;"
      >
        {$t('settings.bans.eyebrow')}
      </div>
      <div
        style="font-family: var(--font-display); font-size: 22px; font-weight: 500; letter-spacing: -0.015em; color: var(--color-fg-primary); margin-bottom: 18px;"
      >
        {$t('settings.bans.title')}
      </div>
      <p
        style="font-size: 13px; color: var(--color-fg-secondary); margin-bottom: 22px; line-height: 1.55; max-width: 620px;"
      >
        {$t('settings.bans.body')}
      </p>

      {#if bansError}
        <p style="font-size: 12px; color: #f87171; margin: 0 0 12px;">{bansError}</p>
      {/if}
      {#if !bansLoaded}
        <p style="font-size: 12px; color: var(--color-fg-tertiary); margin: 0;">…</p>
      {:else if bans.length === 0}
        <p style="font-size: 13px; color: var(--color-fg-tertiary); margin: 0;">
          {$t('settings.bans.empty')}
        </p>
      {:else}
        <div
          class="font-semibold uppercase"
          style="font-size: 10.5px; letter-spacing: 0.18em; color: var(--color-fg-tertiary); margin-bottom: 10px;"
        >
          {$t('settings.bans.list_title')}
        </div>
        <ul class="flex flex-col" style="gap: 10px; margin: 0; padding: 0; list-style: none;">
          {#each bans as b (b.ip)}
            {@const now = Date.now()}
            {@const bannedAgo = now - b.banned_at_ms}
            <li
              class="flex items-center justify-between gap-4 flex-wrap"
              style="background: rgba(248, 113, 113, 0.05); border: 1px solid rgba(248, 113, 113, 0.2); border-radius: 14px; padding: 14px 18px;"
            >
              <div class="flex flex-col" style="gap: 4px; min-width: 0; flex: 1;">
                <div class="flex items-center gap-2 flex-wrap">
                  <Ban size={14} strokeWidth={2} style="color: #f87171;" />
                  <code
                    style="font-family: var(--font-mono); font-size: 14px; font-weight: 500; color: var(--color-fg-primary); letter-spacing: 0.04em;"
                    >{b.ip}</code
                  >
                </div>
                <div
                  class="flex items-center gap-2 flex-wrap"
                  style="font-size: 11.5px; color: var(--color-fg-secondary);"
                >
                  <span>{$t('settings.bans.banned_when', { when: formatRelative(bannedAgo) })}</span>
                  {#if b.failed_count > 0}
                    <span style="color: var(--color-fg-tertiary);">·</span>
                    <span>{$t('settings.bans.fails', { count: String(b.failed_count) })}</span>
                  {/if}
                </div>
              </div>
              <button
                type="button"
                disabled={unbanningIp === b.ip}
                onclick={() => unbanIp(b.ip)}
                class="inline-flex items-center gap-1.5 transition-opacity flex-shrink-0"
                style="background: rgba(255, 255, 255, 0.06); color: var(--color-fg-primary); padding: 8px 16px; border-radius: 999px; font-size: 11px; font-weight: 500; letter-spacing: 0.04em; text-transform: uppercase; border: 1px solid var(--color-border-soft); opacity: {unbanningIp === b.ip ? 0.6 : 1};"
              >
                {unbanningIp === b.ip
                  ? $t('settings.bans.unban_busy')
                  : $t('settings.bans.unban')}
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</section>
