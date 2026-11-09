import React, { useEffect, useState } from 'react';
import { PageHeader, Select, Card, CardBody, EmptyState } from '../ui';
import { getConfig } from '../api';
import PacksPanel from '../components/PacksPanel';

// S14 (2026-11-09): first real top-level home for pack management -- it was
// previously only reachable by opening a library's overlay profile in the
// (legacy) Overlays tab. PacksPanel.tsx itself is unchanged behaviourally
// (shared with that legacy tab, SPEC_S14_UI.md §4b); this page's only new
// contribution is a profile picker, so pack activation no longer requires
// first opening the style editor for that profile.
export function PacksPage(): React.ReactElement {
  const [profileNames, setProfileNames] = useState<string[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    getConfig()
      .then((d) => {
        const names = Object.keys(d.parsed?.overlays?.profiles ?? {}).sort();
        setProfileNames(names);
        setSelected((prev) => prev || names[0] || '');
      })
      .finally(() => setLoaded(true));
  }, []);

  return (
    <div>
      <PageHeader
        title="Packs"
        description="Custom badge image packs and the Kometa logo pack."
      />
      <div style={{ marginTop: 16, display: 'flex', flexDirection: 'column', gap: 16 }}>
        <Card>
          <CardBody>
            <Select
              label="Apply to profile"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              options={profileNames.map((n) => ({ value: n, label: n }))}
              disabled={!profileNames.length}
            />
          </CardBody>
        </Card>

        {loaded && !profileNames.length ? (
          <EmptyState
            icon="packs"
            title="No overlay profiles yet"
            description="Create one on the Overlays page first, then come back here to assign packs to it."
          />
        ) : (
          <PacksPanel profileName={selected || null} />
        )}
      </div>
    </div>
  );
}

export default PacksPage;
