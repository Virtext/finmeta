// S9g2 (K7b, SPEC_KOMETA.md): MDBList trending lists -> Jellyfin collections
// + optional TOP 10 rank overlay. Same getConfig/putConfig round-trip as
// SchedulesPanel/ProfilesPanel so server-side validation and the existing
// last-good/undo semantics apply exactly once.
import { useCallback, useEffect, useState } from 'react';
import { getConfig, putConfig } from '../api';
import * as YAML from 'js-yaml';

interface TrendingListRow {
  url: string; collection_name: string; limit: number; rank_overlay: boolean;
}

const EMPTY_ROW: TrendingListRow = { url: '', collection_name: '', limit: 0, rank_overlay: false };

export default function TrendingPanel() {
  const [lists, setLists] = useState<TrendingListRow[]>([]);
  const [mdblistReady, setMdblistReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const c = await getConfig();
      const raw = (c.parsed?.trending?.lists as any[]) ?? [];
      setLists(raw.map((l) => ({
        url: l.url ?? '', collection_name: l.collection_name ?? '',
        limit: l.limit ?? 0, rank_overlay: Boolean(l.rank_overlay),
      })));
      setMdblistReady(Boolean(c.parsed?.sources?.mdblist?.enabled));
    } catch { /* keep last good state */ }
  }, []);

  useEffect(() => { void load(); }, [load]);

  function flash(m: string) { setMsg(m); setTimeout(() => setMsg(null), 3500); }

  async function save(next: TrendingListRow[]): Promise<boolean> {
    setBusy(true); setErr(null);
    try {
      const d = await getConfig();
      let doc: any;
      try { doc = (YAML.load(d.yaml) as any) ?? {}; } catch { doc = {}; }
      if (!doc.trending) doc.trending = {};
      doc.trending.lists = next.map((l) => ({
        url: l.url, collection_name: l.collection_name, limit: l.limit, rank_overlay: l.rank_overlay,
      }));
      const text = YAML.dump(doc, { noRefs: true, lineWidth: -1 });
      await putConfig(text);     // server-side validated; 400 on error
      // Trust `next` as the new state instead of re-fetching: a re-fetch
      // racing a later still-in-flight edit (e.g. tabbing to the next field
      // before this save settles) would clobber it with stale server data.
      setLists(next);
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  function addList() {
    void save([...lists, { ...EMPTY_ROW }]).then((ok) => { if (ok) flash('List added'); });
  }

  function updateField(i: number, patch: Partial<TrendingListRow>) {
    setLists((rows) => rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }

  function commitField() {
    void save(lists).then((ok) => { if (ok) flash('Saved'); });
  }

  function toggleRankOverlay(i: number) {
    const next = lists.map((r, idx) => (idx === i ? { ...r, rank_overlay: !r.rank_overlay } : r));
    setLists(next);
    void save(next).then((ok) => { if (ok) flash('Saved'); });
  }

  function removeList(i: number) {
    void save(lists.filter((_, idx) => idx !== i)).then((ok) => { if (ok) flash('List removed'); });
  }

  return (
    <div className="trending-panel">
      <h3>Trending (MDBList)</h3>
      <p className="field-hint muted" style={{ marginTop: 0 }}>
        Mirrors MDBList lists (e.g. Trakt Trending Movies/TV Shows) into Jellyfin collections.
        Only items already in a managed library are added — nothing is downloaded. Optionally tags
        the top 10 with a rank for the <code>trending_rank</code> overlay element (Overlays tab).
      </p>
      {!mdblistReady && (
        <div className="field-msg err" style={{ marginBottom: 10 }}>
          MDBList is not enabled, or has no saved API key — turn it on and save a key in
          External services first. Lists below won't fetch anything until then.
        </div>
      )}
      <div className="profiles-table-wrap">
        <table className="profiles-table">
          <thead><tr><th>MDBList URL</th><th>Collection name</th><th>Limit</th><th>Top 10 rank</th><th></th></tr></thead>
          <tbody>
            {lists.map((l, i) => (
              <tr key={i}>
                <td>
                  <input
                    className="config-input" type="text" value={l.url} disabled={busy}
                    placeholder="https://mdblist.com/lists/<user>/<slug>"
                    onChange={(e) => updateField(i, { url: e.target.value })}
                    onBlur={commitField}
                  />
                </td>
                <td>
                  <input
                    className="config-input" type="text" value={l.collection_name} disabled={busy}
                    placeholder="Trending Movies"
                    onChange={(e) => updateField(i, { collection_name: e.target.value })}
                    onBlur={commitField}
                  />
                </td>
                <td>
                  <input
                    className="config-input" type="number" min={0} max={200} style={{ width: 70 }}
                    value={l.limit} disabled={busy}
                    onChange={(e) => updateField(i, { limit: Math.max(0, Number(e.target.value) || 0) })}
                    onBlur={commitField}
                  />
                </td>
                <td>
                  <input type="checkbox" disabled={busy} checked={l.rank_overlay}
                         onChange={() => toggleRankOverlay(i)} />
                </td>
                <td>
                  <button className="btn danger" disabled={busy} onClick={() => removeList(i)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
            {lists.length === 0 && (
              <tr><td colSpan={5} className="field-hint muted">No trending lists yet — add one below.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <button className="btn primary" disabled={busy} onClick={addList}>+ Add list</button>

      {msg && <div className="field-msg ok">{msg}</div>}
      {err && <div className="field-msg err">{err}</div>}
    </div>
  );
}
