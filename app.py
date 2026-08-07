"""Updated endoscopy demo — decoupled capture / inference / display.
 
Architecture (their proven design, cleaned):
  * Capture thread  : pulls newest frame from the source, feeds display + (every
                      Nth frame) inference. Never blocks on inference.
  * Inference thread: OpenVINO detection on the latest frame -> shared results.
  * Display (main)  : draws EVERY captured frame with the latest detections
                      overlaid, so displayed FPS is independent of inference FPS.
 
Removed vs the original reference: the GLX/ctypes VSync thread and the
software-trigger-per-vblank coupling. The display can still be locked to the
monitor's refresh (portable, presentation-only) via the OpenGL presenter in
``display.py`` (``--presenter gl``/``auto``); inference stays decoupled and only
box coordinates cross to the display. Core pinning is retained but fully
configurable and off by default.
"""
from __future__ import annotations
 
# IMPORTANT: load the Basler (pypylon) runtime BEFORE OpenCV. Both bundle their
# own native image codecs (libjpeg etc.); if OpenCV loads first it replaces the
# JPEG error handler that pylon expects, and pylon's longjmp then lands on a
# freed stack frame -> "longjmp causes uninitialized stack frame" abort.
# Optional import so --source file / --source v4l2 users need not install pypylon.
try:  # noqa: SIM105
    import pypylon.pylon  # noqa: F401
except Exception:
    pass
 
import logging
import os
import queue
import signal
import threading
import time
 
import cv2
import numpy as np
 
from config import Config, parse_config
from detector import Box, Detector
from display import create_presenter
from sources import Source, create_source
 
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s: %(message)s")
log = logging.getLogger("app")
 
shutdown = threading.Event()
 
 
class LatestDetections:
    """Thread-safe most-recent detection result with a timestamp."""
 
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._boxes: list[Box] = []
        self._ts_ns = 0
 
    def set(self, boxes: list[Box], ts_ns: int) -> None:
        with self._lock:
            self._boxes, self._ts_ns = boxes, ts_ns
 
    def get(self) -> tuple[list[Box], int]:
        with self._lock:
            return list(self._boxes), self._ts_ns
 
 
class Rate:
    """Rolling FPS/latency counter."""
 
    def __init__(self) -> None:
        self._t = time.monotonic()
        self._n = 0
        self.fps = 0.0
        self._lat_ms = 0.0
 
    def tick(self, latency_ms: float | None = None) -> None:
        self._n += 1
        if latency_ms is not None:
            self._lat_ms = 0.9 * self._lat_ms + 0.1 * latency_ms
        now = time.monotonic()
        if now - self._t >= 1.0:
            self.fps = self._n / (now - self._t)
            self._t, self._n = now, 0
 
    @property
    def latency_ms(self) -> float:
        return self._lat_ms
 
 
def _pin(cpu: int | None, rt_priority: int, label: str) -> None:
    if cpu is None and rt_priority <= 0:
        return
    try:
        if cpu is not None:
            os.sched_setaffinity(0, {cpu})
        if rt_priority > 0:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(rt_priority))
        log.info("%s pinned: cpu=%s rt_priority=%s", label, cpu, rt_priority)
    except (PermissionError, OSError) as exc:
        log.warning("%s pinning skipped (%s) — continuing unpinned", label, exc)
 
 
def _put_latest(q: "queue.Queue[np.ndarray]", frame: np.ndarray) -> None:
    """Non-blocking put that keeps only the newest frame."""
    try:
        q.put_nowait(frame)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(frame)
        except queue.Full:
            pass
 
 
def capture_loop(cfg: Config, src: Source, display_q, infer_q, cap_rate: Rate) -> None:  # noqa: ANN001
    _pin(cfg.cpu_capture, cfg.rt_priority, "capture")
    frame_interval = 1.0 / src.fps if (not src.is_live and src.fps > 0) else 0.0
    n = 0
    next_t = time.monotonic()
    while not shutdown.is_set():
        frame = src.read()
        if frame is None:
            if not src.is_live:
                shutdown.set()
                break
            continue
        n += 1
        cap_rate.tick()
        _put_latest(display_q, frame)
        if n % cfg.frame_skip == 0:
            _put_latest(infer_q, frame)
        # Pace file playback to its native FPS (live sources self-pace).
        if frame_interval:
            next_t += frame_interval
            sleep = next_t - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_t = time.monotonic()
 
 
