import React, { useId } from 'react';

interface FieldWrapProps {
  label?: string;
  help?: string;
  error?: string;
  children: (idProps: { id: string; 'aria-describedby'?: string }) => React.ReactNode;
}

function FieldWrap({ label, help, error, children }: FieldWrapProps): React.ReactElement {
  const id = useId();
  const helpId = help ? `${id}-help` : undefined;
  const errId = error ? `${id}-err` : undefined;
  const describedBy = [helpId, errId].filter(Boolean).join(' ') || undefined;
  return (
    <div className="fm-field">
      {label && <label className="fm-field__label" htmlFor={id}>{label}</label>}
      {children({ id, 'aria-describedby': describedBy })}
      {help && <span className="fm-field__help" id={helpId}>{help}</span>}
      {error && <span className="fm-field__error" id={errId} role="alert">{error}</span>}
    </div>
  );
}

export interface InputProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'id'> {
  label?: string;
  help?: string;
  error?: string;
}

export function Input({ label, help, error, className, ...rest }: InputProps): React.ReactElement {
  return (
    <FieldWrap label={label} help={help} error={error}>
      {(idProps) => (
        <input
          className={['fm-input', error ? 'fm-input--error' : '', className ?? ''].filter(Boolean).join(' ')}
          {...idProps}
          {...rest}
        />
      )}
    </FieldWrap>
  );
}

export interface TextareaProps extends Omit<React.TextareaHTMLAttributes<HTMLTextAreaElement>, 'id'> {
  label?: string;
  help?: string;
  error?: string;
}

export function Textarea({ label, help, error, className, ...rest }: TextareaProps): React.ReactElement {
  return (
    <FieldWrap label={label} help={help} error={error}>
      {(idProps) => (
        <textarea
          className={['fm-textarea', error ? 'fm-textarea--error' : '', className ?? ''].filter(Boolean).join(' ')}
          {...idProps}
          {...rest}
        />
      )}
    </FieldWrap>
  );
}

export interface SelectOption { value: string; label: string; }

export interface SelectProps extends Omit<React.SelectHTMLAttributes<HTMLSelectElement>, 'id'> {
  label?: string;
  help?: string;
  error?: string;
  options: SelectOption[];
}

export function Select({ label, help, error, options, className, ...rest }: SelectProps): React.ReactElement {
  return (
    <FieldWrap label={label} help={help} error={error}>
      {(idProps) => (
        <div className="fm-select-wrap">
          <select className={['fm-select', className ?? ''].filter(Boolean).join(' ')} {...idProps} {...rest}>
            {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
      )}
    </FieldWrap>
  );
}

export interface NumberFieldProps {
  label?: string;
  help?: string;
  error?: string;
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  suffix?: string;
  disabled?: boolean;
}

export function NumberField({
  label, help, error, value, onChange, min, max, step = 1, suffix, disabled,
}: NumberFieldProps): React.ReactElement {
  return (
    <FieldWrap label={label} help={help} error={error}>
      {(idProps) => (
        <div className="fm-numberfield">
          <input
            type="number"
            className="fm-input"
            value={value}
            min={min}
            max={max}
            step={step}
            disabled={disabled}
            onChange={(e) => onChange(Number(e.target.value))}
            {...idProps}
          />
          {suffix && <span className="fm-numberfield__suffix">{suffix}</span>}
        </div>
      )}
    </FieldWrap>
  );
}

export interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  label?: string;
}

export function Switch({ checked, onChange, disabled, label }: SwitchProps): React.ReactElement {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={['fm-switch', checked ? 'fm-switch--on' : ''].filter(Boolean).join(' ')}
      onClick={() => onChange(!checked)}
    >
      <i />
    </button>
  );
}

export interface CheckboxProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  label?: string;
}

export function Checkbox({ checked, onChange, disabled, label }: CheckboxProps): React.ReactElement {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={['fm-checkbox', checked ? 'fm-checkbox--on' : ''].filter(Boolean).join(' ')}
      onClick={() => onChange(!checked)}
    >
      {checked && (
        <svg width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="#06222c" strokeWidth={2.4} aria-hidden="true">
          <path d="M3.4 8.4 6.4 11.4 12.6 5" />
        </svg>
      )}
    </button>
  );
}
