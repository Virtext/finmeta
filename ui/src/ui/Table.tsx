import React from 'react';

export interface TableColumn<T> {
  key: string;
  header: string;
  render: (row: T) => React.ReactNode;
  width?: string;
}

export interface TableProps<T> {
  columns: TableColumn<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  selectedKey?: string | null;
  onRowClick?: (row: T) => void;
}

export function Table<T>({ columns, rows, rowKey, selectedKey, onRowClick }: TableProps<T>): React.ReactElement {
  return (
    <div className="fm-table-wrap">
      <table className="fm-table">
        <thead>
          <tr>
            {columns.map((c) => <th key={c.key} style={{ width: c.width }}>{c.header}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const key = rowKey(row);
            const cls = [
              onRowClick ? 'fm-table__row--clickable' : '',
              key === selectedKey ? 'fm-table__row--selected' : '',
            ].filter(Boolean).join(' ');
            return (
              <tr key={key} className={cls || undefined} onClick={onRowClick ? () => onRowClick(row) : undefined}>
                {columns.map((c) => <td key={c.key}>{c.render(row)}</td>)}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default Table;
