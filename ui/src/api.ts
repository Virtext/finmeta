import type {
  AuthStatus, CandidatesResp, HelpResp, JobResp, LibItemsLightResp, LibItemsResp,
  LibrariesResp, PlanResp, ProvidersResp, RunResp, SecretsResp, ServiceTestResult,
} from './types';

// Same-origin API (FastAPI serves both /api/* and the built UI at /).

export class ApiError extends Error {
  status: number;
  path: string;
  constructor(status: number, path: string, message: string) {
    super(message);
    this.status = status;
    this.path = path;
  }
  /** Any 401 from the Finmeta API means the session expired — log out. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }
}

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, init);
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const b = await r.json();
      detail = typeof b.detail === 'string' ? b.detail : JSON.stringify(b);
    } catch {
      /* ignore */
    }
    throw new ApiError(r.status, path, `HTTP ${r.status}: ${detail}`);
  }
  return (await r.json()) as T;
}

const enc = (s: string) => encodeURIComponent(s);

export const getLibraries = () => j<LibrariesResp>('/api/libraries');
export const getItemsLight = (lib: string) =>
  j<LibItemsLightResp>(`/api/libraries/${enc(lib)}/items/light`);
export const getItems = (lib: string, page?: { limit: number; offset: number }) =>
  j<LibItemsResp>(
    `/api/libraries/${enc(lib)}/items` +
    (page ? `?limit=${page.limit}&offset=${page.offset}` : ''),
  );
export const getPlan = (id: string) => j<PlanResp>(`/api/items/${enc(id)}/plan`);
export const getCandidates = (id: string) =>
  j<CandidatesResp>(`/api/items/${enc(id)}/candidates`);

export const selectCandidate = (id: string, kind: string, ref: string | null) =>
  j(`/api/items/${enc(id)}/candidates`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, ref }),
  });

// S9d4a step 3: bump per-kind reshuffle counters so the deterministic
// random_top_n preset picks a different candidate on re-plan.
export interface ReshuffleResp { ok: boolean; item_id: string; counts: Record<string, number> }
export const reshuffleItem = (id: string, kinds: string[]) =>
  j<ReshuffleResp>(`/api/items/${enc(id)}/reshuffle`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kinds }),
  });

// S9d4b: per-row plan handlers — skip a file kind for this item (or unskip).
export interface SkipKindsResp { ok: boolean; item_id: string; kind: string; skipped: string[] }
export const skipItemKind = (id: string, kind: string) =>
  j<SkipKindsResp>(`/api/items/${enc(id)}/skip-kinds`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind }),
  });
export const unskipItemKind = (id: string, kind: string) =>
  j<SkipKindsResp>(`/api/items/${enc(id)}/skip-kinds/${enc(kind)}`, { method: 'DELETE' });

export const startRun = (action: string, item?: string, library?: string) =>
  j<RunResp>('/api/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, item, library }),
  });

export const getJob = (id: string) => j<JobResp>(`/api/jobs/${enc(id)}`);

// Build a proxy URL from a plan source_ref (the browser never calls
// TMDb/fanart directly — every byte goes through /api/proxy). The token is
// base64url("<kind>|<ref>"), matching backend/finmeta/api/proxy.py.
export function proxyUrl(kind: string, ref: string): string {
  const raw = `${kind}|${ref}`;
  const b64 = btoa(raw);
  const b64url = b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return `/api/proxy/${b64url}`;
}

export function findFile(files: { kind: string; source_ref: string }[], kind: string) {
  return files.find((f) => f.kind === kind) ?? null;
}


// ---- S9a: auth + settings APIs ----

export const getAuthStatus = () => j<AuthStatus>('/api/auth/status');

