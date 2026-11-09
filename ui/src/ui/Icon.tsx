import React from 'react';

// SPEC_S14_UI.md decision D4: a small hand-made inline-SVG set, no new
// dependency, rather than lucide-react. Grows as pages need more glyphs.
export type IconName =
  | 'overview' | 'library' | 'artwork' | 'overlays' | 'profiles' | 'packs'
  | 'automation' | 'activity' | 'settings' | 'search' | 'bell' | 'user'
  | 'collapse' | 'expand' | 'check' | 'close' | 'chevron-down' | 'chevron-right'
  | 'warning' | 'clock' | 'external' | 'undo' | 'lock' | 'sun' | 'moon';

const PATHS: Record<IconName, React.ReactNode> = {
  overview: <><rect x="2" y="2" width="5" height="5" rx="1" /><rect x="9" y="2" width="5" height="5" rx="1" /><rect x="2" y="9" width="5" height="5" rx="1" /><rect x="9" y="9" width="5" height="5" rx="1" /></>,
  library: <><rect x="2" y="2.5" width="12" height="11" rx="1.5" /><path d="M6 2.5v11" /></>,
  artwork: <><rect x="2.5" y="2" width="11" height="12" rx="1.5" /><path d="M2.5 10.5 6 7l3 3 2-2 2.5 2.5" /></>,
  overlays: <path d="M8 1.8 14 8l-6 6.2L2 8z" />,
  profiles: <><rect x="2" y="4" width="9" height="9" rx="1.5" /><path d="M5 4V2.8h9V12" /></>,
  packs: <><path d="M8 1.9 14 5v6l-6 3.1L2 11V5z" /><path d="M2 5l6 3 6-3M8 8v6.1" /></>,
  automation: <><circle cx="8" cy="8" r="6" /><path d="M8 4.6V8l2.6 1.6" /></>,
  activity: <path d="M2 12.5V7m4 5.5V3.5m4 9V8m4 4.5V5" />,
  settings: <><circle cx="8" cy="8" r="2.2" /><path d="M8 1.6v2M8 12.4v2M1.6 8h2M12.4 8h2M3.5 3.5l1.4 1.4M11.1 11.1l1.4 1.4M12.5 3.5l-1.4 1.4M4.9 11.1 3.5 12.5" /></>,
  search: <><circle cx="7" cy="7" r="4.5" /><path d="M10.5 10.5 14 14" /></>,
  bell: <><path d="M4 6.5a4 4 0 0 1 8 0c0 3 1 4 1 4H3s1-1 1-4Z" /><path d="M6.5 13a1.6 1.6 0 0 0 3 0" /></>,
  user: <><circle cx="8" cy="5.6" r="3" /><path d="M2.3 14c.8-2.8 3-4.3 5.7-4.3s4.9 1.5 5.7 4.3" /></>,
  collapse: <path d="M10 3 5 8l5 5" />,
  expand: <path d="M6 3l5 5-5 5" />,
  check: <path d="M3.4 8.4 6.4 11.4 12.6 5" />,
  close: <path d="M4 4l8 8M12 4l-8 8" />,
  'chevron-down': <path d="M4 6l4 4 4-4" />,
  'chevron-right': <path d="M6 4l4 4-4 4" />,
  warning: <><path d="M8 2.5 14.5 13.5h-13z" /><path d="M8 6.4v3.1M8 11.3v.6" /></>,
  clock: <><circle cx="8" cy="8" r="6" /><path d="M8 4.8V8l2.4 1.5" /></>,
  external: <><path d="M6 4H4a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1v-2" /><path d="M9 3h4v4M13 3 7 9" /></>,
  undo: <path d="M4 4.8v3.6h3.6M4.4 8A5.2 5.2 0 1 1 6 11.6" />,
  lock: <><rect x="3.5" y="7" width="9" height="6.5" rx="1.3" /><path d="M5.3 7V5a2.7 2.7 0 0 1 5.4 0v2" /></>,
  sun: <><circle cx="8" cy="8" r="3" /><path d="M8 1.6v2M8 12.4v2M1.6 8h2M12.4 8h2M3.5 3.5l1.4 1.4M11.1 11.1l1.4 1.4M12.5 3.5l-1.4 1.4M4.9 11.1 3.5 12.5" /></>,
  moon: <path d="M13.5 9.6A5.8 5.8 0 0 1 6.4 2.5a5.8 5.8 0 1 0 7.1 7.1Z" />,
};

export interface IconProps {
  name: IconName;
  size?: number;
  className?: string;
  'aria-hidden'?: boolean;
}

export function Icon({ name, size = 16, className, ...rest }: IconProps): React.ReactElement {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.4}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden={rest['aria-hidden'] ?? true}
    >
      {PATHS[name]}
    </svg>
  );
}

export default Icon;
