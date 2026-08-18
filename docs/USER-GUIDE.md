# Endoscopy POC — User Guide

A practical guide to every command and parameter. For architecture/overview see
`README.md`; for the change history see `CHANGES.md`.

---

## 1. Quick start

```bash
make build                                   # build the container image once

make run-camera   SERIAL=<SERIAL_NUMBER>                 # baseline live camera
make run-camera   SERIAL=<SERIAL_NUMBER> LOWLATENCY=1    # low-latency profile
make run-file     VIDEO=/videos/polyp_test.mp4           # a video file (loops)
make run-webcam   DEVICE_INDEX=0                         # USB / V4L2 webcam
```

Press **ESC** in the window to quit (or `make stop` from another terminal).
Find your camera serial with `make list-cameras`.

---

## 2. Commands (make targets)

| Command | What it runs |
|---|---|
| `make build` | Build the Docker image. Run once, and again after any code change. |
| `make run-camera SERIAL=…` | Basler live camera with display. Add `LOWLATENCY=1` for the low-latency profile. |
| `make run-file VIDEO=…` | Run on a video file (loops on EOF). Good for demos without a camera. |
| `make run-webcam DEVICE_INDEX=0` | Run on a USB/V4L2 webcam. |
| `make bench VIDEO=…` | Headless FPS benchmark (no window). |
| `make record VIDEO=…` | Write an annotated `.mp4` to `VIDEOS_DIR/annotated.mp4` (headless). |
| `make list-cameras` | List connected Basler cameras (serial + model) → use as `SERIAL=`. |
| `make show-cores` | Show P-core / E-core CPU sets for `--cpu-*` pinning. |
| `make shell` | Open an interactive shell inside the image. |
| `make stop` | Stop/remove a running container. |
| `make clean` | Stop and remove the image. |
| `make help` | List all targets. |

There is **one** camera target (`run-camera`); the low-latency behaviour is a set
of parameters on that same target, not a separate command.

---

## 3. The low-latency profile switch

| Param | Values | What it does |
|---|---|---|
| `LOWLATENCY` | `0` / `1` | Master switch. `1` turns on the whole low-latency profile at once: sets `CAMERA_TRIGGER=software`, `FULLSCREEN=1`, `LATENCY_TRACE=1`. Any individual knob you also pass overrides that profile. |

```bash
# One switch = software trigger + fullscreen direct-scanout + latency CSV
make run-camera SERIAL=<SERIAL_NUMBER> LOWLATENCY=1

# Phase-locked capture (recommended on a 60 Hz panel)
make run-camera SERIAL=<SERIAL_NUMBER> LOWLATENCY=1 CAMERA_TRIGGER=vsync
```

---

## 4. Capture parameters (how the camera produces frames)

### `CAMERA_TRIGGER` — `off` | `software` | `vsync`
The single most important latency control.

- **`off`** (baseline): the camera free-runs on its own clock; the app grabs the
  newest buffered frame (`LatestImageOnly`). A displayed frame can be up to one
  camera period old → variable "sawtooth" latency.
- **`software`**: the app fires a software trigger and the camera exposes a frame
  **on demand, just-in-time** for each capture. Removes the stale-buffer delay.
- **`vsync`**: same software trigger, but released by the **display's vblank**
  (present-completion) so exposure is **phase-locked** to the screen refresh —
  latency stops drifting and "sticks to the bottom". Requires a GL presenter
  (`--presenter gl`), which `LOWLATENCY=1` selects.

### `VSYNC_DIVISOR` — integer (default `1`)
Only used when `CAMERA_TRIGGER=vsync`. Means "trigger the camera every **Nth**
vblank":

- `1` → capture on every refresh (60 fps on a 60 Hz panel). **Use this on 60 Hz.**
- `2` → every other vblank (30 fps on 60 Hz) — only sensible on a high-refresh
  panel (120/240 Hz) where the camera can't keep up with every vblank.

Has **no effect** when `CAMERA_TRIGGER` is `off` or `software`.

### `EXPOSURE_US` — microseconds (default: empty = leave camera as-is)
Fixed shutter time.

- Empty → the camera keeps its current/operator-set exposure (does **not** touch
  clinical illumination).
- A smaller value (e.g. `1000`) lowers latency and motion blur but **darkens** the
  image — validate against the endoscope's light source before using.

---

## 5. Display parameters (how frames reach the screen)

### `FULLSCREEN` — `1` | `0`
- **`1`**: fullscreen OpenGL window; the compositor leaves it unredirected →
  **direct scanout**, removing ~1–2 frames (~16–33 ms) of compositor buffering.
  Biggest display-side latency win on a normal desktop. Press **ESC** to exit.
