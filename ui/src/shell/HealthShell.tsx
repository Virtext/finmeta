import React, { useEffect, useState } from 'react';
import HealthPage from '../components/HealthPage';
import { getLibraries } from '../api';
import type { LibraryView } from '../types';
import { navigate } from './router';

// 2026-11-10, operator-requested: Overview's "Latest scan" links every
// check group somewhere to see specifics -- only artwork/artwork_quality
// had a modern destination (shell/ArtworkPage.tsx, a deliberately scoped
// cross-library triage list per its own header comment). The other groups
// (metadata, media, episodes, duplicates, path_unmapped) had nothing in
// the modern shell at all; clicking them fell through to Activity's run
// list, which shows jobs, not findings.
//
// Per SPEC_S14_UI.md §4's own IA table ("HealthPage -> Overview summary
// cards + a Health section under Activity, keeping its own deep link"),
// this is exactly where it was always meant to end up -- so this wraps
// the SAME shared HealthPage.tsx legacy already uses (S14 §4b: shared,
// not duplicated) rather than re-implementing its describe()/detailLines()/
// GROUP_LABELS logic a second time for no functional gain.
export function HealthShell({ dryRun }: { dryRun: boolean }): React.ReactElement {
  const [libraries, setLibraries] = useState<LibraryView[]>([]);

  useEffect(() => {
    getLibraries().then((r) => setLibraries(r.libraries)).catch(() => {});
  }, []);

  return (
    <HealthPage
      libraries={libraries}
      dryRun={dryRun}
      onBack={() => navigate('/overview')}
      onPickArtwork={(itemId, _itemName, library) =>
        navigate(`/library/${encodeURIComponent(library)}/item/${encodeURIComponent(itemId)}`)}
    />
  );
}

export default HealthShell;
