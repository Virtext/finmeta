import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  clearQueuedRuns, dropQueuedItem, dropQueuedLibrary, getActivity, getHelp, getJob, retryRun,
} from '../api';
import type { ActivityResp, ActivityRun, HelpResp, JobResp } from '../types';
import HelpIcon from './HelpIcon';

interface Props {
  onBack: () => void;
  /** A scan row links to its findings. */
  onOpenHealth?: () => void;
}

const PAGE_SIZE = 50;
const POLL_MS = 3000;
const RUNNING = new Set(['running']);

const ACTION_LABELS: Record<string, string> = {
  plan: 'Plan', apply: 'Apply', scan: 'Scan',
};

/** "lib:Filmer" -> "Filmer", "item:abc" -> "one item", "all" -> "all libraries" */
function scopeLabel(scope: string | null): string {
  if (!scope || scope === 'all') return 'all libraries';
  if (scope.startsWith('lib:')) return scope.slice(4);
  if (scope.startsWith('item:')) return 'one item';
  return scope;
}

function duration(run: ActivityRun): string {
  if (run.duration_s == null) {
    return RUNNING.has(run.status) ? 'running…' : '—';
  }
  const s = Math.round(run.duration_s);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${String(s % 60).padStart(2, '0')}s`;
}

function elapsed(startedAt: string | null): string {
  if (!startedAt) return '';
  const started = Date.parse(startedAt);
  if (Number.isNaN(started)) return '';
  const s = Math.max(0, Math.round((Date.now() - started) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
}

/** Only the counters that mean something for that action. */
function counters(run: ActivityRun): string {
  if (run.action === 'apply') {
    return `${run.files_written} written · ${run.files_unchanged} unchanged`;
  }
  if (run.action === 'scan') {
    return run.tier && run.tier >= 2 ? 'deep scan' : 'quick scan';
  }
  return '';
}

function when(iso: string | null): string {
  if (!iso) return '—';
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : new Date(t).toLocaleString();
}

export default function ActivityPage({ onBack, onOpenHealth }: Props) {
  const [data, setData] = useState<ActivityResp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState('');
  const [actionFilter, setActionFilter] = useState('');
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [logs, setLogs] = useState<Record<string, JobResp>>({});
  const [msg, setMsg] = useState<string | null>(null);
  const [help, setHelp] = useState<HelpResp | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    getHelp().then(setHelp).catch(() => { /* help is additive */ });
  }, []);

  const load = useCallback(async () => {
    try {
      const d = await getActivity({
        limit,
        status: statusFilter || undefined,
        action: actionFilter || undefined,
      });
      setData(d);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [limit, statusFilter, actionFilter]);

  // Poll while the page is open: a scheduled run or a webhook apply can start
  // without this browser doing anything at all.
  useEffect(() => {
    void load();
    timer.current = window.setInterval(() => { void load(); }, POLL_MS);
    return () => { if (timer.current) window.clearInterval(timer.current); };
  }, [load]);

  const openLog = useCallback(async (runId: string) => {
    if (expanded === runId) { setExpanded(null); return; }
    setExpanded(runId);
    try {
      const job = await getJob(runId);
      setLogs((prev) => ({ ...prev, [runId]: job }));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [expanded]);

  const doRetry = useCallback(async (run: ActivityRun) => {
    setMsg(null);
    try {
      const r = await retryRun(run.run_id);
      setMsg(`Retrying ${ACTION_LABELS[r.action] ?? r.action} on ${scopeLabel(r.scope)} — run ${r.job_id}.`);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }, [load]);

  const dropItem = useCallback(async (itemId: string) => {
    try { await dropQueuedItem(itemId); await load(); }
    catch (e) { setError((e as Error).message); }
  }, [load]);

  const dropLibrary = useCallback(async (action: string, library: string, tier: number) => {
    try { await dropQueuedLibrary(action, library, tier); await load(); }
    catch (e) { setError((e as Error).message); }
  }, [load]);

  // Operator-requested 2026-11-10: a bulk "fix all" can queue hundreds of
  // single-item runs behind whatever is currently running -- dropping
  // them one chip at a time is not realistic at that scale.
  const [clearing, setClearing] = useState(false);
  const clearAllQueued = useCallback(async () => {
    if (!window.confirm(`Drop all ${queuedCount} queued run${queuedCount === 1 ? '' : 's'}? The run already in progress keeps going.`)) return;
    setClearing(true);
    try {
      const r = await clearQueuedRuns();
      setMsg(`Dropped ${r.dropped} queued run${r.dropped === 1 ? '' : 's'}.`);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setClearing(false);
    }
  }, [load]);

  const active = data?.active ?? null;
  const queuedCount = (data?.queued.items.length ?? 0)
    + (data?.queued.libraries.length ?? 0);
  const failing = useMemo(
    () => (data?.runs ?? []).filter((r) => r.status === 'error').length,
    [data],
  );
  const helpFor = (key: string) => help?.help[key]?.long;

  return (
    <div className="settings-page">
      <header className="settings-header">
        <button className="btn settings-back" onClick={onBack}>← Back</button>
        <h2>Activity <HelpIcon help={helpFor('feature.page.activity')} /></h2>
        <a className="btn" href="/api/logs/export" download>Download log</a>
      </header>

      <div className="health-bar">
        <div className="activity-now">
          {active ? (
            <>
              <span className="spinner-dot" />
              <strong>{ACTION_LABELS[active.action ?? ''] ?? active.action ?? 'Run'}</strong>
              {' '}on {scopeLabel(active.scope)}
              <span className="muted"> · {elapsed(active.started_at)}</span>
            </>
          ) : (
            <span className="muted">Idle — nothing running.</span>
          )}
        </div>
        {queuedCount > 0 && (
          <div className="activity-queue">
            <span className="muted">Queued ({queuedCount}):</span>
            {data?.queued.libraries.map((q) => (
              <span className="activity-chip" key={`${q.action}-${q.library}-${q.tier}`}>
                {ACTION_LABELS[q.action] ?? q.action} · {q.library}
                {q.action === 'scan' && q.tier >= 2 ? ' (deep)' : ''}
                <button
                  className="activity-chip-x"
                  title="Drop this queued run"
                  onClick={() => dropLibrary(q.action, q.library, q.tier)}
                >×</button>
              </span>
            ))}
            {/* A bulk fix can queue hundreds of single-item runs -- one
                DOM chip + poll-driven re-render per item is itself what
                made this page feel sluggish at that scale (operator-
                reported). Past a small count, stop rendering them
                individually; "Cancel all queued" is the real way out. */}
            {(data?.queued.items ?? []).slice(0, 15).map((id) => (
              <span className="activity-chip" key={id}>
                one item
                <button
                  className="activity-chip-x"
                  title="Drop this queued item"
                  onClick={() => dropItem(id)}
                >×</button>
              </span>
            ))}
            {(data?.queued.items.length ?? 0) > 15 && (
              <span className="muted">+{data!.queued.items.length - 15} more</span>
            )}
            <button className="btn btn-ghost" disabled={clearing} onClick={() => void clearAllQueued()}>
              {clearing ? 'Cancelling…' : 'Cancel all queued'}
            </button>
          </div>
        )}
        {data?.dry_run && <div className="dryflag on">DRY RUN</div>}
      </div>

      {data && data.schedules.length > 0 && (
        <div className="activity-schedules">
          <span className="muted">Next scheduled:</span>
          {data.schedules.slice(0, 4).map((s) => (
            <span className="activity-sched" key={s.id}>
              {s.name} <span className="muted">{when(s.next_run_at)}</span>
            </span>
          ))}
          {data.schedules.length > 4 && (
            <span className="muted">+{data.schedules.length - 4} more</span>
          )}
        </div>
      )}

      {error && <div className="field-msg err health-msg">{error}</div>}
      {msg && <div className="field-msg ok health-msg">{msg}</div>}

      <div className="settings-body">
        {loading && <p className="muted">Loading…</p>}

        {data && (
          <>
            <div className="activity-filters">
              <label className="health-field">
                <span className="muted">Action</span>
                <select value={actionFilter} onChange={(e) => setActionFilter(e.target.value)}>
                  <option value="">any</option>
                  <option value="plan">plan</option>
                  <option value="apply">apply</option>
                  <option value="scan">scan</option>
                </select>
              </label>
              <label className="health-field">
                <span className="muted">Status</span>
                <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                  <option value="">any</option>
                  <option value="ok">ok</option>
                  <option value="error">error</option>
                  <option value="refused">refused</option>
                  <option value="running">running</option>
                </select>
              </label>
              <span className="muted">
                {data.total} run(s){failing > 0 ? ` · ${failing} failed on this page` : ''}
              </span>
            </div>

            {data.runs.length === 0 && (
              <p className="muted">
                No runs recorded yet. Plan, apply and scan runs all show up here —
                including ones started by a schedule or by Jellyfin's webhook.
              </p>
            )}

            {data.runs.length > 0 && (
              <table className="meta-table activity-table">
                <thead>
                  <tr>
                    <th>Status</th><th>What</th><th>Started</th><th>Took</th>
                    <th>Result</th><th></th>
                  </tr>
                </thead>
                <tbody>
                  {data.runs.map((run) => (
                    // The key belongs on the fragment: a run can render up to
                    // three sibling rows (the run, its last error, its log).
                    <Fragment key={run.run_id}>
                      <tr className="activity-row">
                        <td>
                          <span className={`activity-status st-${run.status}`}>
                            {run.status}
                          </span>
                        </td>
                        <td>
                          <strong>
                            {run.action ? (ACTION_LABELS[run.action] ?? run.action) : 'unknown'}
                          </strong>
                          <span className="muted"> · {scopeLabel(run.scope)}</span>
                          {run.action === 'scan' && onOpenHealth && (
                            <button className="btn btn-ghost activity-link"
                                    onClick={onOpenHealth}>findings →</button>
                          )}
                        </td>
                        <td className="muted">{when(run.started_at)}</td>
                        <td className="muted">{duration(run)}</td>
                        <td>
                          <span className="muted">{counters(run)}</span>
                          {run.errors > 0 && (
                            <span className="activity-count err">{run.errors} error</span>
                          )}
                          {run.warnings > 0 && (
                            <span className="activity-count warn">{run.warnings} warn</span>
                          )}
                        </td>
                        <td className="activity-actions">
                          <button className="btn btn-ghost" onClick={() => openLog(run.run_id)}>
                            {expanded === run.run_id ? 'Hide log' : 'Log'}
                          </button>
                          <button
                            className="btn"
                            disabled={!run.retryable || !!active
                                      || (run.action === 'apply' && data.dry_run)}
                            title={!run.retryable
                              ? 'This run predates action tracking, so there is nothing to resubmit'
                              : active ? 'A run is already in progress'
                              : (run.action === 'apply' && data.dry_run)
                                ? 'dry_run is true — set it false in config to apply'
                                : 'Run this again with the same scope'}
                            onClick={() => doRetry(run)}
                          >
                            Retry
                          </button>
                        </td>
                      </tr>
                      {run.last_error && expanded !== run.run_id && (
                        <tr className="activity-errline">
                          <td />
                          <td colSpan={5} className="field-msg err">{run.last_error}</td>
                        </tr>
                      )}
                      {expanded === run.run_id && (
                        <tr>
                          <td />
                          <td colSpan={5}>
                            <div className="activity-log">
                              {(logs[run.run_id]?.log ?? []).slice().reverse().map((l, i) => (
                                <div className={`activity-log-line lvl-${l.level}`} key={i}>
                                  <span className="muted">{l.ts}</span> {l.message}
                                </div>
                              ))}
                              {!logs[run.run_id] && <span className="muted">Loading log…</span>}
                              {logs[run.run_id]?.log?.length === 0 && (
                                <span className="muted">No log entries.</span>
                              )}
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            )}

            {data.has_more && (
              <button className="btn load-more-btn"
                      onClick={() => setLimit(limit + PAGE_SIZE)}>
                Load more ({data.runs.length} of {data.total})
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}
