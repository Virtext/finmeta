import React from 'react';

export type BadgeTone = 'ok' | 'warn' | 'danger' | 'accent' | 'neutral';

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
}

export function Badge({ tone = 'neutral', className, children, ...rest }: BadgeProps): React.ReactElement {
  return (
    <span className={['fm-badge', `fm-badge--${tone}`, className ?? ''].filter(Boolean).join(' ')} {...rest}>
      {children}
    </span>
  );
}

export interface StatusDotProps { tone?: BadgeTone; title?: string; }

export function StatusDot({ tone = 'neutral', title }: StatusDotProps): React.ReactElement {
  return <span className={`fm-dot fm-dot--${tone}`} title={title} />;
}

export default Badge;
