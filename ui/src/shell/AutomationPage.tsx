import React, { useState } from 'react';
import { PageHeader, Tabs } from '../ui';
import SchedulesPanel from '../components/SchedulesPanel';
import TrendingPanel from '../components/TrendingPanel';
import CollectionsPanel from '../components/CollectionsPanel';

// SPEC_S14_UI.md §4 groups these three under one "Automation" sidebar entry
// ("they are both scheduled list-driven features"). Per §4b, a panel that
// hasn't been individually redesigned yet is SHARED, not rebuilt or faked --
// these three are fully self-contained (no props from their old home inside
// SettingsPage) and 100% real here; they just carry their classic styling
// until a later pass gives each its own modern treatment.

type Section = 'schedules' | 'trending' | 'collections';

export function AutomationPage(): React.ReactElement {
  const [section, setSection] = useState<Section>('schedules');
  return (
    <div>
      <PageHeader title="Automation" description="Scheduled runs, trending lists, and curated collections." />
      <Tabs
        aria-label="Automation sections"
        items={[
          { id: 'schedules', label: 'Schedules' },
          { id: 'trending', label: 'Trending' },
          { id: 'collections', label: 'Collections' },
        ]}
        activeId={section}
        onChange={(id) => setSection(id as Section)}
      />
      <div style={{ marginTop: 16 }}>
        {section === 'schedules' && <SchedulesPanel />}
        {section === 'trending' && <TrendingPanel />}
        {section === 'collections' && <CollectionsPanel />}
      </div>
    </div>
  );
}

export default AutomationPage;
