# Endoscopy (Surgical Instrument) Sample App

Real-time polyp detection on Intel hardware (CPU / iGPU / NPU) using **OpenVINO**,
with a **decoupled capture / inference / display** architecture so the displayed
video stays smooth regardless of inference speed.

This is a cleaned rewrite of the original `Endoscopy-Demo` reference:

| Original | This version |
|---|---|
| GLX/`ctypes` VSync thread driving the **camera** | display-only, portable **vsync presenter** (OpenGL), camera stays free-running |
| Software-trigger-per-vblank camera | **free-running** `LatestImageOnly` grab |
| Hardcoded CPU pinning (`cpu=2/4/6/7`) | **configurable, off by default** |
| ultralytics + hand-wired OpenVINO | **pure OpenVINO** (version-stable) |
| Basler only | **Basler, USB/V4L2, or video file** |

## Why it's fast

Display shows **every captured frame**; inference runs on its **own thread**
every Nth frame and publishes results to a shared slot. Displayed FPS is
therefore independent of inference FPS — no GStreamer / VA-API in the path.

## Display synchronization (vsync)

Capture and inference are decoupled; the **display** stage can additionally be
locked to the monitor's refresh so presentation cadence is deterministic (no
beat between the camera rate and the panel refresh). This replaces the original
GLX/ctypes vsync thread with a portable, **presentation-only** OpenGL path — the
camera stays free-running (`LatestImageOnly`) and inference still crosses only
bounding-box coordinates to the display.

Select the backend with `--presenter` (env `PRESENTER`):

| Value | Behavior |
|---|---|
| `auto` (default) | Use the OpenGL vsync presenter; fall back to `cv2` if GL is unavailable, then to headless if there is no display at all |
| `gl` | OpenGL window with `swap_interval(1)` — buffer swap blocks on the vertical blank, so display FPS locks to the refresh (e.g. flat 60.0). Falls back to `cv2` with a warning if GL can't start |
| `cv2` | Legacy `cv2.imshow` (no vsync lock) |

Pair it with `--camera-fps` set to the refresh (or a submultiple) for the
steadiest result, e.g. a 60 Hz panel:

```bash
make run-camera SERIAL=<SERIAL_NUMBER> DEVICE=GPU EXTRA="--presenter gl --camera-fps 60"
```

**Dependencies (optional, only for `--presenter gl`/`auto`):**

- Python: `glfw`, `PyOpenGL` — `pip install glfw PyOpenGL`
- System: `libglfw3` + an OpenGL/GLX runtime (`libgl1 libglx-mesa0 libgl1-mesa-dri`),
  a reachable display (X11 socket + `DISPLAY`, or Wayland), and the GPU render
  node `/dev/dri` (the Makefile already passes X11 + `/dev/dri` through).

The Docker image bundles all of the above. They are **lazily imported**, so
`--headless`, `--source file`, and CI runs need none of them.

**Fallback / headless behavior:** if the GL libraries are missing, `glfwInit()`
or context creation fails (no display server, unsupported GLX/EGL), or the panel
isn't reachable, the app logs the reason and degrades `gl → cv2 → headless`
instead of crashing. `--headless` skips the display entirely (log/record only).

## Models & video dataset

The container needs an **OpenVINO IR** (the trained model) and, for file mode, a
**test video**, both supplied from the host via bind-mounts. The Makefile mounts
`../models → /models` and `../videos → /videos` by default (override with
`MODELS_DIR` / `VIDEOS_DIR`).

Expected host layout (next to this repo):

```
models/yolo11n_polyp/best_openvino_model/best.xml   (+ best.bin)
videos/polyp_test.mp4
```

This demo does **not** train — it consumes an IR produced by the
`Surgical_Instrument` suite (YOLO11n trained on **CVC-ColonDB**). How that
dataset and IR are created:

1. Download the **CVC-ColonDB** archive from the CVC lab
   (`https://pages.cvc.uab.es/CVC-Colon/index.php/databases/`) after accepting
   their research-use terms.
   *Citation: Bernal, Sánchez, Vilariño (2012), Pattern Recognition 45(9), 3166–3182.*
2. Drop the archive (or extracted folder) into the suite's dataset input at
   `Surgical_Instrument/datasets/CVC-ColonDB/raw/` (accepts `.zip`, `.tar`,
   `.tar.gz`, `.tgz`).
3. On first boot the suite auto-detects the images + masks, converts the binary
   masks to YOLO bounding-box labels, splits 70/15/15, writes `data.yaml`, then
   trains YOLO11n on the Intel Arc iGPU and exports to an OpenVINO IR at
   `models/yolo11n_polyp/best_openvino_model/best.xml`.

Place that resulting IR under `models/` and a test clip under `videos/` (host
paths above) and this demo is ready to run — the presence of
`models/yolo11n_polyp/best_openvino_model/best.xml` is all it needs.

## Start the app

Build once; `make` auto-detects the `render`/`video` GIDs, `/dev/dri`, the X11
socket, and (for camera) USB passthrough.

```bash
make build
```

Discover the camera serial and CPU core layout:

```bash
make list-cameras   # prints Basler serial(s) + model -> use as SERIAL=
make show-cores     # prints CPU topology; P-cores have the highest MAXMHZ -> --cpu-*
```

**Basler live camera** — pass the camera serial:

