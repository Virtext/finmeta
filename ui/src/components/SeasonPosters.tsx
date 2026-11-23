import { useEffect, useState } from 'react';
import { getSeasonCandidates, itemImageUrl, selectCandidate } from '../api';
import type { Candidate, SeasonView } from '../types';
import { CandidateThumb, INITIAL_VISIBLE_PER_SOURCE, LOAD_MORE_STEP } from './CandidatePicker';

interface Props {
  itemId: string;
  seasons: SeasonView[];
  onSeasonsChange: React.Dispatch<React.SetStateAction<SeasonView[]>>;
  onSelected: () => void;   // refetch the plan after a selection
}

/** Pick a poster per season. The seasons are whatever the series has in
 * Jellyfin; a season's posters load when it is opened (a show can have dozens). */
export default function SeasonPosters({ itemId, seasons, onSeasonsChange, onSelected }: Props) {
  const [open, setOpen] = useState<number | null>(null);
  const [cands, setCands] = useState<Candidate[] | null>(null);
  const [shown, setShown] = useState(INITIAL_VISIBLE_PER_SOURCE);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open === null) return undefined;
    let alive = true;
    setCands(null);
    setError(null);
    setShown(INITIAL_VISIBLE_PER_SOURCE);
    getSeasonCandidates(itemId, open)
      .then((d) => { if (alive) setCands(d.candidates); })
      .catch((e) => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [itemId, open]);

  async function pick(season: number, ref: string | null) {
    setBusy(true);
    setError(null);
    try {
      await selectCandidate(itemId, `season_poster:${season}`, ref);
      onSeasonsChange((prev) => prev.map((s) => (s.number === season ? { ...s, picked: ref !== null } : s)));
      if (season === open) setCands((prev) => prev && prev.map((c) => ({ ...c, selected: c.ref === ref })));
      onSelected();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const current = seasons.find((s) => s.number === open);

  return (
    <div className="cand-kind">
      <div className="cand-kind-head">
        <span className="kind-label">season posters ({seasons.length})</span>
        {current?.picked && (
          <button className="link" disabled={busy} onClick={() => pick(current.number, null)}>
            reset {current.name}
          </button>
        )}
      </div>
      <div className="cand-grid">
        {seasons.map((s) => (
          <button
            key={s.number}
            className={`cand-thumb ${s.number === open ? 'selected' : ''}`}
            aria-expanded={s.number === open}
            title={s.picked ? `${s.name} — poster picked by you` : s.name}
            onClick={() => setOpen(s.number === open ? null : s.number)}
          >
            <img src={itemImageUrl(s.item_id, 'poster', 320, s.poster_tag)} alt={s.name} loading="lazy" />
            <span className="cand-src">{s.picked ? '✓ ' : ''}{s.name}</span>
          </button>
        ))}
      </div>
      {error && <div className="error">{error}</div>}
      {current && !error && (
        <div className="cand-source-group" style={{ marginTop: 10 }}>
          <div className="muted cand-source-label">{current.name}{cands && ` (${cands.length})`}</div>
          {cands === null && <div className="muted">loading…</div>}
          {cands?.length === 0 && <div className="muted">No posters found for this season on TMDb.</div>}
          <div className="cand-grid">
            {cands?.slice(0, shown).map((c) => (
              <button
                key={c.ref}
                className={`cand-thumb ${c.selected ? 'selected' : ''} ${busy ? 'busy' : ''}`}
                aria-pressed={c.selected}
                disabled={busy}
                title={`${c.source}${c.lang ? ' · ' + c.lang : ''} (${c.width}x${c.height})`}
                onClick={() => pick(current.number, c.ref)}
              >
                <CandidateThumb c={c} />
                <span className="cand-src">{c.source}</span>
              </button>
            ))}
          </div>
          {cands && cands.length > shown && (
            <button className="link" onClick={() => setShown((n) => n + LOAD_MORE_STEP)}>
              Load more ({cands.length - shown} left)
            </button>
          )}
        </div>
      )}
    </div>
  );
}
