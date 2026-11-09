import { useEffect, useState } from 'react';

// SPEC_S14_UI.md §3, decision D2: a hand-rolled hash router, no dependency.
// main.tsx checks for the literal hash '#/_ds' (the primitive gallery)
// BEFORE this ever mounts, so this router never has to know about it.
function normalize(hash: string): string {
  const h = hash.replace(/^#/, '');
  return h === '' ? '/overview' : h;
}

export function useHashPath(): string {
  const [path, setPath] = useState(() => normalize(window.location.hash));
  useEffect(() => {
    const onHashChange = () => setPath(normalize(window.location.hash));
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);
  return path;
}

export function navigate(path: string): void {
  window.location.hash = path;
}
