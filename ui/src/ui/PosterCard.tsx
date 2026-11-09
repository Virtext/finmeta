import React, { useState } from 'react';
import Icon from './Icon';

export interface PosterMark { text: string; tone?: 'warn' | 'danger'; }

export interface PosterCardProps {
  title: string;
  meta?: string;
  imageUrl?: string | null;
  selected?: boolean;
  locked?: boolean;
  marks?: PosterMark[];
  badges?: string[];
  selectable?: boolean;
  onClick?: () => void;
  onToggleSelect?: () => void;
}

// Renders real artwork via `imageUrl` when the caller has one; falls back to
// a plain tinted panel (never a stand-in "poster-looking" graphic) so it is
// never mistaken for real artwork in the gallery or an empty state.
export function PosterCard({
  title, meta, imageUrl, selected, locked, marks, badges, selectable, onClick, onToggleSelect,
}: PosterCardProps): React.ReactElement {
  // A 404 (item has no poster in Jellyfin yet) is normal library state, not
  // a broken-image icon the user has to puzzle over -- discovered only once
  // the <img> actually fails, since nothing here pre-checks.
  const [failed, setFailed] = useState(false);
  const cls = [
    'fm-poster',
    selected ? 'fm-poster--selected' : '',
    locked ? 'fm-poster--locked' : '',
  ].filter(Boolean).join(' ');

  return (
    <div className={cls}>
      <div className="fm-poster__shot" onClick={onClick} role={onClick ? 'button' : undefined} tabIndex={onClick ? 0 : undefined}>
        {imageUrl && !failed ? (
          <img src={imageUrl} alt={title} loading="lazy" onError={() => setFailed(true)} />
        ) : (
          <div style={{ width: '100%', height: '100%', display: 'grid', placeItems: 'center', color: 'var(--text-faint)', fontSize: 'var(--text-xs)' }}>
            no artwork
          </div>
        )}
        {selectable && (
          <span
            className="fm-poster__check"
            onClick={(e) => { e.stopPropagation(); onToggleSelect?.(); }}
          >
            {selected && <Icon name="check" size={11} />}
          </span>
        )}
        {locked && !selectable && (
          <span className="fm-poster__marks"><span className="fm-poster__mark">🔒</span></span>
        )}
        {marks && marks.length > 0 && (
          <span className="fm-poster__marks">
            {marks.map((m, i) => (
              <span key={i} className={['fm-poster__mark', m.tone ? `fm-poster__mark--${m.tone}` : ''].filter(Boolean).join(' ')}>
                {m.text}
              </span>
            ))}
          </span>
        )}
        {badges && badges.length > 0 && (
          <span className="fm-poster__badges">
            {badges.map((b, i) => <span key={i}>{b}</span>)}
          </span>
        )}
      </div>
      <div className="fm-poster__label">{title}</div>
      {meta && <div className="fm-poster__meta">{meta}</div>}
    </div>
  );
}

export interface ArtworkThumbProps {
  kind?: 'poster' | 'backdrop' | 'landscape';
  imageUrl?: string | null;
  source?: string;
  selected?: boolean;
  onClick?: () => void;
}

export function ArtworkThumb({ kind = 'poster', imageUrl, source, selected, onClick }: ArtworkThumbProps): React.ReactElement {
  const wide = kind === 'backdrop' || kind === 'landscape';
  return (
    <div
      className={['fm-artwork-thumb', wide ? 'fm-artwork-thumb--landscape' : '', selected ? 'fm-artwork-thumb--selected' : ''].filter(Boolean).join(' ')}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
    >
      {imageUrl && <img src={imageUrl} alt="" loading="lazy" />}
      {source && <span className="fm-artwork-thumb__src">{source}</span>}
    </div>
  );
}
