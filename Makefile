# Updated Endoscopy Demo — standalone build/run.
#
# Independent of the rest of Surgical_Instrument. Just needs an OpenVINO IR and
# (for file mode) a video, mounted from the host.
#
#   make build
#   make run-file      VIDEO=/abs/path/polyp_test.mp4
#   make run-camera    SERIAL=40067928
#   make run-webcam    DEVICE_INDEX=0
#   make bench         VIDEO=/abs/path/polyp_test.mp4      # headless FPS
#   make record        VIDEO=/abs/path/polyp_test.mp4
#   make shell
#
SHELL := /bin/bash

IMAGE        ?= endoscopy-demo:latest
CONTAINER    ?= endoscopy-demo

# --- Host paths mounted into the container -----------------------------------
# MODELS_DIR must contain: yolo11n_polyp/best_openvino_model/best.xml
MODELS_DIR   ?= $(abspath ../models)
VIDEOS_DIR   ?= $(abspath ../videos)
VIDEO        ?= /videos/polyp_test.mp4      # container-side path
MODEL        ?= /models/yolo11n_polyp/best_openvino_model/best.xml

# --- Runtime knobs -----------------------------------------------------------
DEVICE       ?= GPU            # CPU | GPU | NPU
FRAME_SKIP   ?= 1
THRESHOLD    ?= 0.5
SERIAL       ?=                # Basler serial ("" = first camera)
DEVICE_INDEX ?= 0              # V4L2 webcam index
EXTRA        ?=                # extra flags, e.g. EXTRA="--frame-skip 2"

# Proxy passthrough for build (optional)
HTTP_PROXY   ?=
HTTPS_PROXY  ?=
NO_PROXY     ?=

# --- Host GPU/USB plumbing (auto-detected) -----------------------------------
RENDER_GID   := $(shell getent group render | cut -d: -f3)
VIDEO_GID    := $(shell getent group video  | cut -d: -f3)
GROUP_ARGS   := $(if $(RENDER_GID),--group-add $(RENDER_GID)) $(if $(VIDEO_GID),--group-add $(VIDEO_GID))

DRI          := $(if $(wildcard /dev/dri),--device /dev/dri:/dev/dri)
X11          := -e DISPLAY=$(DISPLAY) -e XDG_RUNTIME_DIR=$(XDG_RUNTIME_DIR) -v /tmp/.X11-unix:/tmp/.X11-unix:rw
USB          := -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw'

MOUNTS       := -v $(MODELS_DIR):/models:ro -v $(VIDEOS_DIR):/videos:rw
# CAP_SYS_NICE + rtprio ulimit: required for --cpu-* affinity and --rt-priority
# (SCHED_FIFO) pinning; without them the app logs "Operation not permitted".
COMMON       := --rm --name $(CONTAINER) --net=host $(DRI) $(GROUP_ARGS) $(MOUNTS) \
                --cap-add SYS_NICE --ulimit rtprio=99 \
                -e MODEL=$(MODEL) -e DEVICE=$(DEVICE)

.PHONY: build
build: ## Build the standalone image
	docker build \
		--build-arg HTTP_PROXY=$(HTTP_PROXY) \
		--build-arg HTTPS_PROXY=$(HTTPS_PROXY) \
		--build-arg NO_PROXY=$(NO_PROXY) \
		-t $(IMAGE) .

.PHONY: _xhost
_xhost:
	@xhost +local:root >/dev/null 2>&1 || true

.PHONY: run-file
run-file: _xhost ## Run on a video file (loops), with display
	docker run $(COMMON) $(X11) $(IMAGE) \
		--source file --source-arg $(VIDEO) \
		--threshold $(THRESHOLD) --frame-skip $(FRAME_SKIP) $(EXTRA)

.PHONY: run-camera
run-camera: _xhost ## Run on a Basler live camera, with display
	docker run $(COMMON) $(X11) $(USB) $(IMAGE) \
		--source basler --source-arg "$(SERIAL)" \
		--threshold $(THRESHOLD) --frame-skip $(FRAME_SKIP) $(EXTRA)

.PHONY: run-webcam
run-webcam: _xhost ## Run on a USB/V4L2 webcam, with display
	docker run $(COMMON) $(X11) $(USB) $(IMAGE) \
		--source v4l2 --source-arg $(DEVICE_INDEX) \
		--threshold $(THRESHOLD) --frame-skip $(FRAME_SKIP) $(EXTRA)

.PHONY: bench
bench: ## Headless FPS benchmark on a video file (no display)
	docker run $(COMMON) $(IMAGE) \
		--source file --source-arg $(VIDEO) --headless \
		--frame-skip $(FRAME_SKIP) $(EXTRA)

.PHONY: record
record: ## Write an annotated clip to $(VIDEOS_DIR)/annotated.mp4
	docker run $(COMMON) $(IMAGE) \
		--source file --source-arg $(VIDEO) --headless \
		--record /videos/annotated.mp4 $(EXTRA)

.PHONY: list-cameras
list-cameras: ## List connected Basler cameras (serial + model) -> SERIAL=
	@python3 utility.py 2>/dev/null || \
	docker run --rm $(USB) -v $(CURDIR)/utility.py:/tmp/utility.py:ro \
		--entrypoint python3 $(IMAGE) /tmp/utility.py

.PHONY: show-cores
show-cores: ## Show P-core / E-core CPU sets for --cpu-* pinning
	@ALL=$$(cat /sys/devices/system/cpu/present 2>/dev/null || echo 'unknown'); \
	PCORE=$$(cat /sys/devices/cpu_core/cpus 2>/dev/null); \
	ECORE=$$(cat /sys/devices/cpu_atom/cpus 2>/dev/null); \
	N=$$(nproc 2>/dev/null || echo '?'); \
	printf '\n[cores] all CPUs        : %s  (nproc=%s)\n' "$$ALL" "$$N"; \
	if [ -n "$$PCORE" ]; then \
	  printf '[cores] P-cores (perf)  : %s  <-- pin --cpu-capture/--cpu-inference/--cpu-display here\n' "$$PCORE"; \
	  printf '[cores] E-cores (effic) : %s\n' "$$ECORE"; \
	  printf '[cores] hint: EXTRA="--cpu-capture 1 --cpu-inference 2 --cpu-display 3 --rt-priority 20"\n'; \
	else \
	  printf '[cores] no P/E core split detected (non-hybrid CPU or older kernel)\n'; \
	  printf '[cores] all cores are equivalent; pick any distinct --cpu-* indices.\n'; \
	fi; \
	printf '\n[cores] lscpu sample (top 6 rows):\n'; \
	lscpu -e 2>/dev/null | head -7 || lscpu | head -10; \
	printf '\n'

.PHONY: shell
shell: ## Interactive shell in the image
	docker run $(COMMON) $(X11) $(USB) --entrypoint bash -it $(IMAGE)

.PHONY: stop
stop: ## Stop a running container
	-docker rm -f $(CONTAINER) 2>/dev/null || true
	@xhost -local:root >/dev/null 2>&1 || true

.PHONY: clean
clean: stop ## Remove the image
	-docker rmi $(IMAGE) 2>/dev/null || true

.PHONY: help
help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
