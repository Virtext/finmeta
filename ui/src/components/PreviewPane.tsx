import {
  findFile, overlayProxyUrl, proxyUrl, reshuffleItem, skipItemKind, unskipItemKind,
} from '../api';
import type { FileWrite, ItemLightView, PlanResp } from '../types';
import { FileStatusBadge, StatusBadge } from '../badges';
import CandidatePicker from './CandidatePicker';

interface Props {
  item: ItemLightView | null;
  plan: PlanResp | null;
  loading: boolean;
  error: string | null;
  dryRun: boolean;
  library: string;
  onCandidateSelected: () => void;
  /** S9d4b step 4: refresh the plan after skip/unskip (server is truth). */
  onSkipChanged?: () => void;
  /** S9d4b step 6: apply only this item. */
  onApplyItem?: () => void;
  itemApplyJob?: import('../types').JobResp | null;
  itemApplyRunning?: boolean;
  itemApplyError?: string | null;
}

const IMG_KINDS = new Set(['poster', 'backdrop', 'logo', 'landscape']);

function posterUrl(files: FileWrite[], kind: string): string | null {
  const f = findFile(files, kind);
  return f ? proxyUrl(f.kind, f.source_ref) : null;
}

// The poster card must show what Apply actually writes (the overlaid
// render), not the plain candidate proxyUrl() above would fetch --
// source_ref deliberately stays pointed at the pre-overlay original (O7).
// Falls back to the plain candidate if the overlay cache entry is missing
// (e.g. a plan built before this existed, or since pruned) via onError.
function posterOverlayUrl(files: FileWrite[]): { src: string; fallback: string } | null {
  const f = files.find((x) => x.kind === 'poster');
  if (!f) return null;
  return { src: overlayProxyUrl(f.content_sha256), fallback: proxyUrl(f.kind, f.source_ref) };
}

