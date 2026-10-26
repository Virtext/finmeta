import { useMemo, useState } from 'react';
import type { ItemLightView, LibItemsLightResp } from '../types';
import BulkApplyPanel, { type LibraryRunState } from './BulkApplyPanel';

interface Props {
  library: string;
  data: LibItemsLightResp | null;
  selectedId: string | null;
  onSelect: (item: ItemLightView) => void;
  loading: boolean;
  error: string | null;
  dryRun: boolean;
  libraryRun: LibraryRunState;
  onStartLibraryRun: (kind: 'plan' | 'apply') => void;
  /** "Edit overlay profile" -> Settings, Overlays tab, this library preselected. */
  onEditOverlayProfile?: (library: string) => void;
}

function ItemRow({ item, selected, onSelect }: {
  item: ItemLightView; selected: boolean; onSelect: () => void;
}) {
  return (
    <div className={`item-row ${selected ? 'selected' : ''}`} onClick={onSelect}>
      <span className="item-media">{item.media_type === 'tvshow' ? '📺' : '🎬'}</span>
      <span className="item-name" title={item.item_name}>{item.item_name}</span>
    </div>
  );
}

/** S9d4c Fase A step 3: "Library disabled" / "Overlays: <profile> (N elements)" /
 * "Overlays: none" -- same enabled/overlay_profile/overlay_element_count the
 * backend derives (profile wins, auto-migration already applied). Clicking the
 * overlay line jumps to Settings -> Overlays tab for this library (the overlay
 * preview itself lives there now, not duplicated per-item on the home screen). */
function LibraryStatusLine({ data, library, onEditOverlayProfile }: {
  data: LibItemsLightResp; library: string; onEditOverlayProfile?: (library: string) => void;
}) {
  if (!data.enabled) {
    return <div className="library-status-line disabled">Library disabled</div>;
  }
  const label = data.overlay_profile && data.overlay_element_count > 0
    ? `Overlays: ${data.overlay_profile} (${data.overlay_element_count} element${data.overlay_element_count === 1 ? '' : 's'})`
    : 'Overlays: none';
  if (!onEditOverlayProfile) {
    return <div className={`library-status-line ${data.overlay_profile && data.overlay_element_count > 0 ? 'on' : 'off'}`}>{label}</div>;
  }
  return (
    <button
      className={`library-status-line link ${data.overlay_profile && data.overlay_element_count > 0 ? 'on' : 'off'}`}
      title="Edit overlay profile"
      onClick={() => onEditOverlayProfile(library)}
    >
      {label}
    </button>
  );
}

/** S9d2-fix2 step 6a-fix: instances whose api_key is not a valid secret name. */
function invalidInstanceRefs(data: LibItemsLightResp): string[] {
  const out: string[] = [];
  for (const kind of ['sonarr', 'radarr'] as const) {
    for (const inst of data.media_apps?.[kind] ?? []) {
      if (inst.api_key && !inst.api_key_valid) out.push(inst.api_key);
    }
  }
  return out;
}

export default function LibraryBrowser({
  library, data, selectedId, onSelect, loading, error, dryRun, libraryRun, onStartLibraryRun, onEditOverlayProfile,
}: Props) {
  const [query, setQuery] = useState('');
  const filtered = useMemo(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    if (!q) return data.items;
    return data.items.filter((it) => it.item_name.toLowerCase().includes(q));
  }, [data, query]);

  return (
    <aside className="browser">
      <div className="browser-head">
        <h2>Library</h2>
        {data && <span className="summary">{data.items.length} items</span>}
      </div>

      {loading && <div className="muted pad">Loading items…</div>}
      {error && <div className="error pad">{error}</div>}

      {!loading && !error && data && (
        <>
          <LibraryStatusLine data={data} library={library} onEditOverlayProfile={onEditOverlayProfile} />
          {/* Library-wide plan/apply -- lives here (once per library), not
              repeated on every item's preview; see BulkApplyPanel. It builds
              its own plan on demand ("Plan library") instead of reusing this
              list, which is Jellyfin-only and carries no diff/status. */}
          <BulkApplyPanel library={library} dryRun={dryRun} runState={libraryRun} onStart={onStartLibraryRun} />
          {invalidInstanceRefs(data).map((ref) => (
            <div key={ref} className="provider-warning" title={`media_apps instance api_key "${ref}" is not a valid secret name`}>
              ⚠ invalid media-app API key reference: {ref} — re-save the instance in Settings → Local services
            </div>
          ))}
          <input
            className="config-input library-search"
            type="text"
            placeholder={`Search ${data.items.length} title${data.items.length === 1 ? '' : 's'}…`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <div className="item-list">
            {filtered.map((it) => (
              <ItemRow
                key={it.item_id}
                item={it}
                selected={it.item_id === selectedId}
                onSelect={() => onSelect(it)}
              />
            ))}
            {query && filtered.length === 0 && (
              <div className="muted pad">No titles match "{query}".</div>
            )}
          </div>
        </>
      )}
    </aside>
  );
}