export const setupPassword = (password: string) =>
  j('/api/auth/setup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });

export const login = (username: string, password: string) =>
  j('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });

export const logout = () =>
  j('/api/auth/logout', { method: 'POST' });

export const getMe = () => j<{ username: string }>('/api/auth/me');

export const updateAccount = (body: {
  current_password: string;
  new_username?: string;
  new_password?: string;
}) =>
  j('/api/auth/account', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

export const getSecrets = () => j<SecretsResp>('/api/secrets');

export const putSecret = (name: string, value: string) =>
  j(`/api/secrets/${enc(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value }),
  });

export const deleteSecret = (name: string) =>
  j(`/api/secrets/${enc(name)}`, { method: 'DELETE' });

export const testService = (name: string) =>
  j<ServiceTestResult>(`/api/services/${enc(name)}/test`, { method: 'POST' });

export const getJellyfinWebhookStatus = () =>
  j<{ configured: boolean }>('/api/webhooks/jellyfin/status');
export const regenerateJellyfinWebhook = () =>
  j<{ url: string }>('/api/webhooks/jellyfin/regenerate', { method: 'POST' });
export const disableJellyfinWebhook = () =>
  j<{ deleted: boolean }>('/api/webhooks/jellyfin', { method: 'DELETE' });

// S9f1a-fix: test the TYPED form values before saving. The key is only used for
// this one request — never logged or stored by this call.
export interface ArrTestConfigBody {
  name: string; url: string; api_key: string; timeout: number; verify_ssl: boolean;
}
export const testArrConfig = (app: 'sonarr' | 'radarr', body: ArrTestConfigBody) =>
  j<ServiceTestResult>(`/api/services/${app}/test-config`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

export const getHelp = () => j<HelpResp>('/api/help');

// ---- S9c8: logo packs (trademark logos are downloaded on demand at the user's request from the Kometa project; never shipped with Finmeta itself or committed to git) ----

export interface LogoPackDetail { name: string; installed: boolean; complete: boolean; have: number; total: number; missing: string[] }
export interface LogoPacksListResp { packs: string[]; known: string[]; detail: LogoPackDetail[] }
export interface InstallLogoPackResp { pack: string; downloaded: number; skipped: number; failed: number; errors: {file:string,error:string}[]; note?: string }

export const listLogoPacks = () => j<LogoPacksListResp>('/api/assets/logo-packs');
export const logoPackStatus = (name: string) =>
  j<LogoPackDetail>(`/api/assets/logo-packs/${enc(name)}/status`);
export const installLogoPack = (name: string) =>
  j<InstallLogoPackResp>(`/api/assets/logo-packs/${enc(name)}/install`, { method: 'POST' });

export const getConfig = () =>
  j<{ yaml: string; parsed: any; gen: number }>('/api/config');

// S9d1: K9 default format rules from the backend (single source of truth --
// the UI must not keep its own hardcoded copy).
export interface FormatRuleEntry { key: string; match: string; label: string; image: string; priority: number; enabled: boolean }
export const getDefaultFormatRules = () =>
  j<{ rules: Record<string, FormatRuleEntry[]> }>('/api/format-rules/defaults');

export const putConfig = (yaml: string) =>
  j('/api/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ yaml }),
  });

export const getProviders = () => j<ProvidersResp>('/api/providers');

export interface RatingSourceField {
  name: string;
  label: string;
  providers: string[];
  available?: string[];
}

export interface RatingSourcesResp {
  fields: RatingSourceField[];
  enabled_by_name: Record<string, boolean>;
}

export const getRatingSources = () => j<RatingSourcesResp>('/api/rating-sources');

// ---- Overlay editor (S9 §D) ---------------------------------------------
export interface OverlaysPreset { name: string; description?: string; style: any }
export interface OverlaysUserStyle { name: string; style: any }
export interface OverlaysResp {
  presets: OverlaysPreset[];
  user_styles: OverlaysUserStyle[];
  base_style?: Record<string, unknown>;
}
export const getOverlays = () => j<OverlaysResp>('/api/overlays');

export interface OverlayPreviewReq { style: Record<string, unknown>; item_ids: string[] }
export interface FormatRuleMatch { key: string; label: string }
export interface OverlayPreviewEntry {
  item_id: string; image?: string; error?: string; samples?: string[];
  /** S9d4c: the EXACT pre-compositing candidate "image" was rendered from,
   * as a data URL -- show this as "Base image" so it's guaranteed to match
   * "With overlay" (they used to come from two independently-selected
   * candidates, confusing operators into thinking overlays "weren't
   * applying" when really two different posters were being compared). */
  base_image?: string;
  rating_sources?: Record<string, string>;
  format_rule_matches?: { resolution: FormatRuleMatch | null; dynamic_range: FormatRuleMatch | null; audio: FormatRuleMatch | null };
}
export interface OverlayPreviewResp { previews: OverlayPreviewEntry[] }
export function previewOverlay(style: Record<string, unknown>, itemIds: string[], sampleData = false, profile?: string | null): Promise<{previews: OverlayPreviewEntry[]}> {
  // S9d4c: the preview can involve slow external fetches (TMDb etc.) and had
  // no timeout, so a hung upstream call left the UI stuck on "rendering…"
  // forever with no error. 25s is generous but bounded.
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 25000);
  return fetch('/api/overlays/preview', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({style, item_ids:itemIds, sample_data:sampleData, ...(profile ? { profile } : {})}),
    signal: controller.signal,
  }).then(async (r) => { if(!r.ok){const t=await r.text(); throw new Error(`HTTP ${r.status}: ${t}`);} return r.json(); })
    .catch((e: any) => {
      if (e?.name === 'AbortError') throw new Error('preview timed out after 25s');
      throw e;
    })
    .finally(() => clearTimeout(timeout));
}

// ---- S9d2: library profiles + seeded sample + bulk apply ------------------

export interface JfLibrary {
  id: string; name: string; collection_type: string; locations: string[];
  configured: boolean; enabled: boolean; overlay_profile: string | null;
}
export interface JfLibrariesResp { libraries: JfLibrary[] }
export const getJellyfinLibraries = () => j<JfLibrariesResp>('/api/jellyfin/libraries');

export interface ProfileYamlCheck { ok: boolean; name: string }
export const checkProfileYaml = (name: string, yamlText: string) =>
  j<ProfileYamlCheck>(`/api/profiles/${enc(name)}/yaml`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ yaml: yamlText }),
  });

export function saveOverlay(name: string, style: Record<string, unknown>): Promise<{ok:boolean,name:string,gen:number}> {
  return fetch(`/api/overlays/${encodeURIComponent(name)}`, {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name, style}),
  }).then(async (r)=>{ if(!r.ok){const b= await r.json().catch(()=>null); const d=b&&b.detail?String(b.detail):`HTTP ${r.status}`;throw new Error(d);} return r.json();});
}

// S9d3: image packs (GET /api/packs) — pack list with categories and missing images.
export interface PackCategoryDef {
  matcher: string;
  keys?: string[];
  regex?: string | null;
  field?: string | null;
  tiers?: Record<string, string> | null;
}
export interface ImagePackInfo {
  name: string;
  description?: string | null;
  categories: Record<string, PackCategoryDef>;
  missing_images: string[];
  valid: boolean;
  error?: string | null;
}
export interface PacksResp { packs: ImagePackInfo[] }
export const getPacks = () => j<PacksResp>('/api/packs');
export const deletePack = (name: string) =>
  j<{ ok: boolean; name: string }>(`/api/packs/${enc(name)}`, { method: 'DELETE' });

// S9f2: built-in preset collections (seasonal + chart) catalog.
export interface CollectionPresetInfo {
  id: string;
  label: string;
  description: string;
  library_type: 'movies' | 'tvshows';
  kind: 'seasonal_adaptive' | 'chart';
}
export const getCollectionPresets = () =>
  j<{ presets: CollectionPresetInfo[] }>('/api/collection-presets');


export interface KometaImportParams {
  overlay_file: string;
  kometa_root: string;
  pack_name: string;
  dry_run?: boolean;
  overwrite?: boolean;
}
export interface KometaImportResp {
  dry_run: boolean;
  pack_name: string;
  pack_yml: { name: string; description?: string | null; categories: Record<string, PackCategoryDef> };
  image_copies: [string, string][];
  missing_images: string[];
  written?: string[];
  report?: {
    source: string;
    counts: Record<string, number>;
    missing_templates: string[];
    errors: string[];
    items: { key: string; status: string; notes: string[] }[];
  };
}
// S9e4: filesystem browser for the import panel (folders + .yml files only,
// restricted server-side to /import and /work).
export interface FsListResp { path: string; folders: string[]; files: string[] }
export const listFs = (path: string) =>
  j<FsListResp>(`/api/fs/list?path=${enc(path)}`);

export const importKometa = (params: KometaImportParams) =>
  j<KometaImportResp>('/api/kometa/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  });
