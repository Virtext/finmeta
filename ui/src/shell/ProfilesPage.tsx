import React from 'react';
import { PageHeader } from '../ui';
import ProfilesPanel from '../components/ProfilesPanel';
import { navigate } from './router';

// SPEC_S14_UI.md §4b: ProfilesPanel.tsx is shared, not duplicated -- it
// already does library<->profile assignment, per-library artwork kinds,
// artwork_selection overrides and poster rotation (498 lines, real,
// working). The one real integration improvement available for free:
// wire its "Edit in Overlays" action at the real new Overlays page
// instead of a Settings tab that no longer exists in this shell.
export function ProfilesPage(): React.ReactElement {
  return (
    <div>
      <PageHeader title="Profiles" description="Assign overlay profiles and artwork settings per library." />
      <div style={{ marginTop: 16 }}>
        <ProfilesPanel onEditInOverlays={() => navigate('/overlays')} />
      </div>
    </div>
  );
}

export default ProfilesPage;
