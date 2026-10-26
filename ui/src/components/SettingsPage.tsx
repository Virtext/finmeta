import { useCallback, useEffect, useState } from 'react';
import {
  getSecrets, putSecret, deleteSecret, testService, testArrConfig, getHelp,
  getConfig, putConfig, logout, getProviders, getRatingSources,
  getJellyfinWebhookStatus, regenerateJellyfinWebhook, disableJellyfinWebhook,
} from '../api';
import type { ArrTestConfigBody } from '../api';
import * as YAML from 'js-yaml';
import OverlaysEditor from './OverlaysEditor';
import ProfilesPanel from './ProfilesPanel';
import SchedulesPanel from './SchedulesPanel';
import TrendingPanel from './TrendingPanel';
import CollectionsPanel from './CollectionsPanel';
import type { RatingSourcesResp } from '../api';
import type {
  HelpResp, Provider, SecretView, SecretsResp, ServiceTestResult,
} from '../types';

interface Props {
  onBack: () => void;
  /** S9d4c Fase A step 4: open straight to a tab (+ preselected Overlays
   * library) — used by PreviewPane's "Edit overlay profile" link. */
  initialTab?: Tab;
  initialOverlayLibrary?: string | null;
}

type Tab = 'general' | 'local' | 'external' | 'secrets' | 'ratings' | 'overlays' | 'libraries' | 'schedules' | 'trending' | 'collections' | 'advanced';

const TABS: { id: Tab; label: string }[] = [
  { id: 'general', label: 'General' },
  { id: 'local', label: 'Local services' },
  { id: 'external', label: 'External services' },
  { id: 'secrets', label: 'Secrets' },
  { id: 'ratings', label: 'Rating sources' },
  { id: 'overlays', label: 'Overlays' },
  { id: 'libraries', label: 'Libraries' },
  { id: 'schedules', label: 'Schedules' },
  { id: 'trending', label: 'Trending' },
  { id: 'collections', label: 'Collections' },
  { id: 'advanced', label: 'Advanced' },
];

function HelpIcon({ help }: { help?: string }) {
  const [open, setOpen] = useState(false);
  if (!help) return null;
  return (
    <span className="help-icon" onClick={(e) => { e.stopPropagation(); setOpen(!open); }}>
      (i)
      {open && <span className="help-pop" dangerouslySetInnerHTML={{ __html: help.replace(/\n/g, '<br/>') }} />}
    </span>
  );
}

// Patch one dotted path in the parsed config object (immutable update) and
// return the new object. Used by the Local services / External services tabs
// to edit individual fields without touching the raw YAML editor.
function patchParsed(parsed: any, pathParts: string[], value: any): any {
  const clone = structuredClone(parsed);
  let cur = clone;
  for (let i = 0; i < pathParts.length - 1; i++) {
    if (cur[pathParts[i]] == null) cur[pathParts[i]] = {};
    cur = cur[pathParts[i]];
  }
  cur[pathParts[pathParts.length - 1]] = value;
  return clone;
}

function ConfigField({
  label, help, value, onChange, type = 'text', placeholder,
}: {
  label: string; help?: string; value: any;
  onChange: (v: string) => void; type?: string; placeholder?: string;
}) {
  return (
    <div className="field-row">
      <label className="field-label">{label} <HelpIcon help={help} /></label>
      {type === 'checkbox' ? (
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked ? 'true' : 'false')}
        />
      ) : (
        <input
          className="login-input config-input"
          type={type}
          value={value == null ? '' : String(value)}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </div>
  );
}

function SecretField({
  secret, onSave, onDelete,
}: {
  secret: SecretView;
  onSave: (v: string) => Promise<boolean>;
  onDelete: () => void;
}) {
  const [val, setVal] = useState('');
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const save = async () => {
    if (!val) return;
    setErr(null);
    const ok = await onSave(val);
    if (ok) {
      setVal('');
      setMsg('Saved');
      setTimeout(() => setMsg(null), 2000);
    }
  };

  return (
    <div className="secret-field">
      <label className="field-label">
        {secret.short} <HelpIcon help={secret.help} />
      </label>
      <div className="secret-row">
        <input
          className="login-input secret-input"
          type="password"
          placeholder={secret.locked ? `set via ${secret.env_var}` : secret.hint || 'enter value'}
          value={val}
          disabled={secret.locked}
          onChange={(e) => setVal(e.target.value)}
        />
        {!secret.locked && (
          <button className="btn" onClick={save} disabled={!val}>Save</button>
        )}
        {secret.configured && !secret.locked && (
          <button className="btn" onClick={onDelete}>Clear</button>
        )}
      </div>
      {secret.locked && <div className="muted field-hint">Set via environment variable <code>{secret.env_var}</code></div>}
      {msg && <div className="field-msg ok">{msg}</div>}
      {err && <div className="field-msg err">{err}</div>}
    </div>
  );
}

