// MapView (P3): map of one run (docs/06 §4, §6). Rows come in via props from the P4 shell; this
// component never opens its own STDB connection and never writes anything. Replay filtering by
// scrub time is done by the shell (useScrubbed) before rows reach here.
//
// Three views of the same rows:
//   3D   (default) WebGL terrain draped with the ground photos, orbit camera (MapView3D)
//   Top  the same 3D renderer looking straight down: +x up, +y left, like the 2D map
//   2D   the classic canvas map (MapView2D); also the automatic fallback without WebGL2
import { useState } from 'react';
import MapView2D from './MapView2D';
import MapView3D from './MapView3D';
import type { MapViewProps } from './mapdata';
import './map.css';

export type { MapViewProps, NextStop } from './mapdata';
export { defaultBounds, nextStop, tileVisibility } from './mapdata';

type ViewMode = '3d' | 'top' | '2d';
const VIEW_KEY = 'marsrover.mapview.view';
const VIEWS: { mode: ViewMode; label: string; title: string }[] = [
  { mode: '3d', label: '3D', title: '3D terrain view (right-drag to rotate)' },
  { mode: 'top', label: 'Top', title: 'Straight down: +x up, +y left' },
  { mode: '2d', label: '2D', title: 'Classic 2D map' },
];

function loadView(): ViewMode {
  try {
    const v = window.localStorage.getItem(VIEW_KEY);
    if (v === '3d' || v === 'top' || v === '2d') return v;
  } catch {
    // storage blocked: use the default
  }
  return '3d';
}

export default function MapView(props: MapViewProps) {
  const [view, setView] = useState<ViewMode>(loadView);
  const [glFailed, setGlFailed] = useState(false);
  const use2d = view === '2d' || glFailed;

  const choose = (mode: ViewMode) => {
    setView(mode);
    try {
      window.localStorage.setItem(VIEW_KEY, mode);
    } catch {
      // not remembered; fine
    }
  };

  return (
    <div className="mapview-root">
      {use2d ? <MapView2D {...props} /> : <MapView3D {...props} topDown={view === 'top'} onFail={() => setGlFailed(true)} />}
      <div className="mapview-switch" role="group" aria-label="Map view">
        {VIEWS.map((v) => (
          <button
            key={v.mode}
            className={(use2d ? v.mode === '2d' : v.mode === view) ? 'on' : ''}
            aria-pressed={use2d ? v.mode === '2d' : v.mode === view}
            disabled={glFailed && v.mode !== '2d'}
            title={glFailed && v.mode !== '2d' ? '3D needs WebGL2, which this browser could not start' : v.title}
            onClick={() => choose(v.mode)}
          >
            {v.label}
          </button>
        ))}
      </div>
    </div>
  );
}
