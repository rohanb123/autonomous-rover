// base64 PNG tiles -> ImageBitmap, decoded once per row version (docs/06 §9 pitfalls).
// Shared by the 3D and 2D map renderers.
import { useEffect, useMemo, useRef, useState } from 'react';
import type { GlobalTile, LocalMapTile } from '../module_bindings/types';
import * as tiles from './layers/tiles';

export interface ImageCache {
  get: tiles.ImageLookup;
  /** Changes whenever a new image finishes decoding. */
  tick: number;
}

export function useDecodedImages(localTiles: readonly LocalMapTile[], globalTiles: readonly GlobalTile[]): ImageCache {
  const cache = useRef(new Map<string, ImageBitmap | 'pending' | 'failed'>());
  const [tick, setTick] = useState(0);
  // Component-level (not per-effect) liveness: a decode started by one effect run must still
  // trigger a redraw when it finishes after a re-run (React StrictMode runs effects twice).
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    const wanted = new Map<string, string>();
    for (const t of localTiles) if (t.pngB64) wanted.set(tiles.localKey(t), t.pngB64);
    for (const t of globalTiles) if (t.pngB64) wanted.set(tiles.globalKey(t), t.pngB64);
    const map = cache.current;
    for (const [key, entry] of map) {
      if (!wanted.has(key)) {
        if (entry instanceof ImageBitmap) entry.close();
        map.delete(key);
      }
    }
    for (const [key, b64] of wanted) {
      if (map.has(key)) continue;
      map.set(key, 'pending');
      decodePng(b64)
        .then((bmp) => {
          if (map.get(key) === 'pending') map.set(key, bmp);
          else bmp.close();
          if (mounted.current) setTick((n) => n + 1);
        })
        .catch(() => map.set(key, 'failed'));
    }
  }, [localTiles, globalTiles]);

  // A new object per decode tick so draw effects re-run.
  return useMemo(
    () => ({
      get: (key: string) => {
        const e = cache.current.get(key);
        return e instanceof ImageBitmap ? e : undefined;
      },
      tick,
    }),
    [tick]
  );
}

async function decodePng(b64: string): Promise<ImageBitmap> {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return createImageBitmap(new Blob([bytes], { type: 'image/png' }));
}
