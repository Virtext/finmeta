import type { FileStatus } from './types';

export function StatusBadge({ status }: { status: string }) {
  const cls =
    status === 'changes' ? 'badge changes' :
    status === 'clean' ? 'badge clean' :
    status === 'empty' ? 'badge empty' :
    'badge';
  const label =
    status === 'changes' ? 'update/create' :
    status === 'clean' ? 'up to date' :
    // Matched fine, just nothing generated for it (not a TMDb match
    // failure -- see _item_entries' docstring on the backend).
    status === 'empty' ? 'nothing to write' : status;
  return <span className={cls}>{label}</span>;
}

export function FileStatusBadge({ status }: { status: FileStatus }) {
  return <span className={`file-status ${status}`}>{status}</span>;
}

export function DiffCounts({ diff }: { diff: { create: number; update: number; unchanged: number } }) {
  return (
    <span className="diff-counts">
      {diff.create > 0 && <span className="dot create" title="create">{diff.create}</span>}
      {diff.update > 0 && <span className="dot update" title="update">{diff.update}</span>}
      {diff.unchanged > 0 && <span className="dot unchanged" title="unchanged">{diff.unchanged}</span>}
      {diff.create + diff.update + diff.unchanged === 0 && <span className="dot none">0</span>}
    </span>
  );
}
