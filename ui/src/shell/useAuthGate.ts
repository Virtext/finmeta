import { useEffect, useState } from 'react';
import { getAuthStatus } from '../api';

// Same first-run / 401 gating LegacyApp.tsx has always done, factored out so
// ModernApp doesn't duplicate it (and doesn't touch LegacyApp to share it --
// LegacyApp stays a verbatim, lowest-risk copy per SPEC_S14_UI.md §4b).
export function useAuthGate(onReady: () => void): {
  unauthed: boolean;
  setUnauthed: (v: boolean) => void;
} {
  const [unauthed, setUnauthed] = useState(false);

  useEffect(() => {
    getAuthStatus()
      .then((d) => {
        if (d.auth_enabled && !d.has_password) setUnauthed(true);
        else onReady();
      })
      .catch(() => onReady());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { unauthed, setUnauthed };
}
