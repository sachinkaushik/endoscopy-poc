# Endoscopy POC — Low-Latency Changes

Summary of the work done to close the latency gap between the reference
(`Endoscopy-Demo-main`) and this POC (`endoscopy-poc`), plus the new instrumentation
and display path added while chasing the client's photon-to-pixel latency target.

---

## 1. Background

The reference demo achieved low **photon-to-pixel** latency through a
**vblank-synchronized, software-triggered camera capture**. The POC had originally
*decoupled and removed* that mechanism (free-running capture + display-only vsync),
which reintroduced the capture↔refresh beat and raised latency.

The changes below port the reference's latency-reduction logic into the POC's clean
architecture, behind config flags, and add tooling to measure and further reduce
end-to-end latency.

---

## 2. What was missing (reference vs original POC)

| Reference logic | Purpose | Original POC |
|---|---|---|
| VSync thread (`GLX_OML_sync_control`) | Hardware vblank master clock | Removed |
| Software-triggered camera synced to vblank | Expose just-in-time for display | Removed (free-run `LatestImageOnly`) |
| Fixed short exposure (2000 µs) | Deterministic, tiny shutter latency | Optional, unset by default |
| Per-stage latency instrumentation (CSV) | Measure photon-to-pixel path | Only rolling FPS HUD |
| `graph.py` (Arduino photon-to-pixel rig) | Ground-truth latency measurement | Not ported (external tooling) |

---

## 3. Changes made

### 3.1 Phase-lock via present-completion trigger (`app.py`)
- `PresentSignal`: a present-completion tick. The **fullscreen, vsync-locked GL
  presenter** bumps a sequence right after `swap_buffers()` returns from the vblank;
  the capture thread waits on it and fires the software trigger — so exposure is
  phase-locked to the **real scanout, in the same direct-scanout path**.
- This replaces the earlier separate `VSyncClock` (its own GLFW context) + cv2
  detour, which put the compositor back in the present path. One path now does both
  the direct-scanout present and the capture timing.
- Bootstrap/robustness: capture waits with a 0.1 s timeout, so it self-primes at
  startup and keeps running if presents ever stall.
- `--vsync-divisor` still supported (capture every Nth vblank), default **1**.

### 3.2 `sources.py` — `BaslerSource` trigger modes
- New `trigger` parameter: `off | software | vsync`.
  - `off`  — free-running `GrabStrategy_LatestImageOnly` (original behaviour).
  - `software` / `vsync` — `TriggerMode=On`, `TriggerSource=Software`; each `read()`
    does `WaitForFrameTriggerReady` + `ExecuteSoftwareTrigger` + `RetrieveResult`
    (`GrabStrategy_OneByOne`), so the frame is exposed **on demand / just-in-time**.
- **Exposure is left as the camera has it** unless `--exposure-us` is given. We no
  longer silently force 2000 µs — endoscopy illumination must be clinically
  validated, so lowering exposure (less latency/motion-blur, darker image) is now an
  explicit, opt-in choice.

### 3.3 `display.py` — fullscreen presenter (immediate **or** vblank-locked)
- `GLPresenter(fullscreen=..., vsync_lock=...)`:
  - `fullscreen` → real fullscreen window, unredirected by the compositor
    (**direct scanout**), removing the 1–2 frames of compositor buffering.
  - `vsync_lock=True` → `swap_interval(1)` (blocks on vblank; drives the
    present-completion trigger). `vsync_lock=False` → `swap_interval(0)` (immediate,
    lowest latency; used by free-run + fullscreen).
- `create_presenter(..., fullscreen, vsync_lock)` threads both through.

### 3.4 `app.py` display / capture
- `Captured` packet carries per-frame `t_trigger_ns` / `t_grab_ns` for the CSV.
- `display_loop`:
  - Chooses `vsync_lock = (camera_trigger == "vsync") or (not fullscreen)` so vsync
    mode is vblank-locked while software+fullscreen stays immediate.
  - Emits per-frame CSV:
    `clock_time, trigger_to_grab_ms, grab_to_display_ms, trigger_to_display_ms, infer_ms, disp_fps, cap_fps`.
  - **Annotate-once**: held frames re-present a cached drawn buffer (no copy/redraw
    per idle loop). Notifies `PresentSignal` after each present.
