import { useEffect, useMemo, useState } from 'react';
import { getCandidates, selectCandidate } from '../api';
import type { Candidate, CandidatesResp } from '../types';

const IMAGE_KINDS = new Set([
  'poster', 'backdrop', 'logo', 'landscape', 'season_poster', 'episode_thumb',
]);

// 2026-10-23 (operator-reported slowness selecting a title): a popular
// title can have 50-100+ poster candidates from TMDb alone, and every
// rendered thumbnail is a live proxy fetch (not a cheap <img> -- see
// /api/proxy/{token}). Capping how many actually render, per source, keeps
// the initial set small; "Load more" reveals more of the SAME
// already-fetched list (no extra request, no backend change needed).
const INITIAL_VISIBLE_PER_SOURCE = 10;
const LOAD_MORE_STEP = 10;

function groupByKind(cands: Candidate[]): Record<string, Candidate[]> {
  const out: Record<string, Candidate[]> = {};
  for (const c of cands) {
    (out[c.kind] ??= []).push(c);
  }
  return out;
}

function groupBySource(cands: Candidate[]): [string, Candidate[]][] {
  const out: Record<string, Candidate[]> = {};
  for (const c of cands) {
    (out[c.source] ??= []).push(c);
  }
  return Object.entries(out);
}

interface Props {
  itemId: string;
  onSelected: () => void;   // refetch the plan after a selection
}

export default function CandidatePicker({ itemId, onSelected }: Props) {
  const [data, setData] = useState<CandidatesResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // "<kind>:<ref>"
  // How many of each (kind, source) group are rendered -- see constants above.
  const [visible, setVisible] = useState<Record<string, number>>({});

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    setVisible({});
    getCandidates(itemId)
      .then((d) => { if (alive) setData(d); })
      .catch((e) => { if (alive) setError(e.message); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [itemId]);

  const grouped = useMemo(() => (data ? groupByKind(data.candidates) : {}), [data]);

  async function pick(kind: string, ref: string | null) {
    setBusy(ref ? `${kind}:${ref}` : `${kind}:*`);
    setError(null);
    try {
      await selectCandidate(itemId, kind, ref);
      onSelected();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="candidates">
      <div className="section-head">
        <h3>Candidates</h3>
        {loading && <span className="muted">loading…</span>}
        {error && <span className="error">{error}</span>}
      </div>

      {!loading && data && data.candidates.length === 0 && (
        <div className="muted pad">No candidates available.</div>
      )}

      {Object.entries(grouped).map(([kind, list]) => (
        <div key={kind} className="cand-kind">
          <div className="cand-kind-head">
            <span style={{display:'inline-flex', alignItems:'center', gap:'8px'}}>
              <span className="kind-label">{kind}</span>
              {/* S9d5: locked kinds skip source resolution on plan/apply --
                  picking a candidate below still overrides it immediately;
                  Reshuffle also breaks the lock for future auto-selection. */}
              {data?.locked_kinds?.includes(kind) && (
                <span className="muted" title="Locked since the first successful apply — plan/apply won't auto-pick a new candidate for this on its own, but picking one below still works right away. Reshuffle also breaks the lock.">
                  🔒 locked
                </span>
              )}
            </span>
            {list[0]?.selected && list[0].ref !== null && (
              <button className="link" onClick={() => pick(kind, null)}>
                reset
              </button>
            )}
          </div>

          {IMAGE_KINDS.has(kind) ? (
            groupBySource(list).map(([source, srcList]) => {
              const key = `${kind}:${source}`;
              const shown = visible[key] ?? INITIAL_VISIBLE_PER_SOURCE;
              const slice = srcList.slice(0, shown);
              const remaining = srcList.length - slice.length;
              return (
                <div key={source} className="cand-source-group">
                  {srcList.length > INITIAL_VISIBLE_PER_SOURCE && (
                    <div className="muted cand-source-label">{source} ({srcList.length})</div>
                  )}
                  <div className="cand-grid">
                    {slice.map((c) => (
                      <button
                        key={c.ref}
                        className={`cand-thumb ${c.selected ? 'selected' : ''} ${busy === kind + ':' + c.ref ? 'busy' : ''}`}
                        onClick={() => pick(kind, c.ref)}
                        title={`${c.source}${c.lang ? ' · ' + c.lang : ''} (${c.width}x${c.height})`}
                      >
                        <img src={c.proxy_url} alt={c.kind} loading="lazy" />
                        <span className="cand-src">{c.source}</span>
                      </button>
                    ))}
                  </div>
                  {remaining > 0 && (
                    <button className="link" onClick={() => setVisible((v) => ({ ...v, [key]: shown + LOAD_MORE_STEP }))}>
                      Load more ({remaining} left)
                    </button>
                  )}
                </div>
              );
            })
          ) : (
            <div className="cand-list">
              {list.map((c) => (
                <button
                  key={c.ref}
                  className={`cand-opt ${c.selected ? 'selected' : ''} ${busy === kind + ':' + c.ref ? 'busy' : ''}`}
                  onClick={() => pick(kind, c.ref)}
                >
                  <span className="kind-dot" />
                  <span className="cand-opt-src">{c.source}</span>
                  {c.lang && <span className="muted">· {c.lang}</span>}
                  <span className="muted score">{c.score}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      ))}
    </section>
  );
}