- **`0`**: a normal window — convenient, but the compositor is back in the path
  (higher latency), and on a 60 Hz panel the display is vsync-locked to 60 fps.

### `LATENCY_TRACE` — `1` | `0`
`1` prints a per-frame CSV to stdout:

```
clock_time, trigger_to_grab_ms, grab_to_display_ms, trigger_to_display_ms, infer_ms, disp_fps, cap_fps
```

Use it to compare baseline vs low-latency. **Caveat:** `trigger_to_display_ms`
ends when the frame is handed to the presenter — it excludes present→GPU→scanout
and exposure, so it reads **lower** than an Arduino photon-to-pixel rig. Use it
for relative before/after comparison, not as an absolute figure.

---

## 6. Model / inference parameters (same in every mode)

| Param | Values | What it does |
|---|---|---|
| `DEVICE` | `GPU` / `CPU` / `NPU` | Which Intel device runs OpenVINO inference. |
| `THRESHOLD` | `0.0`–`1.0` (default `0.5`) | Detection confidence cutoff. Lower = more (noisier) boxes. |
| `FRAME_SKIP` | integer (default `1`) | Run inference every Nth captured frame. Raise to lighten the GPU; display still shows every captured frame. |

---

## 7. Source / plumbing parameters

| Param | Values | What it does |
|---|---|---|
| `SERIAL` | Basler serial | Which camera (empty = first found). Use `make list-cameras`. |
| `DEVICE_INDEX` | integer | Webcam index for `run-webcam` (`/dev/videoN`). |
| `VIDEO` | path | Container-side video path for `run-file` / `bench` / `record`. |
| `MODEL` | path | OpenVINO IR (`best.xml`) inside the container. |
| `MODELS_DIR` / `VIDEOS_DIR` | host paths | Host folders bind-mounted to `/models` and `/videos`. |
| `EXTRA` | flag string | Any extra app flag passed straight through (see below). |

### Useful things to pass via `EXTRA`
- Core pinning + real-time priority (deterministic scheduling):
  ```bash
  EXTRA="--cpu-capture 1 --cpu-inference 2 --cpu-display 3 --rt-priority 20"
  ```
  Use `make show-cores` to pick P-core indices. `--rt-priority > 0` needs
  `CAP_SYS_NICE` (the Makefile grants it); it falls back to normal scheduling if
  not permitted.
- Capture-rate cap: `EXTRA="--camera-fps 60"` locks the sensor to a fixed FPS
  (only relevant when `CAMERA_TRIGGER=off`).
- Headless: `EXTRA="--headless"`.

---

## 8. How the parameters relate

- **`CAMERA_TRIGGER`** controls **capture** latency (free-run → just-in-time →
  phase-locked).
- **`FULLSCREEN`** controls **display** latency (compositor bypass).
- **`VSYNC_DIVISOR`** only fine-tunes the capture rate in `vsync` mode.
- **`EXPOSURE_US`** trades brightness for shutter latency/motion-blur.

Recommended on a 60 Hz panel:

```bash
make run-camera SERIAL=<SERIAL_NUMBER> LOWLATENCY=1 CAMERA_TRIGGER=vsync
# = software-trigger phase-locked to the vblank + fullscreen direct scanout + CSV
```

**Physical floor:** a 60 Hz monitor cannot show a new pixel faster than ~one
refresh (~16 ms) regardless of software. Single-digit-ms photon-to-pixel needs a
144–240 Hz+ low-lag panel with the compositor off. See `CHANGES.md` §5 for the
full latency budget.

---

## 9. Common recipes

```bash
# Baseline vs low-latency A/B (compare trigger_to_display_ms in the CSV)
make run-camera SERIAL=<SERIAL_NUMBER>                       # baseline
make run-camera SERIAL=<SERIAL_NUMBER> LOWLATENCY=1          # low-latency

# Phase-locked, windowed (debugging on a shared desktop)
make run-camera SERIAL=<SERIAL_NUMBER> LOWLATENCY=1 CAMERA_TRIGGER=vsync FULLSCREEN=0

# Low-latency + shorter exposure (only if illumination allows)
make run-camera SERIAL=<SERIAL_NUMBER> LOWLATENCY=1 EXPOSURE_US=1000

# Low-latency + core pinning + RT priority
make run-camera SERIAL=<SERIAL_NUMBER> LOWLATENCY=1 \
  EXTRA="--cpu-capture 1 --cpu-inference 2 --cpu-display 3 --rt-priority 20"

# Video-file demo, headless benchmark, recording
make run-file VIDEO=/videos/polyp_test.mp4 DEVICE=GPU
make bench    VIDEO=/videos/polyp_test.mp4
make record   VIDEO=/videos/polyp_test.mp4
```
