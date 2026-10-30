// S9d5 (CLAUDE.md §4.1): "Tidsplan (schedules) for nattlig overlay-kjøring
// får UI (i dag bare rå YAML)" — a Schedules tab replacing raw-YAML-only
// editing for `schedules` (cron + action + library subset). Same
// getConfig/putConfig round-trip as ProfilesPanel so server-side validation
// (cron syntax, library references) and the existing last-good/undo
// semantics apply exactly once.
import { useCallback, useEffect, useState } from 'react';
import { getConfig, putConfig } from '../api';
import * as YAML from 'js-yaml';

interface ScheduleRow { cron: string; action: 'plan' | 'apply'; libraries: string[] }

export default function SchedulesPanel() {
  const [schedules, setSchedules] = useState<ScheduleRow[]>([]);
  const [libNames, setLibNames] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const c = await getConfig();
      const raw = (c.parsed?.schedules as any[]) ?? [];
      setSchedules(raw.map((s) => ({
        cron: s.cron ?? '', action: (s.action ?? 'plan') as 'plan' | 'apply',
        libraries: Array.isArray(s.libraries) ? s.libraries : [],
      })));
      setLibNames(Object.keys(c.parsed?.libraries ?? {}));
    } catch { /* keep last good state */ }
  }, []);

  useEffect(() => { void load(); }, [load]);

  function flash(m: string) { setMsg(m); setTimeout(() => setMsg(null), 3500); }

  async function save(next: ScheduleRow[]): Promise<boolean> {
    setBusy(true); setErr(null);
    try {
      const d = await getConfig();
      let doc: any;
      try { doc = (YAML.load(d.yaml) as any) ?? {}; } catch { doc = {}; }
      doc.schedules = next.map((s) => ({ cron: s.cron, action: s.action, libraries: s.libraries }));
      const text = YAML.dump(doc, { noRefs: true, lineWidth: -1 });
      await putConfig(text);     // server-side validated (cron syntax, library refs); 400 on error
      // Trust `next` as the new state instead of re-fetching: a re-fetch
      // racing a later still-in-flight edit (e.g. tabbing to the next field
      // before this save settles) would clobber it with stale server data.
      setSchedules(next);
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  function addSchedule() {
    void save([...schedules, { cron: '0 4 * * *', action: 'plan', libraries: [] }])
      .then((ok) => { if (ok) flash('Schedule added'); });
  }

  function updateCron(i: number, cron: string) {
    setSchedules((rows) => rows.map((s, idx) => (idx === i ? { ...s, cron } : s)));
  }

  function commitCron() {
    void save(schedules).then((ok) => { if (ok) flash('Schedule saved'); });
  }

  function setAction(i: number, action: 'plan' | 'apply') {
    const next = schedules.map((s, idx) => (idx === i ? { ...s, action } : s));
    setSchedules(next);
    void save(next).then((ok) => { if (ok) flash('Schedule saved'); });
  }

  function toggleLibrary(i: number, lib: string) {
    const s = schedules[i];
    const libraries = s.libraries.includes(lib)
      ? s.libraries.filter((l) => l !== lib)
      : [...s.libraries, lib];
    const next = schedules.map((row, idx) => (idx === i ? { ...row, libraries } : row));
    setSchedules(next);
    void save(next).then((ok) => { if (ok) flash('Schedule saved'); });
  }

  function removeSchedule(i: number) {
    void save(schedules.filter((_, idx) => idx !== i))
      .then((ok) => { if (ok) flash('Schedule removed'); });
  }

  return (
    <div className="schedules-panel">
      <h3>Schedules</h3>
      <p className="field-hint muted" style={{ marginTop: 0 }}>
        Recurring plan/apply runs (cron). Empty library selection = all configured libraries.
        Once an item's artwork is locked (first successful apply — see the candidate picker's
        🔒 indicator), an "apply" schedule here already behaves as a nightly overlay-only
        refresh: it never re-queries TMDb/fanart for locked items.
        {' '}Not sure about the cron syntax?{' '}
        <a href="https://crontab.guru/" target="_blank" rel="noreferrer" className="link">crontab.guru</a>.
      </p>
      <div className="profiles-table-wrap">
        <table className="profiles-table">
          <thead><tr><th>Cron</th><th>Action</th><th>Libraries</th><th></th></tr></thead>
          <tbody>
            {schedules.map((s, i) => (
              <tr key={i}>
                <td>
                  <input
                    className="config-input" type="text" value={s.cron} disabled={busy}
                    placeholder="0 4 * * *"
                    onChange={(e) => updateCron(i, e.target.value)}
                    onBlur={() => commitCron()}
                  />
                </td>
                <td>
                  <select
                    value={s.action} disabled={busy}
                    onChange={(e) => setAction(i, e.target.value as 'plan' | 'apply')}
                  >
                    <option value="plan">plan</option>
                    <option value="apply">apply</option>
                  </select>
                </td>
                <td>
                  <div className="schedule-libs">
                    {libNames.length === 0 && (
                      <span className="field-hint muted">no libraries configured</span>
                    )}
                    {libNames.map((lib) => (
                      <label key={lib} className="pack-cat" title={`Restrict this schedule to ${lib}`}>
                        <input
                          type="checkbox" disabled={busy}
                          checked={s.libraries.includes(lib)}
                          onChange={() => toggleLibrary(i, lib)}
                        />
                        {lib}
                      </label>
                    ))}
                    {s.libraries.length === 0 && <span className="field-hint muted">(all)</span>}
                  </div>
                </td>
                <td>
                  <button className="btn danger" disabled={busy} onClick={() => removeSchedule(i)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
            {schedules.length === 0 && (
              <tr><td colSpan={4} className="field-hint muted">No schedules yet — add one below.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <button className="btn primary" disabled={busy} onClick={addSchedule}>+ Add schedule</button>

      {msg && <div className="field-msg ok">{msg}</div>}
      {err && <div className="field-msg err">{err}</div>}
    </div>
  );
}
