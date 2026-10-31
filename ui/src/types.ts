// Types mirror the FastAPI JSON shapes (backend/finmeta/api/*).

export type FileStatus = 'create' | 'update' | 'unchanged';

export interface FileWrite {
  target_path: string;
  kind: string;
  source_ref: string;
  content_sha256: string;
  status: FileStatus;
}

export interface FileEntry {
  kind: string;
  status: FileStatus;
  target_path: string;
  source_ref: string;
}

export interface ItemView {
  item_id: string;
  item_name: string;
  media_type: 'movie' | 'tvshow';
  tmdb_id: number | null;
  matched_by: string | null;
  files: FileEntry[];
  diff: { create: number; update: number; unchanged: number };
  status: 'empty' | 'changes' | 'clean';
}

/** Jellyfin-only item (GET .../items/light): no TMDb/diff/status -- just
 * enough to list and search. See LibraryBrowser.tsx. */
export interface ItemLightView {
  item_id: string;
  item_name: string;
  media_type: 'movie' | 'tvshow';
}

export interface UnmatchedView {
  item_id: string;
  name: string;
  reason: string;
}

export interface LibraryView {
  name: string;
  type: string;
  metadata: boolean;
  theme_song: boolean;
  overlays: string[];
  artwork_enabled: string[];
}

export interface LibrariesResp {
  libraries: LibraryView[];
  dry_run: boolean;
}

export interface ArrInstanceView {
  name: string; url: string; api_key: string;
  timeout: number; verify_ssl: boolean; enabled: boolean;
  /** False when api_key is not a valid secret name (invalid reference). */
  api_key_valid?: boolean;
}

export interface ProviderWarning {
  provider: string; error: string; count: number;
}

export interface LibItemsResp {
  library: string;
  /** S9d4c Fase A step 3: this library's master switch + effective overlay
   * profile/element count (profile wins, auto-migration already applied). */
  enabled: boolean;
  overlay_profile: string | null;
  overlay_element_count: number;
  /** S9d2-fix2 step 6a-fix: per-instance api_key_valid (invalid reference — re-save). */
  media_apps?: Record<'sonarr' | 'radarr', ArrInstanceView[]>;
  items: ItemView[];
  /** Present only on a paginated request (limit given): the full Movie/
   * Series count before slicing, so the caller knows if more pages remain. */
  total_items?: number | null;
  unmatched: UnmatchedView[];
  /** S9d2-fix2 step 6b: provider errors that degraded to "no candidates". */
  provider_warnings?: ProviderWarning[];
  summary: { items: number; create: number; update: number; unchanged: number };
}

/** GET .../items/light -- what the browser loads first (see LibraryBrowser.tsx).
 * Paginated (2026-10-30 fix): `items` is one page; `total_items`/`has_more`
 * tell the UI whether to offer "Load more". */
export interface LibItemsLightResp {
  library: string;
  enabled: boolean;
  overlay_profile: string | null;
  overlay_element_count: number;
  media_apps?: Record<'sonarr' | 'radarr', ArrInstanceView[]>;
  items: ItemLightView[];
  total_items: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface PlanResp {
  item_id: string;
  item_name: string;
  media_type: string;
  tmdb_id: number | null;
  matched_by: string | null;
  files: FileEntry[];
  diff: { create: number; update: number; unchanged: number };
  status: string;
  /** S9d4b step 4: kinds skipped for this item (server is the truth source). */
  skipped?: string[];
  plan: {
    item_id: string;
    item_name: string;
    media_type: string;
    tmdb_id: number | null;
    matched_by: string | null;
    files: FileWrite[];
    /** S9g1/K7a: computed TV show status from Sonarr, e.g. "new_season_soon" (TV shows only). */
    tv_status?: string | null;
  };
}

export interface Candidate {
  kind: string;
  source: string;
  ref: string;
  proxy_url: string;
  lang: string | null;
  width: number | null;
  height: number | null;
  score: number;
  selected: boolean;
}

export interface CandidatesResp {
  item_id: string;
  tmdb_id: number | null;
  candidates: Candidate[];
  /** S9d5: kinds locked since their first successful apply (plan/apply
   * never re-queries sources for these — see Reshuffle, which also clears
   * the lock). */
  locked_kinds?: string[];
}

export interface RunResp {
  job_id: string;
  status: string;
  action: string;
}

export interface JobResp {
  run_id: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  files_written: number;
  files_unchanged: number;
  api_calls: number;
  log: Array<{ ts: string; level: string; message: string }>;
  /** S9d2-fix2 step 6b: provider warnings for this run (mirrors plan). */
  provider_warnings?: ProviderWarning[];
  /** Parsed from the "plan ok ..." log line -- null for apply runs (and for
   * a plan run that hasn't reached that line yet). files_written/
   * files_unchanged/api_calls above are always 0 for plan, so this is the
   * only place the real counts live. */
  plan_summary?: {
    items: number; files: number; unmatched: number; actions: number;
    format: string | null;
  } | null;
}

export interface ActiveRunScope {
  action: string | null;
  library: string | null;
  item_id: string | null;
}

export interface HealthResp {
  status: string;
  version: string;
  config_loaded: boolean;
  config_error: string | null;
  dry_run: boolean;
  active_run: string | null;
  active_run_scope: ActiveRunScope | null;
}

// ---- S9a: auth + settings types ----

export interface AuthStatus {
  auth_enabled: boolean;
  has_password: boolean;
  first_run: boolean;
}

export interface SecretView {
  name: string;
  configured: boolean;
  source: 'env' | 'ui' | 'none';
  hint: string;
  env_var: string;
  locked: boolean;
  short: string;
  help: string;
}

export interface SecretsResp {
  encrypted_at_rest: boolean;
  secrets: Record<string, SecretView>;
}

export interface ServiceTestResult {
  ok: boolean;
  latency_ms: number | null;
  version: string | null;
  error: string | null;
}

export interface HelpResp {
  help: Record<string, { short: string; long: string }>;
}

// ---- S9b: provider registry types ----

export interface ProviderCapabilities {
  ratings: string[];
  metadata_fields: string[];
  image_kinds: string[];
}

export interface Provider {
  name: string;
  label: string;
  kind: 'local' | 'external';
  requires_key: boolean;
  capabilities: ProviderCapabilities;
  rate_limit: string | null;
  short: string;
  long: string;
  secret: string | null;
  env_var: string | null;
  get_key_url: string | null;
  enabled: boolean;
  coming_later: boolean;
}

export interface ProvidersResp {
  providers: Provider[];
}

