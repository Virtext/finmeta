import React from 'react';

export interface PageHeaderProps {
  crumb?: string;
  title: string;
  description?: string;
  actions?: React.ReactNode;
}

export function PageHeader({ crumb, title, description, actions }: PageHeaderProps): React.ReactElement {
  return (
    <div className="fm-pagehead">
      <div>
        {crumb && <div className="fm-pagehead__crumb">{crumb}</div>}
        <h1 className="fm-pagehead__title">{title}</h1>
        {description && <div className="fm-pagehead__sub">{description}</div>}
      </div>
      {actions && <div className="fm-pagehead__actions">{actions}</div>}
    </div>
  );
}

export default PageHeader;
