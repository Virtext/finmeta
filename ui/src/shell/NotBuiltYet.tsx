import React from 'react';
import { Button, EmptyState } from '../ui';
import { setUiShell } from './uiShell';

export interface NotBuiltYetProps {
  title: string;
}

// Honest placeholder for every sidebar entry the redesign hasn't reached
// yet (SPEC_S14_UI.md phases P3-P6). The real feature still lives in the
// classic interface, so this offers a genuine way to it instead of a dead
// end -- never a faked preview of a page that doesn't exist.
export function NotBuiltYet({ title }: NotBuiltYetProps): React.ReactElement {
  return (
    <EmptyState
      icon="clock"
      title={`${title} hasn't moved here yet`}
      description="This part of the redesign isn't built. The real feature still works in the classic interface."
      action={<Button onClick={() => setUiShell('legacy')}>Open in classic interface</Button>}
    />
  );
}

export default NotBuiltYet;
