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
  overlay_profile: string | null;
  overlay_element_count: number;
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
  // has_default/default/tier are only populated for config-field entries
  // (SPEC_S13.md S13b items 3 and 6) -- provider/feature/secret help entries
  // omit them. tier is hand-tagged per field and most of the 159 don't have
  // one yet, so it's always optional, never a bare null.
  help: Record<string, { short: string; long: string; has_default?: boolean; default?: unknown; tier?: 'basic' | 'advanced' | 'expert' }>;
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


// ---- S10a: library health scanner (SPEC_S10) ----

export type Severity = 'error' | 'warning' | 'info';
export type FixKind =
  | 'apply_item' | 'refresh_item' | 'candidate_picker' | 'external' | 'none';

export interface ScanRow {
  scan_id: string;
  started_at: string;
  finished_at: string | null;
  status: string;                 // running | ok | error
  library: string | null;         // null = every configured library
  tier: number;
  items_scanned: number;
  finding_count: number;
}

export interface ScanCounts {
  by_severity: Record<string, number>;
  by_group: Record<string, number>;
  by_check: Record<string, number>;
}

export interface ScanFinding {
  id: number;
  scan_id: string;
  library: string;
  item_id: string | null;         // null for library-level findings
  item_name: string;
  media_type: string;
  check_code: string;             // "<group>.<detail>"
  group: string;
  severity: Severity;
  detail: Record<string, any>;
  fix_kind: FixKind;
  fixed_at: string | null;        // a fix was submitted; rescan to confirm
}

export interface ScansResp { scans: ScanRow[] }
export interface LatestScanResp { scan: ScanRow | null; counts: ScanCounts }
export interface FindingsResp {
  scan_id: string;
  findings: ScanFinding[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}
export interface FixResp {
  submitted: Array<{
    item_id: string; action: string; run_id?: string;
    queued?: boolean; detail?: string | null; error?: string;
  }>;
  not_fixable: number[];
  unknown: number[];
}

// ---- S11a: Activity Center ----

export interface ActivityRun {
  run_id: string;
  action: string | null;          // null on rows from before S11a
  scope: string | null;           // "lib:<name>" | "item:<id>" | "all"
  tier: number | null;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  duration_s: number | null;
  files_written: number;
  files_unchanged: number;
  api_calls: number;
  errors: number;
  warnings: number;
  last_error: string | null;
  retryable: boolean;
}

export interface QueuedLibrary { action: string; library: string; tier: number }

export interface ActivityResp {
  active: {
    run_id: string; action: string | null; scope: string | null;
    tier: number | null; started_at: string | null;
  } | null;
  queued: { items: string[]; libraries: QueuedLibrary[] };
  schedules: Array<{ id: string; name: string; next_run_at: string | null }>;
  runs: ActivityRun[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  dry_run: boolean;
}

// ---- S11c: backup bundles ----

export interface BundleRow { name: string; bytes: number; modified_at: number }

export interface BundlesResp {
  bundles: BundleRow[];
  keep_bundles: number;
  excluded: string[];
}

export interface InspectResp {
  manifest: {
    bundle_version: number; finmeta_version: string; created_at: string;
    contents: string[]; excluded: string[]; secret_names: string[];
    counts: Record<string, number>;
  };
  members: string[];
  overrides: Record<string, number>;
  packs: Record<string, string[]>;
  diff: {
    libraries?: { added: string[]; removed: string[]; kept: string[] };
    overlay_profiles?: { added: string[]; removed: string[] };
    schedules?: { current: number; incoming: number };
    dry_run?: { current: boolean; incoming: boolean };
    config?: string;
  };
  secrets_required: string[];
  note: string;
}

export interface RestoreResp {
  restored: { config: boolean; overrides: Record<string, number> };
  mode: string;
  packs_to_reimport: Record<string, string[]>;
  secrets_required: string[];
  note: string;
}