function TestButton({ service }: { service: string }) {
  const [result, setResult] = useState<ServiceTestResult | null>(null);
  const [busy, setBusy] = useState(false);

  const test = async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await testService(service);
      setResult(r);
    } catch (e: any) {
      setResult({ ok: false, latency_ms: null, version: null, error: e.message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="test-row">
      <button className="btn" onClick={test} disabled={busy}>
        {busy ? 'Testing…' : 'Test'}
      </button>
      {result && (
        <span className={`test-result ${result.ok ? 'ok' : 'err'}`}>
          {result.ok ? `✓ ${result.version ?? 'OK'} (${result.latency_ms}ms)` : `✗ ${result.error ?? 'failed'}`}
        </span>
      )}
    </div>
  );
}

function WebhookSection() {
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [freshUrl, setFreshUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getJellyfinWebhookStatus().then((d) => setConfigured(d.configured)).catch(() => {});
  }, []);

  async function regenerate() {
    setBusy(true); setError(null);
    try {
      const d = await regenerateJellyfinWebhook();
      setFreshUrl(d.url);
      setConfigured(true);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function disable() {
    setBusy(true); setError(null);
    try {
      await disableJellyfinWebhook();
      setConfigured(false);
      setFreshUrl(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="field-row" style={{ flexDirection: 'column', alignItems: 'stretch', marginTop: 12 }}>
      <label className="field-label">
        Webhook (auto-apply on new content)
        <span className="muted" title="Jellyfin can call Finmeta the moment a new movie or episode is added, instead of waiting for the next scheduled run.">ⓘ</span>
      </label>
      <div className="field-hint muted">
        Requires Jellyfin's <strong>Webhook</strong> plugin: add a Generic destination with the
        URL below, enable "Item Added" for the libraries Finmeta manages. No custom payload
        template needed. Only Movies/Episodes in a library with "Managed by Finmeta" on are
        applied; everything else is ignored. If a run is already in progress, the item is
        queued and applied right after.
      </div>
      {freshUrl && (
        <div className="field-msg ok" style={{ wordBreak: 'break-all' }}>
          {freshUrl}
          <div className="field-hint muted">Copy this now — it is shown only once. Paste it into
            Jellyfin's webhook destination URL field.</div>
        </div>
      )}
      <div className="secret-row">
        <button className="btn" onClick={() => void regenerate()} disabled={busy}>
          {configured ? 'Regenerate URL' : 'Generate webhook URL'}
        </button>
        {configured && (
          <button className="btn" onClick={() => void disable()} disabled={busy}>Disable</button>
        )}
        {configured !== null && !freshUrl && (
          <span className={`test-result ${configured ? 'ok' : 'err'}`}>
            {configured ? '✓ configured' : '✗ not configured'}
          </span>
        )}
      </div>
      {error && <div className="field-msg err">{error}</div>}
    </div>
  );
}

interface ArrInstance {
  name: string; url: string; api_key: string;
  timeout: number; verify_ssl: boolean; enabled: boolean;
}

function MediaAppInstances({ app, label, helpKey, instances, validKey, onSaveList }: {
  app: 'sonarr' | 'radarr'; label: string; helpKey?: string;
  instances: ArrInstance[];
  /** S9d2-fix2 step 6a: whether an api_key reference names a valid secret. */
  validKey: (name: string) => boolean;
  /** Persist the new list (add or edit). Returns a validation error message
   *  (e.g. duplicate name) or null on success. */
  onSaveList: (list: ArrInstance[]) => Promise<string | null>;
}) {
  const [form, setForm] = useState<ArrInstance | null>(null); // null = not editing
  const [editingName, setEditingName] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const openAdd = () => {
    setEditingName(null);
    setForm({ name: '', url: '', api_key: '', timeout: 30, verify_ssl: true, enabled: true });
    setErr(null);
  };
  const openEdit = (inst: ArrInstance) => {
    setEditingName(inst.name);
    setForm({ ...inst, api_key: '' }); // key is secret-only; left blank = keep existing
    setErr(null);
  };

  const save = async () => {
    if (!form) return;
    if (!form.name.trim() || !form.url.trim()) {
      setErr('Name and URL are required.');
      return;
    }
    const others = instances.filter((i) => i.name !== editingName);
    if (others.some((i) => i.name === form.name.trim())) {
      setErr(`A ${label.toLowerCase()} instance named "${form.name.trim()}" already exists — names must be unique within a kind.`);
      return;
    }
    const clean: ArrInstance = { ...form, name: form.name.trim(), url: form.url.trim() };
    // When editing and the key field is blank, keep the existing secret name.
    if (editingName && !clean.api_key.trim()) {
      const prev = instances.find((i) => i.name === editingName);
      clean.api_key = prev?.api_key ?? '';
    }
    const list = editingName
      ? instances.map((i) => (i.name === editingName ? clean : i))
      : [...instances, clean];
    const e = await onSaveList(list);
    if (e) {
      setErr(e);
    } else {
      setForm(null);
      setEditingName(null);
      setMsg(editingName ? 'Saved' : 'Added');
      setTimeout(() => setMsg(null), 2000);
    }
  };

  const remove = async (inst: ArrInstance) => {
    // Deleting the instance also deletes its stored secret (<kind>_<name>_api_key).
    try { await deleteSecret(`${app}_${inst.name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'instance'}_api_key`); } catch { /* may not exist */ }
    const e = await onSaveList(instances.filter((i) => i.name !== inst.name));
    if (e) setErr(e);
  };

  return (
    <div className="service-card">
      <div className="service-head">
        <strong>{label}</strong> <HelpIcon help={helpKey} />
        <button className="btn" onClick={openAdd}>Add instance</button>
      </div>
      {instances.length === 0 && !form && (
        <div className="field-hint muted">No {label.toLowerCase()} instances configured.</div>
      )}
      {instances.map((inst) => (
        <div key={inst.name} className={`rating-row ${!inst.enabled ? 'rating-disabled' : ''}`}>
          <span className="rating-name">{inst.name}</span>
          <span className="field-hint muted">{inst.url} {!inst.enabled ? '(disabled)' : ''}</span>
          {inst.api_key && !validKey(inst.api_key) && (
            <span className="test-result err" title={`api_key "${inst.api_key}" is not a valid secret name`}>
              invalid reference — re-save instance
            </span>
          )}
          <TestButton service={`${app}:${inst.name}`} />
          <button className="btn" onClick={() => openEdit(inst)}>Edit</button>
          <button className="btn" onClick={() => remove(inst)}>Remove</button>
        </div>
      ))}
      {form && (
        <div className="secret-field">
          <div className="field-row">
            <input className="login-input config-input"
                   placeholder={app === 'sonarr' ? 'sonarr-4k' : 'radarr-4k'}
                   value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <input className="login-input config-input" placeholder={app === 'sonarr' ? 'url (e.g. http://192.168.1.10:8989)' : 'url (e.g. http://192.168.1.10:7878)'}
                   value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
          </div>
          <div className="field-row">
            <input className="login-input config-input" type="password"
                   placeholder={editingName ? 'API key (blank = keep current)' : 'API key'}
                   value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} />
            <input className="login-input config-input" type="number" style={{ width: 90 }}
                   title="Timeout (s)" value={String(form.timeout)}
                   onChange={(e) => setForm({ ...form, timeout: Number(e.target.value) || 30 })} />
            <label className="field-label">verify SSL
              <input type="checkbox" checked={form.verify_ssl}
                     onChange={(e) => setForm({ ...form, verify_ssl: e.target.checked })} />
            </label>
            <label className="field-label">enabled
              <input type="checkbox" checked={form.enabled}
                     onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
            </label>
          </div>
          <div className="secret-row">
            <button className="btn" onClick={save}>Save</button>
            <button className="btn" onClick={() => { setForm(null); setEditingName(null); setErr(null); }}>Cancel</button>
            <TestTyped app={app} form={form} />
          </div>
        </div>
      )}
      {err && <div className="field-msg err">{err}</div>}
      {msg && <div className="field-msg ok">{msg}</div>}
    </div>
  );
}

function TestTyped({ app, form }: { app: 'sonarr' | 'radarr'; form: ArrInstance }) {
  const [result, setResult] = useState<ServiceTestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const test = async () => {
    if (!form.name.trim() || !form.url.trim()) return;
    setBusy(true);
    setResult(null);
    const body: ArrTestConfigBody = {
      name: form.name.trim(), url: form.url.trim(), api_key: form.api_key.trim(),
      timeout: form.timeout, verify_ssl: form.verify_ssl,
    };
    try {
      setResult(await testArrConfig(app, body));
    } catch (e: any) {
      setResult({ ok: false, latency_ms: null, version: null, error: e.message });
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button className="btn" onClick={test} disabled={busy || !form.url.trim()}>
        {busy ? 'Testing…' : 'Test'}
      </button>
      {result && (
        <span className={`test-result ${result.ok ? 'ok' : 'err'}`}>
          {result.ok ? `✓ ${result.version ?? 'OK'} (${result.latency_ms}ms)` : `✗ ${result.error ?? 'failed'}`}
        </span>
      )}
    </>
  );
}

interface PathMappingRow { jellyfin: string; local: string; }

function GeneralTab({ configParsed, secrets, helpFor, saveConfigPatch }: {
  configParsed: any;
  secrets: SecretsResp | null;
  helpFor: (key: string) => string | undefined;
  saveConfigPatch: (pathParts: string[], value: any) => Promise<void>;
}) {
  const [newJellyfin, setNewJellyfin] = useState('');
  const [newLocal, setNewLocal] = useState('');
  const [err, setErr] = useState<string | null>(null);

  const mappings: PathMappingRow[] = configParsed?.path_mappings ?? [];
  const fallback: (string | null)[] = configParsed?.artwork_language_fallback ?? [null];

  const patchMappings = (rows: PathMappingRow[]) => {
    setErr(null);
    void saveConfigPatch(['path_mappings'], rows).catch((e: any) => setErr(e.message));
  };

  const addMapping = () => {
    if (!newJellyfin.trim() || !newLocal.trim()) return;
    patchMappings([...mappings, { jellyfin: newJellyfin.trim(), local: newLocal.trim() }]);
    setNewJellyfin('');
    setNewLocal('');
  };

  const setFallbackAt = (idx: number, value: string) => {
    const next = fallback.slice();
    next[idx] = value === '' ? null : value;
    void saveConfigPatch(['artwork_language_fallback'], next).catch(() => {});
  };

  // S9d2-fix2 step 5c: a field whose value comes from an env var (e.g. the
  // Jellyfin key via server.api_key_env) is shown locked with the env var name.
  const jellyfinKey = secrets?.secrets['jellyfin_api_key'] ?? null;

  return (
    <div className="settings-section">
      <h3>General</h3>
      <div className="field-row">
        <label className="field-label">
          dry_run <HelpIcon help={helpFor('dry_run')} />
        </label>
        <input
          type="checkbox"
          checked={Boolean(configParsed?.dry_run)}
          onChange={(e) => void saveConfigPatch(['dry_run'], e.target.checked).catch(() => {})}
        />
      </div>
      <div className="field-row">
        <label className="field-label">
          language <HelpIcon help={helpFor('language')} />
        </label>
        <input
          className="login-input config-input"
          type="text"
          value={configParsed?.language ?? ''}
          placeholder="en-US"
          onChange={(e) => void saveConfigPatch(['language'], e.target.value).catch(() => {})}
        />
      </div>
      {/* S9d4a: global artwork selection preset + top_n */}
      <div className="field-row">
        <label className="field-label">
          artwork_selection.preset <HelpIcon help={helpFor('artwork_selection.preset')} />
        </label>
        <select
          className="login-input config-input"
          value={configParsed?.artwork_selection?.preset ?? 'best_rated'}
          onChange={(e) => void saveConfigPatch(['artwork_selection', 'preset'], e.target.value).catch(() => {})}
        >
          <option value="best_rated">best_rated</option>
          <option value="random_top_n">random_top_n</option>
        </select>
      </div>
      <div className="field-row">
        <label className="field-label">
          artwork_selection.top_n <HelpIcon help={helpFor('artwork_selection.top_n')} />
        </label>
        <input
          className="login-input config-input"
          type="number" min={1} max={20}
          value={configParsed?.artwork_selection?.top_n ?? 3}
          onChange={(e) => void saveConfigPatch(['artwork_selection', 'top_n'], Math.max(1, Number(e.target.value) || 1)).catch(() => {})}
        />
      </div>
      {/* S9d5: global artwork write method (sidecar files vs Jellyfin API upload) */}
      <div className="field-row">
        <label className="field-label">
          artwork_write_method <HelpIcon help={helpFor('artwork_write_method')} />
        </label>
        <select
          className="login-input config-input"
          value={configParsed?.artwork_write_method ?? 'sidecar'}
          onChange={(e) => void saveConfigPatch(['artwork_write_method'], e.target.value).catch(() => {})}
        >
          <option value="sidecar">sidecar</option>
          <option value="jellyfin_api">jellyfin_api</option>
        </select>
      </div>
      {/* S9d5: backup trash retention (days a superseded backup is kept before deletion) */}
      <div className="field-row">
        <label className="field-label">
          backup_retention_days <HelpIcon help={helpFor('backup_retention_days')} />
        </label>
        <input
          className="login-input config-input"
          type="number" min={0} max={365}
          value={configParsed?.backup_retention_days ?? 14}
          onChange={(e) => void saveConfigPatch(['backup_retention_days'], Math.max(0, Number(e.target.value) || 0)).catch(() => {})}
        />
      </div>
      {/* S9g1/K7a: TV show status (Sonarr) — recent-days window + optional per-status collections */}
      <div className="field-row">
        <label className="field-label">
          status_overlay.recent_days <HelpIcon help={helpFor('status_overlay.recent_days')} />
        </label>
        <input
          className="login-input config-input"
          type="number" min={0} max={90}
          value={configParsed?.status_overlay?.recent_days ?? 14}
          onChange={(e) => void saveConfigPatch(['status_overlay', 'recent_days'], Math.max(0, Number(e.target.value) || 0)).catch(() => {})}
        />
      </div>
      <div className="field-row">
        <label className="field-label">
          status_overlay.create_collections <HelpIcon help={helpFor('status_overlay.create_collections')} />
        </label>
        <input
          type="checkbox"
          checked={Boolean(configParsed?.status_overlay?.create_collections)}
          onChange={(e) => void saveConfigPatch(['status_overlay', 'create_collections'], e.target.checked).catch(() => {})}
        />
      </div>
      <div className="field-row">
        <label className="field-label">
          artwork_language_fallback <HelpIcon help={helpFor('artwork_language_fallback')} />
        </label>
        <div className="ordered-list">
          {fallback.map((lang, idx) => (
            <div key={idx} className="ordered-row">
              <span className="rating-order">{idx + 1}</span>
              <input
                className="login-input config-input"
                type="text"
                value={lang ?? ''}
                placeholder="(textless)"
                onChange={(e) => setFallbackAt(idx, e.target.value)}
              />
              <button
                className="btn rating-arrow"
                disabled={idx === 0}
                onClick={() => {
                  const next = fallback.slice();
                  [next[idx - 1], next[idx]] = [next[idx], next[idx - 1]];
                  void saveConfigPatch(['artwork_language_fallback'], next).catch(() => {});
                }}
              >↑</button>
              <button
                className="btn rating-arrow"
                disabled={idx === fallback.length - 1}
                onClick={() => {
                  const next = fallback.slice();
                  [next[idx + 1], next[idx]] = [next[idx], next[idx + 1]];
                  void saveConfigPatch(['artwork_language_fallback'], next).catch(() => {});
                }}
              >↓</button>
              <button
                className="btn rating-arrow"
                disabled={fallback.length <= 1}
                onClick={() => {
                  const next = fallback.slice();
                  next.splice(idx, 1);
                  void saveConfigPatch(['artwork_language_fallback'], next).catch(() => {});
                }}
              >✕</button>
            </div>
          ))}
        </div>
      </div>
      <div className="field-row">
        <label className="field-label">
          path_mappings <HelpIcon help={helpFor('path_mappings')} />
        </label>
        <table className="mapping-table">
          <thead>
            <tr><th>Jellyfin path</th><th>Local path</th><th /></tr>
          </thead>
          <tbody>
            {mappings.map((m, idx) => (
              <tr key={idx}>
                <td>{m.jellyfin}</td>
                <td>{m.local}</td>
                <td>
                  <button className="btn rating-arrow" onClick={() => {
                    const next = mappings.slice();
                    next.splice(idx, 1);
                    patchMappings(next);
                  }}>✕</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="mapping-add">
          <input
            className="login-input config-input"
            type="text"
            value={newJellyfin}
            placeholder="/media (Jellyfin)"
            onChange={(e) => setNewJellyfin(e.target.value)}
          />
          <input
            className="login-input config-input"
            type="text"
            value={newLocal}
            placeholder="/media (local)"
            onChange={(e) => setNewLocal(e.target.value)}
          />
          <button className="btn" onClick={addMapping}>Add</button>
        </div>
      </div>
      {jellyfinKey?.locked && (
        <div className="muted field-hint">
          Jellyfin API key: set by environment variable <code>{jellyfinKey.env_var}</code>
        </div>
      )}
      {err && <div className="field-msg err">{err}</div>}
      {secrets && !secrets.encrypted_at_rest && (
        <div className="warn-banner">
          ⚠ Secrets are stored in plaintext. Set <code>FINMETA_SECRET_KEY</code> to enable encryption at rest.
        </div>
      )}
    </div>
  );
}

export default function SettingsPage({ onBack, initialTab, initialOverlayLibrary }: Props) {
  const [tab, setTab] = useState<Tab>(initialTab ?? 'general');
  // S9d2-fix2 step 1: remount OverlaysEditor when the deep link changes.
  const [overlayKey, setOverlayKey] = useState(0);
  // S9d2-fix2 step 1 (+ S9d4c step 4: also set from PreviewPane's "Edit
  // overlay profile" link): library preselected via a deep link.
  const [overlayLib, setOverlayLib] = useState<string | null>(initialOverlayLibrary ?? null);


  const [secrets, setSecrets] = useState<SecretsResp | null>(null);
  const [help, setHelp] = useState<HelpResp | null>(null);
  const [configYaml, setConfigYaml] = useState('');
  const [configMsg, setConfigMsg] = useState<string | null>(null);
  const [configErr, setConfigErr] = useState<string | null>(null);
  const [secretErr, setSecretErr] = useState<string | null>(null);

  const [providers, setProviders] = useState<Provider[]>([]);
  const [configParsed, setConfigParsed] = useState<any>(null);
  const [ratingSources, setRatingSources] = useState<RatingSourcesResp | null>(null);

  const load = useCallback(() => {
    getSecrets().then(setSecrets).catch(() => {});
    getHelp().then(setHelp).catch(() => {});
    getConfig().then((d) => { setConfigYaml(d.yaml); setConfigParsed(d.parsed); }).catch(() => {});
    getProviders().then((d) => setProviders(d.providers)).catch(() => {});
    getRatingSources().then(setRatingSources).catch(() => {});
  }, []);

  useEffect(() => { load(); }, [load]);

  const saveSecret = async (name: string, value: string): Promise<boolean> => {
    try {
      await putSecret(name, value);
      load();
      return true;
    } catch (e: any) {
      setSecretErr(e.message ?? 'Save failed');
      return false;
    }
  };

  const removeSecret = async (name: string) => {
    setSecretErr(null);
    try {
      await deleteSecret(name);
      load();
    } catch (e: any) {
      setSecretErr(e.message ?? 'Clear failed');
    }
  };

  const saveConfig = async () => {
    setConfigMsg(null);
    setConfigErr(null);
    try {
      await putConfig(configYaml);
      setConfigMsg('Config saved');
      setTimeout(() => setConfigMsg(null), 3000);
    } catch (e: any) {
      setConfigErr(e.message);
    }
  };

  // Edit one config field by dotted path; re-serializes the parsed object to
  // YAML and saves through the existing PUT /api/config hot-reload path.
  const saveConfigPatch = async (pathParts: string[], value: any) => {
    if (!configParsed) return;
    const updated = patchParsed(configParsed, pathParts, value);
    try {
      const yamlText = YAML.dump(updated, { noRefs: true });
      await putConfig(yamlText);
      setConfigYaml(yamlText);
      setConfigParsed(updated);
    } catch (e: any) {
      throw e; // caller decides how to surface the error
    }
  };

  const secret = (name: string): SecretView | null => secrets?.secrets[name] ?? null;
  const helpFor = (key: string): string | undefined =>
    help?.help[key]?.long;

  return (
    <div className="settings-page">
      <header className="settings-header">
        <button className="btn settings-back" onClick={onBack}>← Back</button>
        <h2>Settings</h2>
        <div className="settings-actions">
          <button className="btn" onClick={() => { logout().then(onBack); }}>Log out</button>
        </div>
      </header>

      <nav className="settings-tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`settings-tab ${t.id === tab ? 'active' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <div className="settings-body">
        {tab === 'general' && configParsed && (
          <GeneralTab
            configParsed={configParsed}
            secrets={secrets}
            helpFor={helpFor}
            saveConfigPatch={saveConfigPatch}
          />
        )}

        {tab === 'local' && (
          <div className="settings-section">
            <h3>Local services</h3>
            {providers.filter((p) => p.kind === 'local').map((p) => (
              <div key={p.name} className="service-card">
                <div className="service-head">
                  <strong>{p.label}</strong> <HelpIcon help={p.long} />
                  <TestButton service={p.name} />
                </div>
                {p.requires_key && p.secret && secret(p.secret) && (
                  <SecretField
                    secret={secret(p.secret)!}
                    onSave={(v) => saveSecret(p.secret!, v)}
                    onDelete={() => removeSecret(p.secret!)}
                  />
                )}
                {/* Local assets: show the configured asset dir + status */}
                {p.name === 'local' && configParsed && (
                  <div className="field-row">
                    <ConfigField
                      label="Asset directory"
                      help={helpFor('sources.local.path')}
                      value={configParsed.sources?.local?.path ?? ''}
                      onChange={(v) => void saveConfigPatch(['sources','local','path'], v).catch(() => {})}
                    />
                    <span className={`test-result ${configParsed.sources?.local?.enabled ? 'ok' : 'err'}`} style={{ alignSelf: 'center' }}>
                      {configParsed.sources?.local?.enabled ? '✓ enabled' : '✗ disabled (sources.local.enabled)'}
                    </span>
                  </div>
                )}
                {/* Jellyfin connection fields (S9b3 step 1) */}
                {p.name === 'jellyfin' && configParsed && (
                  <>
                    <ConfigField
                      label="URL"
                      help={helpFor('server.url')}
                      value={configParsed.server?.url ?? ''}
                      onChange={(v) => void saveConfigPatch(['server','url'], v).catch(() => {})}
                    />
                    <ConfigField
                      label="Base path"
                      help={helpFor('server.base_path')}
                      value={configParsed.server?.base_path ?? ''}
                      placeholder="/jellyfin (optional)"
                      onChange={(v) => void saveConfigPatch(['server','base_path'], v || null).catch(() => {})}
                    />
                    <ConfigField
                      label="Verify SSL"
                      help={helpFor('server.verify_ssl')}
                      type="checkbox"
                      value={configParsed.server?.verify_ssl ?? true}
                      onChange={(v) => void saveConfigPatch(['server','verify_ssl'], v === 'true').catch(() => {})}
                    />
                    <ConfigField
                      label="Timeout (s)"
                      help={helpFor('server.timeout')}
                      type="number"
                      value={configParsed.server?.timeout ?? 30}
                      onChange={(v) => void saveConfigPatch(['server','timeout'], Number(v)).catch(() => {})}
                    />
                    <WebhookSection />
                  </>
                )}
                {p.rate_limit && (
                  <div className="field-hint muted">{p.rate_limit}</div>
                )}
                {p.coming_later && (
                  <div className="field-hint muted">Coming later</div>
                )}
              </div>
            ))}
            {/* Sonarr / Radarr instances (S9f1a-fix) */}
            {(['sonarr', 'radarr'] as const).map((app) => (
              <MediaAppInstances
                key={app}
                app={app}
                label={app === 'sonarr' ? 'Sonarr' : 'Radarr'}
                helpKey={helpFor(`media_apps.${app}`)}
                instances={(configParsed?.media_apps?.[app] ?? []) as ArrInstance[]}
                validKey={(name: string) => {
                  const inst = (configParsed?.media_apps?.[app] ?? []).find((i: any) => i.api_key === name);
                  if (inst && typeof inst.api_key_valid === 'boolean') return inst.api_key_valid;
                  return Boolean(secrets?.secrets[name]);
                }}
                onSaveList={async (list) => {
                  try {
                    // S9d2-fix2 step A: the form's api_key is a literal KEY value.
                    // Store it in the secrets store under <kind>_<name>_api_key and
                    // write only that NAME into the config — never the key itself.
                    const stored: typeof list = [];
                    for (const inst of list) {
                      const keyVal = (inst.api_key ?? '').trim();
                      if (!keyVal) { stored.push(inst); continue; } // blank = keep existing secret name
                      const slug = inst.name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'instance';
                      const secretName = `${app}_${slug}_api_key`;
                      await putSecret(secretName, keyVal);
                      stored.push({ ...inst, api_key: secretName });
                    }
                    await saveConfigPatch(['media_apps', app], stored);
                    return null;
                  } catch (e: any) {
                    return e.message ?? 'Save failed';
                  }
                }}
              />
            ))}
          </div>
        )}

        {tab === 'external' && (
          <div className="settings-section">
            <h3>External services</h3>
            {providers.filter((p) => p.kind === 'external').map((p) => (
              <div key={p.name} className={`service-card ${!p.enabled ? 'service-disabled' : ''}`}>
                <div className="service-head">
                  <strong>{p.label}</strong> <HelpIcon help={p.long} />
                  <TestButton service={p.name} />
                </div>
                {/* Enabled toggle (S9b3 step 2). TMDb excluded: it is the
                    zero-config default source and other code paths assume
                    it is always available (SPEC §C). */}
                {configParsed && ['mdblist', 'imdb_datasets', 'fanart', 'omdb'].includes(p.name) && (
                  <ConfigField
                    label="Enabled"
                    help={helpFor(`sources.${p.name}.enabled`)}
                    type="checkbox"
                    value={configParsed.sources?.[p.name]?.enabled ?? false}
                    onChange={(v) => void saveConfigPatch(['sources', p.name, 'enabled'], v === 'true').catch(() => {})}
                  />
                )}
                {p.requires_key && p.secret && secret(p.secret) && (
                  <SecretField
                    secret={secret(p.secret)!}
                    onSave={(v) => saveSecret(p.secret!, v)}
                    onDelete={() => removeSecret(p.secret!)}
                  />
                )}
                {p.get_key_url && (
                  <div className="field-hint muted">
                    <a href={p.get_key_url} target="_blank" rel="noreferrer" className="link">
                      Get a {p.label} API key
                    </a>
                  </div>
                )}
                {p.rate_limit && (
                  <div className="field-hint muted">{p.rate_limit}</div>
                )}
                {p.coming_later && (
                  <div className="field-hint muted">Coming later</div>
                )}
              </div>
            ))}
          </div>
        )}

        {tab === 'secrets' && (
          <div className="settings-section">
            <h3>Secrets</h3>
            <div className="field-hint muted" style={{ marginBottom: 12 }}>
              API keys are stored encrypted at rest in the Settings store. A value set via its
              environment variable takes precedence and locks the field here. Values are never
              shown back — only a masked hint.
            </div>
            {secretErr && <div className="field-msg err" style={{ marginBottom: 12 }}>{secretErr}</div>}
            {!secrets && <div className="field-hint muted">Loading…</div>}
            {secrets && Object.values(secrets.secrets).map((sec) => (
              <div key={sec.name} className="service-card secret-row-card">
                <div className="service-head">
                  <strong>{sec.short}</strong> <HelpIcon help={sec.help} />
                  <span className={`test-result ${sec.configured ? 'ok' : 'err'}`}>
                    {sec.configured ? '✓ configured' : '✗ not set'}
                  </span>
                </div>
                <div className="field-hint muted">
                  name: <code>{sec.name}</code> · source:{' '}
                  {sec.source === 'env' ? `environment variable ${sec.env_var}`
                    : sec.source === 'ui' ? 'Settings store' : 'none'}
                </div>
                <SecretField
                  secret={sec}
                  onSave={(v) => saveSecret(sec.name, v)}
                  onDelete={() => removeSecret(sec.name)}
                />
              </div>
            ))}
          </div>
        )}

        {tab === 'ratings' && (
          <div className="settings-section">
            <h3>Rating sources</h3>
            <div className="field-hint muted" style={{ marginBottom: 12 }}>
              For each rating field, providers are tried in order; the first one with a value wins.
              Reorder with the arrows. Disabled or unconfigured providers are greyed out.
              Scales: IMDb/TMDb/Trakt 0–10 · Rotten Tomatoes/Metacritic 0–100 · Letterboxd 0–5.
            </div>
            {ratingSources?.fields.map((f) => (
              <div key={f.name} className="service-card">
                <div className="service-head">
                  <strong>{f.label}</strong>
                  <HelpIcon help={`Canonical scale for ${f.label}: see field help.`} />
                </div>
                {f.providers.length === 0 && (
                  <div className="field-hint muted">No providers configured for this field.</div>
                )}
                {f.providers.map((prov, idx) => {
                  const enabled = ratingSources?.enabled_by_name[prov] ?? false;
                  return (
                    <div key={prov} className={`rating-row ${!enabled ? 'rating-disabled' : ''}`}>
                      <span className="rating-order">{idx + 1}</span>
                      <span className="rating-name">{prov}</span>
                      {!enabled && <span className="rating-hint">disabled / unconfigured</span>}
                      <span className="rating-arrows">
                        <button
                          className="btn rating-arrow"
                          disabled={idx === 0}
                          onClick={() => {
                            const arr = [...f.providers];
                            [arr[idx - 1], arr[idx]] = [arr[idx], arr[idx - 1]];
                            saveConfigPatch(['sources', 'field_sources', f.name], arr);
                          }}
                        >↑</button>
                        <button
                          className="btn rating-arrow"
                          disabled={idx === f.providers.length - 1}
                          onClick={() => {
                            const arr = [...f.providers];
                            [arr[idx + 1], arr[idx]] = [arr[idx], arr[idx + 1]];
                            saveConfigPatch(['sources', 'field_sources', f.name], arr);
                          }}
                        >↓</button>
                      </span>
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
        )}

        {tab === 'overlays' && (
          <div key={overlayKey}><OverlaysEditor initialLibrary={overlayLib} /></div>
        )}

        {tab === 'libraries' && <ProfilesPanel onEditInOverlays={(lib) => {
          // S9d2-fix2 step 1: open the Overlays tab with this library selected.
          setOverlayLib(lib);
          setTab('overlays');
          setOverlayKey((k) => k + 1);
        }} />}

        {tab === 'schedules' && <SchedulesPanel />}

        {tab === 'trending' && <TrendingPanel />}
        {tab === 'collections' && <CollectionsPanel />}

        {tab === 'advanced' && (
          <div className="settings-section">
            <h3>Advanced — raw YAML</h3>
            <textarea
              className="yaml-editor"
              value={configYaml}
              onChange={(e) => setConfigYaml(e.target.value)}
              rows={24}
              spellCheck={false}
            />
            <div className="yaml-actions">
              <button className="btn primary" onClick={saveConfig}>Save config</button>
              {configMsg && <span className="field-msg ok">{configMsg}</span>}
              {configErr && <span className="field-msg err">{configErr}</span>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