- `main`: in vsync mode forces `--presenter gl` (cv2 can't vblank-lock) and warns if
  `--fullscreen` is off.

### 3.5 `config.py` — flags / env vars
| Flag | Env | Default | Purpose |
|---|---|---|---|
| `--camera-trigger {off,software,vsync}` | `CAMERA_TRIGGER` | `off` | capture mode |
| `--vsync-divisor N` | `VSYNC_DIVISOR` | `1` | capture every Nth vblank (1 = every refresh) |
| `--fullscreen` | `FULLSCREEN` | off | GL fullscreen direct-scanout (bypass compositor) |
| `--latency-trace` | `LATENCY_TRACE` | off | emit per-stage CSV |

### 3.6 `Dockerfile` / `Makefile`
- `Makefile`: `run-camera-lowlatency` target; `CAMERA_TRIGGER` (default `software`),
  `VSYNC_DIVISOR` (default `1`), `EXPOSURE_US` (default empty = camera as-is),
  `FULLSCREEN` (default `1`). vsync runs now assemble
  `--camera-trigger vsync --presenter gl --fullscreen`.
- `Dockerfile`: copies the five app modules (the earlier standalone `vsync.py` was
  removed once superseded by the present-completion trigger).


---

## 4. How to run

A single `run-camera` target; `LOWLATENCY=1` flips the low-latency profile, and
individual knobs override it.

```bash
make build

# Baseline (free-running capture, windowed) — for comparison
make run-camera SERIAL=<SERIAL_NUMBER>

# Low-latency profile: software trigger + fullscreen direct-scanout + latency CSV
make run-camera SERIAL=<SERIAL_NUMBER> LOWLATENCY=1

# Phase-locked (sticks to the bottom): capture driven by the fullscreen present's vblank
make run-camera SERIAL=<SERIAL_NUMBER> LOWLATENCY=1 CAMERA_TRIGGER=vsync

# Variants
make run-camera SERIAL=<SERIAL_NUMBER> LOWLATENCY=1 FULLSCREEN=0        # windowed
make run-camera SERIAL=<SERIAL_NUMBER> LOWLATENCY=1 EXPOSURE_US=500     # shorter shutter (darker) — validate illumination
```

The CSV streams to stdout; compare `trigger_to_display_ms` between baseline and
`LOWLATENCY=1` to quantify the improvement. Note this is a *relative* internal
metric — it ends at frame hand-off and excludes present→GPU→scanout + exposure,
so it reads lower than an Arduino photon-to-pixel rig.

---

## 5. Latency findings & expectations

Measured on the target setup (**60 Hz monitor, desktop compositor on**):

- Software-trigger capture runs at full rate (~120 fps capture/display, ~8.5 ms
  inference) with `trigger_to_display` ~15–35 ms.
- Client observed ~40 ms **photon-to-pixel** end-to-end.

### Where the ~40 ms goes (photon → pixel)
| Stage | Approx | In our code? |
|---|---|---|
| Exposure integration | 1–2 ms | yes (`EXPOSURE_US`) |
| Sensor readout + USB3 transfer | ~3–6 ms | no (camera/link) |
| Host pipeline (convert, queue, draw) | ~2–4 ms | yes |
| Present → GPU → **compositor** | ~16–33 ms | partly (fullscreen bypass) |
| Monitor scanout + panel response | ~8–20 ms | **no** (display hardware) |

### Realistic targets
- **On a 60 Hz monitor**, sub-~16 ms photon-to-pixel is **physically impossible**
  (one refresh period), regardless of software. Fullscreen direct-scanout should bring
  the client from ~40 ms to roughly **16–24 ms**.
- **2–3 ms is not achievable** with this architecture (USB3 camera + Python + OpenVINO
  + standard monitor). Single-digit ms requires:
  - a **144–240 Hz+ low-lag monitor** (240 Hz frame = 4.2 ms),
  - compositor off / kiosk session + fullscreen,
  - and even then the practical floor is ~8–12 ms.
- Sub-3 ms photon-to-pixel means a dedicated hardware path (global-shutter camera →
  FPGA / direct GPU passthrough → strobed high-refresh panel), a different architecture.

---

## 6. Files touched

- Modified: `app.py`, `config.py`, `sources.py`, `display.py`, `Dockerfile`, `Makefile`
- Not ported (external tooling): `graph.py` (Arduino photon-to-pixel measurement rig)
