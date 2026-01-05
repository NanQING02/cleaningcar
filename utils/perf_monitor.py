import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple


def _fps_from_samples(samples: Deque[Tuple[float, int]]) -> float:
    if len(samples) < 2:
        return 0.0
    start_ts, start_idx = samples[0]
    end_ts, end_idx = samples[-1]
    elapsed = end_ts - start_ts
    if elapsed <= 1e-6:
        return 0.0
    frames = max(0, end_idx - start_idx)
    return frames / elapsed


@dataclass
class PerformanceSnapshot:
    decode_fps: float = 0.0
    output_fps: float = 0.0
    latency_ms: float = 0.0
    latency_avg_ms: float = 0.0
    task_queue: int = 0
    result_queue: int = 0
    pending_results: int = 0
    last_frame_idx: int = 0
    last_capture_ts: float = 0.0
    last_output_ts: float = 0.0
    rtsp_delay_s: float = 0.0


class PerformanceMonitor:
    def __init__(
        self,
        target_fps: float = 25.0,
        metrics_path: Optional[Path] = None,
        window_seconds: float = 10.0,
        flush_interval: float = 1.0,
    ):
        self.target_fps = target_fps or 25.0
        self.metrics_path = Path(metrics_path) if metrics_path else None
        self.window = max(1.0, window_seconds)
        self.flush_interval = max(0.5, flush_interval)
        self._decode_samples: Deque[Tuple[float, int]] = deque()
        self._output_samples: Deque[Tuple[float, int]] = deque()
        self._latency_samples: Deque[Tuple[float, float]] = deque()
        self._lock = threading.Lock()
        self._snapshot = PerformanceSnapshot()
        self._last_flush = 0.0

    def record_decode(self, frame_idx: int, capture_ts: float, task_depth: int, result_depth: int):
        now = time.time()
        with self._lock:
            self._decode_samples.append((now, frame_idx))
            self._trim(self._decode_samples, now)
            self._snapshot.decode_fps = _fps_from_samples(self._decode_samples)
            self._snapshot.task_queue = max(0, int(task_depth))
            self._snapshot.result_queue = max(0, int(result_depth))
            self._snapshot.last_capture_ts = capture_ts
            self._maybe_flush_locked(now)

    def record_output(self, frame_idx: int, capture_ts: Optional[float], output_ts: float, pending_results: int) -> float:
        latency = 0.0
        if capture_ts:
            latency = max(0.0, output_ts - capture_ts)
        now = output_ts
        with self._lock:
            self._output_samples.append((now, frame_idx))
            self._trim(self._output_samples, now)
            self._latency_samples.append((now, latency))
            self._trim(self._latency_samples, now)
            self._snapshot.output_fps = _fps_from_samples(self._output_samples)
            self._snapshot.latency_ms = latency * 1000.0
            self._snapshot.latency_avg_ms = self._avg_latency_locked() * 1000.0
            self._snapshot.rtsp_delay_s = latency
            self._snapshot.pending_results = max(0, int(pending_results))
            self._snapshot.last_frame_idx = frame_idx
            self._snapshot.last_output_ts = output_ts
            self._maybe_flush_locked(now)
        return latency

    def snapshot(self) -> Dict:
        with self._lock:
            return {
                "target_fps": self.target_fps,
                "decode_fps": self._snapshot.decode_fps,
                "output_fps": self._snapshot.output_fps,
                "latency_ms": self._snapshot.latency_ms,
                "latency_avg_ms": self._snapshot.latency_avg_ms,
                "task_queue": self._snapshot.task_queue,
                "result_queue": self._snapshot.result_queue,
                "pending_results": self._snapshot.pending_results,
                "last_frame_idx": self._snapshot.last_frame_idx,
                "last_capture_ts": self._snapshot.last_capture_ts,
                "last_output_ts": self._snapshot.last_output_ts,
                "rtsp_delay_s": self._snapshot.rtsp_delay_s,
                "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            }

    def overlay_lines(self, capture_ts: Optional[float], latency: Optional[float]) -> List[str]:
        snap = self.snapshot()
        lines: List[str] = []
        now_local = datetime.now().strftime("%H:%M:%S")
        lines.append(f"Now {now_local}")
        if capture_ts:
            capture_local = datetime.fromtimestamp(capture_ts).strftime("%H:%M:%S.%f")[:-3]
            lines.append(f"Captured {capture_local}")
        lat_ms = latency * 1000.0 if latency is not None else snap.get("latency_ms")
        avg_ms = snap.get("latency_avg_ms")
        if lat_ms is not None:
            lines.append(f"Latency {lat_ms:.0f} ms (avg {avg_ms:.0f} ms)")
        lines.append(
            f"Decode {snap.get('decode_fps', 0.0):.1f} FPS  Output {snap.get('output_fps', 0.0):.1f} FPS"
        )
        lines.append(
            f"Queues task {snap.get('task_queue', 0)} result {snap.get('result_queue', 0)} pending {snap.get('pending_results', 0)}"
        )
        return lines

    def _avg_latency_locked(self) -> float:
        if not self._latency_samples:
            return 0.0
        total = sum(value for _, value in self._latency_samples)
        return total / len(self._latency_samples)

    def _trim(self, samples: Deque, now: float):
        while samples and now - samples[0][0] > self.window:
            samples.popleft()

    def _maybe_flush_locked(self, now: float):
        if not self.metrics_path:
            return
        if now - self._last_flush < self.flush_interval:
            return
        data = self.snapshot()
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.metrics_path.with_suffix(".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp_path.replace(self.metrics_path)
        except Exception:
            pass
        self._last_flush = now
