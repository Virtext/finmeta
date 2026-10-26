// S9d2: bulk apply for a whole library — plan first (counts incl. format_rules),
// confirm, then apply as a normal run (existing one-run-at-a-time + undo flow).
// dry_run=true blocks Apply exactly like the per-item apply button.
// Lives in the library sidebar (not per-item). 2026-10-23: no longer reuses
// an eagerly-loaded item list (that list is Jellyfin-only now, see
// LibraryBrowser.tsx) -- "Plan library" builds and shows its own counts via
// the run log instead of a live summary row computed up front.
//
// 2026-10-25: run state + polling moved up to App.tsx (per-library, keyed by
// name) instead of living here. Switching library tabs flips `loading` on
// the parent, which unmounts this component -- it used to own the poll
// timer itself, so navigating away from an in-flight run silently stopped
// showing it (the backend job kept running regardless; only the UI lost
// track of it). This component is now purely presentational.
import type { JobResp } from '../types';

export interface LibraryRunState {
  job: JobResp | null;
  action: string;
  running: boolean;
  error: string | null;
}

export const EMPTY_LIBRARY_RUN: LibraryRunState = { job: null, action: '', running: false, error: null };

interface Props {
  library: string;
  dryRun: boolean;
  runState: LibraryRunState;
  onStart: (kind: 'plan' | 'apply') => void;
}

const fmtTs = (s: string) => s.replace('T', ' ').replace('Z', '');

export default function BulkApplyPanel({ library, dryRun, runState, onStart }: Props) {
  const { job, action, running, error } = runState;
  const logTail = job ? job.log.slice(-50) : [];

  return (
    <section className="run-panel">
      <div className="section-head">
        <h3>Bulk apply — {library}</h3>
        <div className="run-buttons">
          <button className="btn" disabled={running} onClick={() => onStart('plan')}>Plan library</button>
          <button className="btn primary" disabled={running || dryRun}
                  title={dryRun ? 'dry_run is true — set it false in config to apply' : undefined}
                  onClick={() => { if (window.confirm(`Apply “${library}” now? Use “Plan library” first to see what will change.`)) onStart('apply'); }}>
            Apply library
          </button>
        </div>
      </div>

      <div className="muted pad">
        “Plan library” builds the full plan for this library (every item — the one slow,
        on-demand operation this still has to be) and shows counts in the run log below.
      </div>

      {dryRun && <div className="muted pad">dry_run is ON — Apply is disabled until config.dry_run=false.</div>}
      {running && (
        <div className="muted pad">
          Running in the background — this keeps going even if you switch to another library tab.
        </div>
      )}
      {error && <div className="error pad">{error}</div>}

      {job && (
        <div className={`run-result ${job.status}`}>
          <div className="run-statusline">
            <span className={`run-status ${job.status}`}>{job.status}</span>
            <span className="muted">
              {action} · {job.files_written} written · {job.files_unchanged} unchanged · {job.api_calls} api calls
            </span>
          </div>
          {logTail.length > 0 && (
            <div className="run-log">
              {logTail.map((line, i) => (
                <div key={i} className={`run-log-line lvl-${line.level}`}>
                  <span className="run-log-ts muted">{fmtTs(line.ts)}</span>
                  <span className="run-log-level">{line.level}</span>
                  <span className="run-log-msg">{line.message}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
