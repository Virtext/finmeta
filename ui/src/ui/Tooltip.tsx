import React, { useState } from 'react';

export interface TooltipProps {
  text: string;
  children: React.ReactElement;
}

// Replaces bare `title=` attributes (SPEC_S14_UI.md §2.5): title= never shows
// on touch and cannot be styled to match the design system.
export function Tooltip({ text, children }: TooltipProps): React.ReactElement {
  const [show, setShow] = useState(false);
  return (
    <span
      className="fm-tooltip-wrap"
      onMouseEnter={() => setShow(true)}
      onMouseLeave={() => setShow(false)}
      onFocus={() => setShow(true)}
      onBlur={() => setShow(false)}
    >
      {children}
      {show && <span className="fm-tooltip" role="tooltip">{text}</span>}
    </span>
  );
}

export default Tooltip;