```bash
# With display
make run-camera SERIAL=<SERIAL_NUMBER> DEVICE=GPU

# GL vsync-locked display + sensor capped to 60 fps
make run-camera SERIAL=40067928 DEVICE=GPU EXTRA="--presenter gl --camera-fps 60"

# Lock the sensor to the display refresh (e.g. 60 Hz) to remove cadence jitter
make run-camera SERIAL=<SERIAL_NUMBER> DEVICE=GPU EXTRA="--camera-fps 60"

# Headless
make run-camera SERIAL=<SERIAL_NUMBER> DEVICE=GPU EXTRA="--headless"

# With core pinning + real-time priority (the Makefile grants CAP_SYS_NICE)
make run-camera SERIAL=<SERIAL_NUMBER> DEVICE=GPU \
  EXTRA="--cpu-capture 1 --cpu-inference 2 --cpu-display 3 --rt-priority 20"

# Headless + core pinning
make run-camera SERIAL=<SERIAL_NUMBER> DEVICE=GPU \
  EXTRA="--headless --cpu-capture 1 --cpu-inference 2 --cpu-display 3 --rt-priority 20"
```

**Video file** — with display (loops) vs. headless (FPS/latency logged only):

```bash
make run-file VIDEO=/videos/polyp_test.mp4 DEVICE=GPU
make run-file VIDEO=/videos/polyp_test.mp4 DEVICE=GPU EXTRA="--headless"
```

`DEVICE` accepts `GPU | CPU | NPU`; append any extra app flag through `EXTRA`.

## Run locally (without Docker)

```bash
pip install -r requirements.txt
# live Basler camera only:
pip install pypylon
```

```bash
# Video file (loops)
python app.py --source file --source-arg /videos/polyp_test.mp4 --device GPU

# Basler live camera (first camera; or pass a serial)
python app.py --source basler --source-arg <SERIAL_NUMBER> --device GPU

# USB / V4L2 webcam (device index)
python app.py --source v4l2 --source-arg 0 --device GPU
```

Press **ESC** to quit.

## Key options (CLI flag / env var)

| Flag | Env | Default | Purpose |
|---|---|---|---|
| `--source` | `SOURCE` | `basler` | `file` \| `v4l2` \| `basler` |
| `--source-arg` | `SOURCE_ARG` | `""` | path / device index / camera serial |
| `--device` | `DEVICE` | `GPU` | `CPU` \| `GPU` \| `NPU` |
| `--model` | `MODEL` | `.../best.xml` | OpenVINO IR path |
| `--threshold` | `THRESHOLD` | `0.5` | detection confidence |
| `--iou` | `IOU` | `0.45` | NMS IoU |
| `--frame-skip` | `FRAME_SKIP` | `1` | infer every Nth frame (raise to lighten GPU) |
| `--width/--height` | `WIDTH/HEIGHT` | `1280/720` | capture resolution |
| `--camera-fps` | `CAMERA_FPS` | `0` | Basler capture-rate cap (0 = free-running); set to a submultiple of the display refresh to remove cadence jitter |
| `--headless` | `HEADLESS` | off | no window (benchmark / server) |
| `--presenter` | `PRESENTER` | `auto` | display backend: `auto` (GL vsync, else cv2) \| `gl` (vsync-locked) \| `cv2` (legacy imshow) |
| `--record` | `RECORD` | — | write annotated `.mp4` |
| `--display-scale` | `DISPLAY_SCALE` | `1.0` | window scale |
| `--detection-ttl-ms` | `DETECTION_TTL_MS` | `200` | how long a detection stays overlaid |
| `--no-loop` | `LOOP=0` | loop on | stop file at EOF instead of looping |
| `--exposure-us` / `--gain` | `EXPOSURE_US`/`GAIN` | auto | Basler manual exposure/gain |

### Optional core pinning (off by default)

Portable by default (no affinity). Enable per-thread only where the platform
topology is known:

```bash
python app.py --source basler \
  --cpu-capture 4 --cpu-inference 6 --cpu-display 7 --rt-priority 80
```

`--rt-priority > 0` uses `SCHED_FIFO` (needs `CAP_SYS_NICE` / root); it silently
falls back to normal scheduling if not permitted.

## Usability / maintainability additions

- **One interface for all sources** (`sources.py`) — camera and file are
  interchangeable; add a new source by implementing `read()`.
- **Pure-OpenVINO detector** (`detector.py`) — no ultralytics version coupling;
  standard YOLO letterbox + numpy NMS.
- **Headless + `--record`** for CI/benchmark runs and demo capture without a display.
- **On-screen HUD + periodic log**: display / capture / inference FPS and
  inference latency, so regressions are obvious.
- **Env-var or CLI** config (CLI wins) — friendly for both `docker run -e` and
  interactive use.
- **Graceful shutdown** on ESC / SIGINT / SIGTERM.
- **GPU config baked in**: `GPU_DISABLE_WINOGRAD_CONVOLUTION`, `NUM_STREAMS=1`,
  `ALLOW_AUTO_BATCHING=NO`, `PERFORMANCE_HINT=LATENCY`, plus a startup warmup
  inference so the first frames aren't cold-GPU throttled.

## Files

```
updated-endoscopy-demo/
├── app.py           # threads (capture / inference / display), HUD, record
├── detector.py      # OpenVINO IR load, letterbox preprocess, YOLO decode + NMS
├── sources.py       # FileSource / V4L2Source / BaslerSource + factory
├── display.py       # vsync OpenGL presenter + cv2 fallback + factory
├── config.py        # CLI + env configuration
├── requirements.txt
└── README.md
```

> Research/reference implementation for evaluating Intel inference performance —
> not a medical device.
