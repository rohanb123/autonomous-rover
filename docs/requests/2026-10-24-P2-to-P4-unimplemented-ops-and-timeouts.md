# P2 → P4: what the real Pi answers for ops I haven't built, plus two timing heads-ups

**From:** Shrujan (P2, rover runtime) · **To:** Rohan (P4, brain/integration lead) · **Date:** 2026-10-24
**Status:** open — item 1 is an FYI, item 2 is a question for you, items 3–5 are heads-ups

The real Pi runtime now answers the brain: `rover/rover/ws_server.py` + `main.py` serve
`ws://…:8765/rover`, and `start_run`, `drive`, `turn`, `stop`, `estop`, `clear_estop`,
`set_pose` and `heartbeat` all work for real, with 5 Hz telemetry and the 1.0 s watchdog. You
can point `RoverLink` at it today:

```
python rover/rover/main.py --motors sim --servos off --imu off --camera sim
```

`--motors sim` records the pin writes instead of energising a TB6612 but runs the real timing
loop, the real `Safety` and the real watchdog, so the link behaves exactly as it will on
hardware. That covers the I0 check "brain ↔ Pi WS stub; heartbeat flows".

## 1. Unbuilt ops answer `E_INTERNAL`, not a plausible physical code (FYI)

`station`, `approach`, `scoop`, `scoop_pose`, `dump` and `snapshot` are not built yet. They
reply `E_INTERNAL` with a message naming the missing module, e.g.

```
E_INTERNAL  station needs bev/proposals/local_store/uploader (P2 lands them at I2)
E_INTERNAL  approach is not implemented yet (P2 I3); not E_LOST_TARGET, nothing was tracked
```

I deliberately did **not** use the codes that look more natural, because your FSM launders
them into physical outcomes:

| Code | What `gx10/brain/fsm.py` does with it | Why that is wrong here |
|---|---|---|
| `E_LOST_TARGET` | line 527 → `_record_visit(c, "unreachable")` (line 493) and carries on to the next candidate | The run would silently record "we tried and couldn't reach it" for a module that does not exist |
| `E_SCOOP` | line 500 swallows it → `outcome = "empty_scoop"` (line 490), retries once | Invents a sample with no assay, and burns a retry |
| `E_CAMERA` | propagates, run STOPPED | Honest about failing, dishonest about why — a camera *is* attached |
| **`E_INTERNAL`** | `_guarded` (line 265) logs at `error` and stops the run | Loud, visible in the dashboard EventLog, impossible to mistake for a physical result |

**The ask: please don't special-case `E_INTERNAL` into a recoverable path.** Right now it
stopping the run is exactly what I want — it means nobody demos a mission and reads a
fabricated `empty_scoop` as a real result.

The same rule covers a missing *device*: `drive` with `--motors off` answers
`E_INTERNAL  drive needs the motors, which is not attached (use --motors sim …)`.

Note `scoop_pose` and `dump` are pure servo moves — they will work for real the moment P1
wires the PCA9685, since `servos.py` is already built and tested. Only `scoop{sample_id}`
needs the uploader.

## 2. Question for you: do you want an `E_UNIMPLEMENTED` code?

`shared/py/marsrover_shared/schemas.py` is yours, so this is your call.

**My recommendation: no.** It would mean editing doc 00 §6.2, bumping CONTRACT_VERSION
1.3.0 → 1.4.0, you editing `schemas.py`, and updating four role docs' contract copies — for a
condition that disappears at I2. `E_INTERNAL` plus an honest `msg` carries the same
information, and `Err.msg` is already a free string.

If you disagree, say so before I2 and I will switch.

## 3. FYI: `Event.kind` has no value for these, so they go to events.jsonl

`Event.kind` is a closed Literal (`tilt|bump|stall|upload_retry|camera_reset`) with nothing
for "op refused, module not built" or "device absent". Rather than ask you to widen it, those
go to `events.jsonl` (whose `kind` is a free `str`) with `kind="unimplemented"` and
`kind="no_device"`. **No contract change requested** — just so you know where to look when the
dashboard EventLog shows nothing for a refusal.

## 4. Heads-up: a long slow `drive` can outlive `RoverLink`'s 30 s done timeout

`gx10/brain/rover_link.py:30` sets `DEFAULT_DONE_TIMEOUT_S = 30.0`, and `drive` isn't in
`DONE_TIMEOUT_S`, so it gets the default.

With real wheels at the currently-planned 0.12–0.20 m/s, a 1.5 m leg is 8–13 s, which is
fine. But a low-speed leg is not: `drive(dist_m=1.5, speed=0.3)` is **12.5 s at 0.12 m/s**,
and at `speed=0.3` over 3 m it is 25 s and climbing. Once P1's real numbers land, slower
speeds are plausible.

If the client times out while the Pi is still happily driving, you get a spurious `E_TIMEOUT`
and the Pi keeps going — the brain and the rover disagree about what is happening. Two ways
out, your pick: pass an explicit `timeout_s` for long legs, or keep `dist_m` per `drive` to
about 1.5 m (which `fsm._go_to` mostly already does).

I handle the mirror image on my side: when *my* per-op deadline fires I now call
`backend.request_stop()` before replying, so the rover doesn't keep driving after you've been
told the op failed.

## 5. FYI: docs/05 resolutions coming from my Pi session

doc 05 is yours, so I won't edit it. After I run on the Pi I will send the answers for U-20
(`cv2.aruco` on the Pi), U-21 (does the webcam honour a manual exposure lock), U-7 (minimum
focus distance) and U-2's negative branch as a follow-up request.

Two I can resolve from the laptop already: **U-23 is settled** — `servos.py` uses plain
`smbus2` register writes rather than Adafruit Blinka (one fewer dependency, no `board`/`busio`
Pi-5 question), and `prescale_for`/`us_to_ticks` are unit-tested against the datasheet, so
U-23 now only risks the I2C transport. **U-1** is Pi 5, so `gpiozero` + `lgpio`.

## 6. Unrelated, but it breaks the "main must always run make test" rule

`gx10/brain/tests/test_fsm.py::test_look_only_and_time_up` is flaky — **3 failures in 5 runs**
on its own, on a clean checkout, unrelated to my changes. It races a `max_duration_s` of
0.01 s against the FSM getting through two visits, and on a fast machine the visits win:

```
assert not g.db.visits and g.fsm.state == "DONE"
E  AssertionError: assert (not [Visit(visit_id='v_0001', …, outcome='sampled'), …])
```

Not mine to fix (`gx10/brain/**` is yours), but docs/04 §4 says `main` must always run
`make test`, so it will keep tripping gates. Probably wants a deterministic clock rather than
a 10 ms deadline.
