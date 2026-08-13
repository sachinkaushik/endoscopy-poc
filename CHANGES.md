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

### 3.1 New file: `vsync.py`
- `VSyncClock`: ports the reference `vsync_loop` as a reusable class.
- Runs a small GLFW window and uses `GLX_OML_sync_control` (`glXWaitForMscOML`) to
  publish a vblank counter + perf-clock tick via a condition variable.
- Picks the highest-refresh monitor; window is **visible** (a hidden/unmapped window
  receives no vblank events, which would stall the clock).
- Graceful: if GLFW/GLX is unavailable, `start()` returns `False` and the app falls
  back to free-running / on-demand capture.

### 3.2 `sources.py` — `BaslerSource` trigger modes
- New `trigger` parameter: `off | software | vsync`.
  - `off`  — free-running `GrabStrategy_LatestImageOnly` (original behaviour).
  - `software` / `vsync` — `TriggerMode=On`, `TriggerSource=Software`; each `read()`
    does `WaitForFrameTriggerReady` + `ExecuteSoftwareTrigger` + `RetrieveResult`
    (`GrabStrategy_OneByOne`), so the frame is exposed **on demand / just-in-time**.
- Fixed short-exposure default (2000 µs) applied automatically when triggering and no
  exposure was supplied.

### 3.3 `config.py` — new flags / env vars
| Flag | Env | Default | Purpose |
|---|---|---|---|
| `--camera-trigger {off,software,vsync}` | `CAMERA_TRIGGER` | `off` | capture mode |
| `--vsync-divisor N` | `VSYNC_DIVISOR` | `2` | trigger every Nth vblank (vsync mode) |
| `--latency-trace` | `LATENCY_TRACE` | off | emit per-stage photon-to-pixel CSV |
| `--fullscreen` | `FULLSCREEN` | off | GL fullscreen direct-scanout (bypass compositor) |

### 3.4 `app.py`
- `Captured` packet carries per-frame `t_trigger_ns` / `t_grab_ns` through the queues.
- `capture_loop`:
  - In `vsync` mode, waits on `VSyncClock` and triggers in lock-step with the vblank
    (`counter % divisor`).
  - **Safety fallback**: if the vblank clock does not advance (compositor/driver
    without working `GLX_OML`), it is disabled after ~0.75 s and capture continues at
    full rate via on-demand trigger — no freeze, no 2 fps limp.
- `inference_loop` / `display_loop`: unwrap `Captured.image`.
- `display_loop`:
  - Emits per-frame CSV:
    `clock_time, trigger_to_grab_ms, grab_to_display_ms, trigger_to_display_ms, infer_ms, disp_fps, cap_fps`.
  - **Annotate-once**: held frames re-present a cached drawn buffer instead of
    copying + redrawing every idle loop (saves a full-frame copy + redraw).
  - Keeps the window responsive (cv2 `waitKey` pumped even between new frames).
- `main`: builds/stops the `VSyncClock`; in vsync mode pairs it with the cv2 presenter
  to avoid two GLFW contexts contending; passes `--fullscreen` to the presenter.

### 3.5 `display.py` — fullscreen low-latency presenter
- `GLPresenter` gains a `fullscreen` mode:
  - Real fullscreen window (unredirected by the compositor → **direct scanout**),
    removing the 1–2 frames of compositor buffering that dominate desktop latency.
  - `swap_interval(0)` (immediate present, lowest latency; may tear).
- `create_presenter(..., fullscreen=...)` threads the option through.

### 3.6 `Dockerfile`
- Added `vsync.py` to the `COPY` layer (was missing → would have crashed
  `run-camera-lowlatency` with `ModuleNotFoundError`).
- Existing deps already cover it: `glfw`, `PyOpenGL`, and the GL/GLX runtime libs.

### 3.7 `Makefile`
- New target `run-camera-lowlatency` (low-latency capture + fullscreen + latency CSV).
- New knobs: `CAMERA_TRIGGER` (default `software`), `VSYNC_DIVISOR`, `EXPOSURE_US`
  (default `1000`), `FULLSCREEN` (default `1`).

---

## 4. How to run

```bash
make build

# Low-latency (default: software trigger + fullscreen direct-scanout + latency CSV)
make run-camera-lowlatency SERIAL=40067928

# Variants
make run-camera-lowlatency SERIAL=40067928 EXPOSURE_US=500     # shorter shutter (darker)
make run-camera-lowlatency SERIAL=40067928 FULLSCREEN=0        # windowed
make run-camera-lowlatency SERIAL=40067928 CAMERA_TRIGGER=vsync VSYNC_DIVISOR=2  # vblank-locked (needs working GLX_OML)

# Baseline for comparison (original free-running capture)
make run-camera SERIAL=40067928
```

The CSV streams to stdout; compare `trigger_to_display_ms` between `run-camera`
(free-run) and `run-camera-lowlatency` to quantify the improvement.

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

- Added: `vsync.py`
- Modified: `app.py`, `config.py`, `sources.py`, `display.py`, `Dockerfile`, `Makefile`
- Not ported (external tooling): `graph.py` (Arduino photon-to-pixel measurement rig)