export default function PreviewPane(p: Props) {
  if (!p.item) {
    return (
      <main className="preview empty">
        <div className="placeholder">
          <img src="/finmeta-icon.svg" alt="" className="placeholder-logo" />
          <p>Select an item to see its preview.</p>
        </div>
      </main>
    );
  }

  const files = p.plan?.plan.files ?? [];
  const backdrop = posterUrl(files, 'backdrop');
  const poster = posterOverlayUrl(files);
  const logo = posterUrl(files, 'logo');
  const themeSong = findFile(files, 'theme_song');

  return (
    <main className="preview" style={backdrop ? { backgroundImage: `url(${backdrop})` } : undefined}>
      <div className="preview-scroller">
        <div className="preview-card-row">
          {/* Jellyfin-style poster card (planned / overlaid poster). */}
          <div className="poster-card">
            {poster ? (
              <img
                className="poster-img" src={poster.src} alt={`${p.item.item_name} poster`}
                onError={(e) => {
                  if (e.currentTarget.src !== poster.fallback) e.currentTarget.src = poster.fallback;
                }}
              />
            ) : (
              <div className="poster-img poster-fallback">no poster</div>
            )}
            {p.plan && (
              <div className="poster-chip">
                <StatusBadge status={p.plan.status} />
              </div>
            )}
            <div className="poster-title">{p.item.item_name}</div>
          </div>

          <div className="preview-info">
            <h1 className="preview-title">{p.item.item_name}</h1>
            <div className="preview-sub muted">
              {p.item.media_type}
              {p.plan?.tmdb_id ? ` · TMDb ${p.plan.tmdb_id}` : ''}
              {p.plan?.matched_by ? ` · matched by ${p.plan.matched_by}` : ''}
              {/* S9g1/K7a: TV show status from Sonarr, when detected. */}
              {p.plan?.plan?.tv_status ? ` · ${p.plan.plan.tv_status.replace(/_/g, ' ')}` : ''}
            </div>

            {logo && <img className="logo-img" src={logo} alt="logo" />}

            {themeSong && (
              <div className="theme-song">
                <span className="muted">Theme song</span>
                <audio controls src={proxyUrl('theme_song', themeSong.source_ref)} preload="none" />
              </div>
            )}

            {p.plan && (
              <div className="preview-diff muted">
                {p.plan.diff.create} create · {p.plan.diff.update} update · {p.plan.diff.unchanged} unchanged
              </div>
            )}

            {/* S9d4b step 6: apply only this item (scope item:<id>). */}
            <div className="preview-actions">
              <button
                className="btn primary"
                disabled={p.dryRun || !p.plan || p.plan.diff.create + p.plan.diff.update === 0}
                title={p.dryRun ? 'dry_run is true — set it false in config to apply' : 'Apply only this item (scope item:<id>)'}
                onClick={() => p.onApplyItem?.()}>
                {p.itemApplyRunning ? 'Applying…' : 'Apply this item'}
              </button>
            </div>

            {/* S9d4b step 6: item-apply status. */}
            {p.itemApplyError && <div className="error pad">{p.itemApplyError}</div>}
            {p.itemApplyJob && (
              <div className={`run-result ${p.itemApplyJob.status}`}>
                <span className={`run-status ${p.itemApplyJob.status}`}>{p.itemApplyJob.status}</span>
                <span className="muted">
                  {' '}{p.itemApplyJob.files_written} written · {p.itemApplyJob.files_unchanged} unchanged
                </span>
              </div>
            )}
          </div>
        </div>

        {/* Before / after metadata table — straight from the WritePlan. */}
        {p.loading && <div className="muted pad">Building plan…</div>}
        {p.error && <div className="error pad">{p.error}</div>}

        {p.plan && (
          <table className="meta-table">
            <thead>
              <tr><th>File</th><th>Kind</th><th>Status</th><th>Target path</th><th></th></tr>
            </thead>
            <tbody>
              {files.map((f, i) => (
                <tr key={`${f.kind}-${i}`} className={f.status}>
                  <td className="meta-kind">{IMG_KINDS.has(f.kind) ? '🖼' : f.kind === 'theme_song' ? '🎵' : '📄'} {f.kind}</td>
                  <td><FileStatusBadge status={f.status} /></td>
                  <td className="meta-path" title={f.target_path}>{f.target_path}</td>
                  <td className="meta-src muted" title={f.source_ref}>{f.source_ref}</td>
                  {/* S9d4a: reshuffle — bump the per-kind counter so the
                      deterministic random_top_n preset picks a new candidate.
                      S9d5: also breaks this kind's artwork lock ("re-pick
                      once" — once an item has been applied, plan/apply never
                      re-queries sources for it until this is clicked). */}
                  <td className="meta-actions">
                    {IMG_KINDS.has(f.kind) && (
                      <button
                        className="btn"
                        title="Pick another candidate for this image type and break its artwork lock, if any (works with the random_top_n preset)"
                        onClick={() => {
                          void reshuffleItem(p.item!.item_id, [f.kind])
                            .then(() => p.onCandidateSelected())
                            .catch(() => {});
                        }}>
                        ↻ Re-pick
                      </button>
                    )}
                    {/* S9d4b step 4: skip this file type for the item. */}
                    <button
                      className="btn btn-ghost"
                      title="Skip this file type — it will no longer be written for this item"
                      onClick={() => {
                        void skipItemKind(p.item!.item_id, f.kind)
                          .then(() => p.onSkipChanged?.())
                          .catch(() => {});
                      }}>
                      Skip
                    </button>
                  </td>
                </tr>
              ))}
              {/* S9d4b step 4: skipped kinds — dimmed rows with Unskip.
                  Server response carries the `skipped` list (truth source). */}
              {(p.plan.skipped ?? []).map((kind) => (
                <tr key={`skipped-${kind}`} className="meta-row-skipped">
                  <td className="meta-kind muted">{IMG_KINDS.has(kind) ? '🖼' : kind === 'theme_song' ? '🎵' : '📄'} {kind}</td>
                  <td><span className="badge badge-skipped">skipped</span></td>
                  <td className="meta-path muted" colSpan={2}>—</td>
                  <td className="meta-actions">
                    <button
                      className="btn btn-ghost"
                      title="Restore this file type for the item"
                      onClick={() => {
                        void unskipItemKind(p.item!.item_id, kind)
                          .then(() => p.onSkipChanged?.())
                          .catch(() => {});
                      }}>
                      Unskip
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <CandidatePicker itemId={p.item.item_id} onSelected={p.onCandidateSelected} />
      </div>
    </main>
  );
}
