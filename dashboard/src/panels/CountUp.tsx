import { useEffect, useRef, useState } from 'react';

// A number that eases to its new value (600 ms ease-out) instead of jumping. Tabular figures
// (set globally) keep the digits from jittering while it counts.
export default function CountUp({ value, digits = 2, ms = 600 }: { value: number; digits?: number; ms?: number }) {
  const [shown, setShown] = useState(value);
  const from = useRef(value);
  useEffect(() => {
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    if (reduce || !Number.isFinite(value)) {
      setShown(value);
      return;
    }
    const start = performance.now();
    const a = from.current;
    let raf = 0;
    const step = (now: number) => {
      const k = Math.min(1, (now - start) / ms);
      const eased = 1 - Math.pow(1 - k, 3);
      setShown(a + (value - a) * eased);
      if (k < 1) raf = requestAnimationFrame(step);
      else from.current = value;
    };
    raf = requestAnimationFrame(step);
    return () => {
      from.current = value;
      cancelAnimationFrame(raf);
    };
  }, [value, ms]);
  return <>{shown.toFixed(digits)}</>;
}
