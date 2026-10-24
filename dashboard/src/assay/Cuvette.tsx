import { useLayoutEffect, useState } from 'react';
import { CUVETTE_CAPTION, DYE_BROWN, SIMULATED_BANNER, liquidColor } from './format';

type Props = { a595: number; autoplay: boolean };

// SVG cuvette. On open, and again when replay reaches the sample (autoplay), the liquid
// moves from unbound brown to the A595 blue over 1.5 s. Reduced motion jumps to the end.
export default function Cuvette({ a595, autoplay }: Props) {
  const final = liquidColor(a595);
  const [color, setColor] = useState(final);
  const [snap, setSnap] = useState(true);

  useLayoutEffect(() => {
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduce) {
      setSnap(true);
      setColor(final);
      return;
    }
    setSnap(true);
    setColor(DYE_BROWN);
    const id = requestAnimationFrame(() => {
      setSnap(false);
      setColor(final);
    });
    return () => cancelAnimationFrame(id);
  }, [final, autoplay]);

  return (
    <figure className="cuvette-wrap">
      <svg className="cuvette" viewBox="0 0 80 120" role="img" aria-label={SIMULATED_BANNER}>
        <path d="M28 6 h24 v10 h-24 z" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <rect x="18" y="16" width="44" height="92" rx="3" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <rect
          className={snap ? 'cuvette-liquid snap' : 'cuvette-liquid'}
          x="21"
          y="36"
          width="38"
          height="69"
          rx="1"
          fill={color}
        />
        <path d="M21 36 h38" stroke="#000" strokeOpacity="0.25" />
      </svg>
      <figcaption>{CUVETTE_CAPTION}</figcaption>
    </figure>
  );
}
