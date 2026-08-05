# Updated Endoscopy Demo

Real-time polyp detection on Intel hardware (CPU / iGPU / NPU) using **OpenVINO**,
with a **decoupled capture / inference / display** architecture so the displayed
video stays smooth regardless of inference speed.

This is a cleaned rewrite of the original `Endoscopy-Demo` reference:

| Original | This version |
|---|---|
| GLX/`ctypes` VSync thread (X11-only) | **removed** (not needed for the 30 ms target) |
| Software-trigger-per-vblank camera | **free-running** `LatestImageOnly` grab |
| Hardcoded CPU pinning (`cpu=2/4/6/7`) | **configurable, off by default** |
| ultralytics + hand-wired OpenVINO | **pure OpenVINO** (version-stable) |
| Basler only | **Basler, USB/V4L2, or video file** |

## Why it's fast

Display shows **every captured frame**; inference runs on its **own thread**
every Nth frame and publishes results to a shared slot. Displayed FPS is
therefore independent of inference FPS — no GStreamer / VA-API in the path.

## Install

```bash
pip install -r requirements.txt
# live Basler camera only:
pip install pypylon
```

## Run

```bash
# Video file (loops)
python app.py --source file --source-arg /videos/polyp_test.mp4 --device GPU

# Basler live camera (first camera; or pass a serial)
python app.py --source basler --source-arg 40067928 --device GPU

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
| `--headless` | `HEADLESS` | off | no window (benchmark / server) |
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
├── config.py        # CLI + env configuration
├── requirements.txt
└── README.md
```

> Research/reference implementation for evaluating Intel inference performance —
> not a medical device.
