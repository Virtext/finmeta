import React from 'react';

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'default' | 'ghost' | 'danger';
  size?: 'sm' | 'md';
  loading?: boolean;
}

export function Button({
  variant = 'default', size = 'md', loading = false, disabled, className, children, ...rest
}: ButtonProps): React.ReactElement {
  const cls = [
    'fm-btn',
    variant !== 'default' ? `fm-btn--${variant}` : '',
    `fm-btn--${size}`,
    className ?? '',
  ].filter(Boolean).join(' ');
  return (
    <button className={cls} disabled={disabled || loading} {...rest}>
      {loading && <span className="fm-btn__spinner" aria-hidden="true" />}
      {children}
    </button>
  );
}

export default Button;
