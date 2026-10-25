# P2 → P3, P4: proposal: a local map the Pi can pull from the mapper (post-I3, optional)

**From:** Shrujan (P2, rover runtime) · **To:** Aditi (P3, mapper), Rohan (P4, contracts) · **Date:** 2026-10-24
**Status:** proposal only. **Nothing to build before I3.** This is here so it can be scheduled, not to change anything now.

## The idea

The GX10 already holds the fused map (mapper global tiles). The rover could ask it for the
patch around its current pose and use it on board.

## Why it isn't in the contract today (and doesn't need to be yet)

The brain plans and the Pi only runs primitives (`drive`, `turn`, `station`, `approach`).
The map's consumers are the brain and the dashboard, and the Pi has no decision that needs
one. 3D rendering is a dashboard concern.

## Where it would pay off

1. **Relocalization**: correlate the station's BEV tile against the global map around the
   dead-reckoned pose and correct x/y/heading. Heading drift is our biggest accuracy risk
   (5° ≈ 13 cm at 1.5 m, bigger than the 0.20 m exclusion margin). The overhead ArUco
   tracker → brain `set_pose` already gives an absolute pose more cheaply, so this matters
   only if the overhead camera can't see the whole arena.
2. Obstacle masks for `approach`.

## Shape, if we do it

- **Pull, not push**: the Pi calls the mapper over HTTP from its op worker thread when it
  needs the map. Never over the `:8765` WS, where a large frame would delay heartbeats and
  acks.
- `GET http://192.168.50.10:8200/map/local?run_id=…&x=…&y=…&r_m=1.0` → `image/png`
  (grayscale, mission frame, image up = +x), with headers or a JSON sidecar giving
  `res_m_px` and the mission-frame `origin_xy` of pixel (0,0). Units follow doc 00 §4.
- A contract bump through AGENTS.md §3 when it's scheduled.