def inference_loop(cfg: Config, det: Detector, infer_q, latest: LatestDetections, inf_rate: Rate) -> None:  # noqa: ANN001
    _pin(cfg.cpu_inference, cfg.rt_priority, "inference")
    while not shutdown.is_set():
        try:
            frame = infer_q.get(timeout=0.1)
        except queue.Empty:
            continue
        t0 = time.perf_counter()
        try:
            boxes = det.infer(frame)
        except Exception as exc:  # noqa: BLE001
            log.warning("inference error: %s", exc)
            continue
        latest.set(boxes, time.perf_counter_ns())
        inf_rate.tick((time.perf_counter() - t0) * 1000.0)
 
 
def display_loop(cfg: Config, src: Source, display_q, latest: LatestDetections,
                 cap_rate: Rate, inf_rate: Rate) -> None:  # noqa: ANN001
    _pin(cfg.cpu_display, cfg.rt_priority, "display")
    writer = None
    if cfg.record_path:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        fps = src.fps if src.fps > 0 else 30.0
        writer = cv2.VideoWriter(cfg.record_path, fourcc, fps, (src.width, src.height))
        log.info("recording annotated output -> %s", cfg.record_path)
 
    presenter = create_presenter(cfg.headless, cfg.presenter, src.width, src.height, cfg.display_scale)
    vsync = getattr(presenter, "vsync", False)

    ttl_ns = cfg.detection_ttl_ms * 1_000_000
    disp_rate = Rate()
    last_log = time.monotonic()
    last_raw: np.ndarray | None = None

    while not shutdown.is_set():
        # Drain the queue to the newest frame (newest-frame-wins).
        new = None
        try:
            while True:
                new = display_q.get_nowait()
        except queue.Empty:
            pass
        if new is not None:
            last_raw = new

        if last_raw is None:
            time.sleep(0.005)  # nothing captured yet
            continue
        # Only a vsync presenter re-presents a held frame every vblank (to keep
        # cadence). Headless / cv2 advance solely on new frames to avoid busy-loop
        # spinning and duplicate recorded frames.
        if new is None and not vsync:
            time.sleep(0.002)
            continue

        frame = last_raw.copy()
        boxes, ts_ns = latest.get()
        fresh = boxes and (time.perf_counter_ns() - ts_ns) < ttl_ns
        if fresh:
            for b in boxes:
                cv2.rectangle(frame, (int(b.x1), int(b.y1)), (int(b.x2), int(b.y2)), (0, 255, 0), 2)
                cv2.putText(frame, f"{b.score:.2f}", (int(b.x1), max(14, int(b.y1) - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)

        disp_rate.tick()
        hud = (f"disp {disp_rate.fps:4.1f}  cap {cap_rate.fps:4.1f}  "
               f"infer {inf_rate.fps:4.1f} ({inf_rate.latency_ms:4.1f}ms)  "
               f"det {len(boxes) if fresh else 0}")
        cv2.putText(frame, hud, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(frame, hud, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1, cv2.LINE_AA)

        # Record only genuinely new frames so the file stays at capture rate.
        if writer is not None and new is not None:
            writer.write(frame)
        if presenter is not None:
            if not presenter.present(frame):  # GL: blocks until vblank; ESC/close -> False
                shutdown.set()

        now = time.monotonic()
        if now - last_log >= 5.0:
            log.info("FPS display=%.1f capture=%.1f inference=%.1f latency=%.1fms",
                     disp_rate.fps, cap_rate.fps, inf_rate.fps, inf_rate.latency_ms)
            last_log = now

    if writer is not None:
        writer.release()
    if presenter is not None:
        presenter.close()
    src = create_source(cfg)
    log.info("source: %s %dx%d fps=%.1f live=%s", src.name, src.width, src.height, src.fps, src.is_live)
 
    display_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=2)
    infer_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=1)
    latest = LatestDetections()
    cap_rate, inf_rate = Rate(), Rate()
 
    threading.Thread(target=capture_loop, args=(cfg, src, display_q, infer_q, cap_rate),
                     name="capture", daemon=True).start()
    threading.Thread(target=inference_loop, args=(cfg, det, infer_q, latest, inf_rate),
                     name="inference", daemon=True).start()
    try:
        display_loop(cfg, src, display_q, latest, cap_rate, inf_rate)  # main thread (cv2 highgui)
    finally:
        shutdown.set()
        time.sleep(0.2)
        src.close()
    return 0
 
 
if __name__ == "__main__":
    raise SystemExit(main())