import React from 'react';

export interface CardProps extends React.HTMLAttributes<HTMLDivElement> {}

export function Card({ className, children, ...rest }: CardProps): React.ReactElement {
  return <div className={['fm-card', className ?? ''].filter(Boolean).join(' ')} {...rest}>{children}</div>;
}

export interface CardHeaderProps { title: string; action?: React.ReactNode; }

export function CardHeader({ title, action }: CardHeaderProps): React.ReactElement {
  return (
    <div className="fm-card__head">
      <h5 className="fm-card__title">{title}</h5>
      {action && <div className="fm-card__head-action">{action}</div>}
    </div>
  );
}

export function CardBody({ children, className, ...rest }: React.HTMLAttributes<HTMLDivElement>): React.ReactElement {
  return <div className={['fm-card__body', className ?? ''].filter(Boolean).join(' ')} {...rest}>{children}</div>;
}

export function CardFooter({ children, className, ...rest }: React.HTMLAttributes<HTMLDivElement>): React.ReactElement {
  return <div className={['fm-card__footer', className ?? ''].filter(Boolean).join(' ')} {...rest}>{children}</div>;
}

export default Card;
