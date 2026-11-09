import React from 'react';
import Icon, { IconName } from './Icon';

export interface IconButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  icon: IconName;
  size?: 'sm' | 'md';
  label: string; // required -- this is the only accessible name an icon-only button gets
  dot?: boolean;
}

export function IconButton({
  icon, size = 'md', label, dot, className, ...rest
}: IconButtonProps): React.ReactElement {
  const cls = ['fm-iconbtn', size === 'sm' ? 'fm-iconbtn--sm' : '', className ?? ''].filter(Boolean).join(' ');
  return (
    <button className={cls} aria-label={label} title={label} {...rest}>
      <Icon name={icon} size={size === 'sm' ? 13 : 15} />
      {dot && <span className="fm-iconbtn__dot" />}
    </button>
  );
}

export default IconButton;
