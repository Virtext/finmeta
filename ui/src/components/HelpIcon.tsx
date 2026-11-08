import { useState } from 'react';

/**
 * The "(i)" help bubble used next to a label or heading.
 *
 * Text always comes from the backend help registry (GET /api/help), never
 * from the frontend -- SPEC S9 §B. SettingsPage and OverlaysEditor still
 * carry their own identical inline copies from before this file existed;
 * they can adopt this one whenever either is next touched.
 */
export default function HelpIcon({ help }: { help?: string }) {
  const [open, setOpen] = useState(false);
  if (!help) return null;
  return (
    <span className="help-icon" onClick={(e) => { e.stopPropagation(); setOpen(!open); }}>
      (i)
      {open && (
        <span
          className="help-pop"
          dangerouslySetInnerHTML={{ __html: help.replace(/\n/g, '<br/>') }}
        />
      )}
    </span>
  );
}
