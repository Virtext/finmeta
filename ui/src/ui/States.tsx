import React from 'react';
import Icon, { IconName } from './Icon';

export interface SkeletonProps { width?: string | number; height?: string | number; radius?: string; className?: string; }

export function Skeleton({ width = '100%', height = '1em', radius, className }: SkeletonProps): React.ReactElement {
  return (
    <span
      className={['fm-skeleton', className ?? ''].filter(Boolean).join(' ')}
      style={{ display: 'inline-block', width, height, borderRadius: radius }}
      aria-hidden="true"
    />
  );
}

export interface StateProps {
  icon?: IconName;
  title: string;
  description?: string;
  action?: React.ReactNode;
}

export function EmptyState({ icon = 'search', title, description, action }: StateProps): React.ReactElement {
  return (
    <div className="fm-state">
      <Icon name={icon} size={28} className="fm-state__icon" />
      <div className="fm-state__title">{title}</div>
      {description && <div className="fm-state__desc">{description}</div>}
      {action}
    </div>
  );
}

export function ErrorState({ icon = 'warning', title, description, action }: StateProps): React.ReactElement {
  return (
    <div className="fm-state fm-state--error">
      <Icon name={icon} size={28} className="fm-state__icon" />
      <div className="fm-state__title">{title}</div>
      {description && <div className="fm-state__desc">{description}</div>}
      {action}
    </div>
  );
}
