import React from 'react';
import SettingsPage from '../components/SettingsPage';
import { navigate } from './router';

// SPEC_S14_UI.md P6/S13b: progressive disclosure (search, reset-to-default,
// save-time diff, Basic/Advanced/Expert) landed 2026-11-09. Per §4b, the
// FULL real SettingsPage is still shared here (not rebuilt) -- every
// setting stays reachable, just with classic field styling. No PageHeader
// here on purpose: SettingsPage renders its own "← Back / Settings" header.
//
// 2026-11-09, operator-requested: Libraries/Schedules/Trending/Collections/
// Overlays are all fully covered by modern pages now (Profiles/Automation/
// Overlay Studio) -- hiding them here removes the duplicate, not any
// capability. Overlays joined last, once the Studio reached parity (base
// settings, per-element fine-tuning, format rules, named styles) -- see
// SettingsPage.tsx's Props comment and OverlaysPage.tsx's header comment.
export function SettingsShell(): React.ReactElement {
  return <SettingsPage onBack={() => navigate('/overview')} hideRedundantTabs />;
}

export default SettingsShell;
