// SPEC_S14_UI.md §4b: which shell renders. The operator's explicit
// requirement is that the old interface is never deleted -- this is the
// switch between it and the new one.
export type UiShell = 'legacy' | 'modern';

const KEY = 'finmeta_ui_shell';

// Default is 'legacy' on purpose: an existing install should see exactly
// what it saw yesterday until the operator opts in. ?ui=legacy|modern in
// the URL always wins, so a broken modern shell can never lock anyone out.
export function getUiShell(): UiShell {
  try {
    const qp = new URLSearchParams(window.location.search).get('ui');
    if (qp === 'legacy' || qp === 'modern') return qp;
  } catch { /* ignore */ }
  try {
    return window.localStorage.getItem(KEY) === 'modern' ? 'modern' : 'legacy';
  } catch {
    return 'legacy';
  }
}

export function setUiShell(shell: UiShell): void {
  try { window.localStorage.setItem(KEY, shell); } catch { /* ignore */ }
  window.location.reload();
}
