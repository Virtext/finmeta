// Collection Builder: the operator's own `collections:` entries (filter rules
// or hand-picked TMDb ids). Same getConfig/putConfig YAML round-trip as
// CollectionsPanel/TrendingPanel so server-side validation applies.
import { useEffect, useState } from 'react';
import { getConfig, putConfig } from '../api';
import * as YAML from 'js-yaml';

type Row = {
  type: string; name: string; library: string;
  year_from: string; year_to: string; min_rating: string; genres: string; tmdb_ids: string;
  raw?: any; // tmdb_collection entries: kept untouched, not editable here
};

const csv = (v: unknown) => (Array.isArray(v) ? v.join(', ') : '');
const split = (s: string) => s.split(',').map((x) => x.trim()).filter(Boolean);
const num = (s: string) => (s.trim() === '' ? undefined : Number(s));

function fromDoc(c: any): Row {
  const f = c.filter ?? {};
  return {
    type: c.type, name: c.name ?? '', library: c.library ?? '',
    year_from: f.year_from?.toString() ?? '', year_to: f.year_to?.toString() ?? '',
    min_rating: f.min_rating?.toString() ?? '', genres: csv(f.genres), tmdb_ids: csv(c.tmdb_ids),
    raw: c.type === 'tmdb_collection' ? c : undefined,
  };
}

function toDoc(r: Row): any {
  if (r.raw) return r.raw;
  const base = { type: r.type, name: r.name.trim(), library: r.library };
  if (r.type === 'list') {
    const ids = split(r.tmdb_ids).map(Number);
    if (ids.some((n) => !Number.isInteger(n))) throw new Error(`"${r.name}": TMDb ids must be whole numbers`);
    return { ...base, tmdb_ids: ids };
  }
  const filter: any = {
    year_from: num(r.year_from), year_to: num(r.year_to), min_rating: num(r.min_rating),
    genres: split(r.genres).length ? split(r.genres) : undefined,
  };
  Object.keys(filter).forEach((k) => filter[k] === undefined && delete filter[k]);
  return { ...base, filter };
}

export default function CustomCollections() {
  const [rows, setRows] = useState<Row[]>([]);
  const [libs, setLibs] = useState<string[]>([]);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getConfig().then((c) => {
      setLibs(Object.keys((c.parsed?.libraries as object | undefined) ?? {}));
      setRows(((c.parsed?.collections as any[] | undefined) ?? []).map(fromDoc));
    }).catch((e) => setErr((e as Error).message));
  }, []);

  function edit(i: number, patch: Partial<Row>) {
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
    setDirty(true);
  }

  function add() {
    setRows((rs) => [...rs, { type: 'filter', name: '', library: libs[0] ?? '', year_from: '',
      year_to: '', min_rating: '', genres: '', tmdb_ids: '' }]);
    setDirty(true);
  }

  async function save() {
    setBusy(true); setErr(null);
    try {
      if (rows.some((r) => !r.name.trim())) throw new Error('Every collection needs a name');
      const collections = rows.map(toDoc);
      const d = await getConfig();
      let doc: any;
      try { doc = (YAML.load(d.yaml) as any) ?? {}; } catch { doc = {}; }
      doc.collections = collections;
      await putConfig(YAML.dump(doc, { noRefs: true, lineWidth: -1 }));
      setDirty(false);
      setMsg('Saved'); setTimeout(() => setMsg(null), 3500);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const input = (i: number, key: keyof Row, props: Record<string, unknown> = {}) => (
    <input className="config-input" disabled={busy} value={rows[i][key] as string}
           onChange={(e) => edit(i, { [key]: e.target.value })} {...props} />
  );

  return (
    <div className="custom-collections">
      <h4>Your collections</h4>
      <p className="field-hint muted">
        Build your own collections from one library — either by rules (year range, minimum TMDb
        rating, genres; all set rules must match) or from a hand-picked list of TMDb ids. On a
        full plan/apply they are kept exactly in sync: items that stop matching are removed, and
        a collection with no matches is deleted. A Jellyfin collection with the same name is taken
        over, so pick a name you don't already use. Genre names follow the configured metadata
        language (e.g. "Horror" for en-US).
      </p>
      <div className="profiles-table-wrap">
        <table className="profiles-table">
          <thead>
            <tr><th>Name</th><th>Library</th><th>Type</th><th>Rules</th><th></th></tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td>{r.raw ? r.name : input(i, 'name', { type: 'text', placeholder: 'e.g. 90s Horror' })}</td>
                <td>
                  {r.raw ? r.library : (
                    <select className="config-input" disabled={busy} value={r.library}
                            onChange={(e) => edit(i, { library: e.target.value })}>
                      {libs.map((l) => <option key={l} value={l}>{l}</option>)}
                    </select>
                  )}
                </td>
                <td>
                  {r.raw ? 'TMDb collection' : (
                    <select className="config-input" disabled={busy} value={r.type}
                            onChange={(e) => edit(i, { type: e.target.value })}>
                      <option value="filter">Rules</option>
                      <option value="list">Hand-picked</option>
                    </select>
                  )}
                </td>
                <td>
                  {r.raw && <span className="field-hint muted">Not supported yet — edit in YAML.</span>}
                  {!r.raw && r.type === 'list' &&
                    input(i, 'tmdb_ids', { type: 'text', placeholder: 'TMDb ids, e.g. 27205, 157336' })}
                  {!r.raw && r.type === 'filter' && (
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      {input(i, 'year_from', { type: 'number', placeholder: 'Year from', style: { width: 95 } })}
                      {input(i, 'year_to', { type: 'number', placeholder: 'Year to', style: { width: 95 } })}
                      {input(i, 'min_rating', { type: 'number', step: 0.1, min: 0, max: 10,
                        placeholder: 'Min rating', style: { width: 95 } })}
                      {input(i, 'genres', { type: 'text', placeholder: 'Genres (any of), e.g. Horror, Thriller' })}
                    </div>
                  )}
                </td>
                <td>
                  <button className="btn danger" disabled={busy}
                          onClick={() => { setRows((rs) => rs.filter((_, j) => j !== i)); setDirty(true); }}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr><td colSpan={5} className="field-hint muted">No collections yet — add one below.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="run-controls">
        <button className="btn" disabled={busy || libs.length === 0} onClick={add}>+ Add collection</button>
        <button className="btn btn-primary" disabled={busy || !dirty} onClick={() => void save()}>
          {busy ? 'Saving…' : 'Save'}
        </button>
      </div>
      {msg && <div className="field-msg ok">{msg}</div>}
      {err && <div className="field-msg err">{err}</div>}
    </div>
  );
}
