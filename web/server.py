import argparse
import csv
import json
import os
import subprocess
import threading
import time
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import cv2
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, PlainTextResponse, FileResponse
from pydantic import BaseModel, Field

from config_manager import ConfigManager, ConfigError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
TEMPLATE_PATH = ROOT / "web" / "templates" / "zone_editor.html"
RUN_SCRIPT = ROOT / "run_zone_detect.py"
EVENT_LOG_PATH = ROOT / "events" / "event_log.csv"


class FlowVector(BaseModel):
    start: List[float] = Field(..., min_length=2, max_length=2)
    end: List[float] = Field(..., min_length=2, max_length=2)


class ZonePayload(BaseModel):
    zone_a_detection: List[List[float]]
    zone_b_wash: List[List[float]]
    flow_vector: FlowVector


class ConfigPayload(BaseModel):
    system: Optional[Dict] = None
    video: Optional[Dict] = None
    logic: Optional[Dict] = None


class ConfigSelectPayload(BaseModel):
    name: str


class ConfigSaveAsPayload(BaseModel):
    name: str
    data: Dict


app = FastAPI(title="CleaningCar Zone Editor")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def _startup_manager():
    try:
        _get_default_inference_manager()
    except Exception as exc:
        print(f"[server] startup: failed to init default inference manager: {exc}")


@app.on_event("shutdown")
def _shutdown_manager():
    for mgr in list(INFERENCE_MANAGERS.values()):
        try:
            mgr.shutdown_manager()
        except Exception as exc:
            print(f"[server] shutdown: failed to shutdown manager: {exc}")


class FrameCache:
    def __init__(self):
        self.data = None
        self.size = (960, 540)

    def clear(self):
        self.data = None


FRAME_CACHE = FrameCache()


class InferenceManager:
    def __init__(self, script_path: Path, config_path: Path):
        self.script_path = Path(script_path)
        self.config_path = Path(config_path)
        self.process: Optional[subprocess.Popen] = None
        self.single_shot = False
        self.lock = threading.Lock()
        self.desired = False
        self.auto_restart = True
        self.restart_count = 0
        self.last_start: Optional[float] = None
        self.last_exit: Optional[Dict[str, float]] = None
        self.log_buffer = deque(maxlen=800)
        self.log_lock = threading.Lock()
        self.shutdown = threading.Event()
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

    def _append_log(self, message: str):
        ts = time.time()
        stamp = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
        line = f'{stamp} {message}'
        print(line)
        with self.log_lock:
            self.log_buffer.append((ts, message))

    def _capture_output(self, proc: subprocess.Popen, log_path: Optional[Path] = None):
        if not proc.stdout:
            return
        f = None
        if log_path is not None:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                f = log_path.open("a", encoding="utf-8")
            except Exception:
                f = None
        try:
            for raw in proc.stdout:
                if not raw:
                    break
                line = raw.rstrip()
                self._append_log(f'[infer] {line}')
                if f is not None:
                    try:
                        f.write(line + "\n")
                    except Exception:
                        pass
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass

    def _launch_locked(self):
        if not self.script_path.exists():
            raise RuntimeError('run_zone_detect.py not found')
        self.single_shot = self._detect_single_shot()
        if self.single_shot:
            self._append_log('[guardian] file source detected，本次推理完成后不会自动重启')
        cmd = [sys.executable, str(self.script_path), "--config", str(self.config_path)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.process = proc
        self.restart_count += 1
        self.last_start = time.time()
        log_dir = ROOT / "logs" / "inference"
        log_name = datetime.fromtimestamp(self.last_start).strftime("infer_%Y%m%d_%H%M%S.log")
        log_path = log_dir / log_name
        threading.Thread(target=self._capture_output, args=(proc, log_path), daemon=True).start()
        self._append_log(f'[guardian] started inference pid={proc.pid} log={log_path}')

    def _terminate_locked(self):
        if not self.process:
            return
        proc = self.process
        self._append_log(f'[guardian] stopping pid={proc.pid}')
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        finally:
            code = proc.poll()
            self.last_exit = {'time': time.time(), 'code': code if code is not None else -1}
            self.process = None

    def start(self):
        with self.lock:
            self.desired = True
            if not self.process or self.process.poll() is not None:
                self._launch_locked()
        return self.status()

    def stop(self):
        with self.lock:
            self.desired = False
            self.auto_restart = False
            self._terminate_locked()
        return self.status()

    def restart(self):
        with self.lock:
            self.desired = True
            self._terminate_locked()
            self._launch_locked()
        return self.status()

    def set_auto_restart(self, enabled: bool):
        with self.lock:
            self.auto_restart = bool(enabled)
        self._append_log(f'[guardian] auto_restart set to {enabled}')

    def status(self):
        with self.lock:
            running = bool(self.process and self.process.poll() is None)
            pid = self.process.pid if running else None
            last_start = self.last_start
            last_exit = self.last_exit
            restart_count = self.restart_count
            auto_restart = self.auto_restart
        status = {
            'running': running,
            'pid': pid,
            'last_start': last_start,
            'last_exit': last_exit,
            'restart_count': restart_count,
            'auto_restart': auto_restart,
            'single_shot': self.single_shot,
        }
        return status

    def logs(self, limit: int = 200):
        limit = max(1, min(1000, int(limit)))
        with self.log_lock:
            items = list(self.log_buffer)[-limit:]
        result = []
        for ts, line in items:
            result.append({'timestamp': datetime.fromtimestamp(ts).isoformat(timespec='seconds'), 'line': line})
        return result

    def _detect_single_shot(self) -> bool:
        try:
            cfg = ConfigManager(self.config_path)
        except ConfigError as exc:
            self._append_log(f'[guardian] config error: {exc}')
            return False
        video_cfg = cfg.video
        source = str(video_cfg.get('source', '')).strip()
        if not source:
            return False
        lowered = source.lower()
        if lowered.startswith(('rtsp://', 'rtmp://', 'rtp://', 'rtsps://', 'http://', 'https://')):
            return False
        mode = str(video_cfg.get('source_mode', '')).lower()
        if mode == 'file':
            return True
        if mode == 'camera':
            return False
        candidate = Path(source).expanduser()
        return candidate.is_file()

    def _monitor_loop(self):
        while not self.shutdown.is_set():
            with self.lock:
                desired = self.desired
                auto_restart = self.auto_restart
            if desired:
                should_launch = False
                with self.lock:
                    if not self.process:
                        should_launch = True
                    else:
                        code = self.process.poll()
                        if code is not None:
                            self._append_log(f'[guardian] process exited with code {code}')
                            self.last_exit = {'time': time.time(), 'code': code}
                            self.process = None
                            if self.single_shot:
                                should_launch = False
                                self.desired = False
                                self._append_log('[guardian] 单次文件源运行完成，等待手动启动')
                                self.single_shot = False
                            else:
                                should_launch = auto_restart
                if should_launch:
                    try:
                        with self.lock:
                            self._launch_locked()
                    except Exception as exc:
                        self._append_log(f'[guardian] failed to start inference: {exc}')
                        time.sleep(5)
            else:
                with self.lock:
                    if self.process:
                        self._terminate_locked()
            time.sleep(1)

    def shutdown_manager(self):
        self.shutdown.set()
        with self.lock:
            self.desired = False
            self.auto_restart = False
            self._terminate_locked()
        self._append_log('[guardian] shutdown complete')

    def set_config_path(self, path: Path):
        with self.lock:
            self.config_path = Path(path)


INFERENCE_MANAGERS: Dict[str, InferenceManager] = {}


def _resolve_config_path_for_key(key: str) -> Path:
    key = str(key or "").strip()
    base = CONFIG_PATH.parent
    if not key:
        candidate = CONFIG_PATH
    else:
        candidate = (base / key).resolve()
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="配置文件不存在")
    if candidate.suffix.lower() != ".json":
        raise HTTPException(status_code=400, detail="仅支持 JSON 配置")
    return candidate


def _get_inference_manager_for_key(key: str) -> InferenceManager:
    cfg_path = _resolve_config_path_for_key(key)
    mgr = INFERENCE_MANAGERS.get(key)
    if mgr is None:
        mgr = InferenceManager(RUN_SCRIPT, cfg_path)
        INFERENCE_MANAGERS[key] = mgr
    else:
        mgr.script_path = RUN_SCRIPT
        mgr.config_path = cfg_path
    return mgr


def _get_default_inference_manager() -> InferenceManager:
    return _get_inference_manager_for_key(CONFIG_PATH.name)


def _resolve_path(path_str: Optional[str], default: Optional[Path] = None) -> Path:
    if path_str:
        candidate = Path(path_str)
    elif default is not None:
        candidate = Path(default)
    else:
        candidate = ROOT
    if not candidate.is_absolute():
        candidate = (ROOT / candidate).resolve()
    return candidate


def _event_log_path(cfg: ConfigManager) -> Path:
    base_dir = cfg.data.get("event_output_dir")
    if not base_dir:
        base_dir = cfg.data.get("system", {}).get("event_output_dir")
    base = _resolve_path(base_dir or (ROOT / "events"))
    return base / "event_log.csv"


def _detection_csv_path(cfg: ConfigManager) -> Optional[Path]:
    csv_path = cfg.video.get("csv")
    if not csv_path:
        return None
    return _resolve_path(csv_path)


def _debug_frame_path(cfg: ConfigManager) -> Optional[Path]:
    dbg_path = cfg.video.get("debug_frame_path")
    if not dbg_path:
        return None
    return _resolve_path(dbg_path)


def _per_id_video_root(cfg: ConfigManager) -> Path:
    logic = cfg.data.get("logic", {}) or {}
    base_dir = logic.get("per_id_video_dir") or cfg.data.get("per_id_video_dir")
    base = _resolve_path(base_dir or (ROOT / "video_result" / "per_id"))
    return base


def _events_root(cfg: ConfigManager) -> Path:
    base_dir = cfg.data.get("event_output_dir")
    if not base_dir:
        base_dir = cfg.data.get("system", {}).get("event_output_dir")
    base = _resolve_path(base_dir or (ROOT / "events"))
    return base


def _read_csv_tail(path: Path, limit: int):
    if not path.exists():
        return []
    rows = deque(maxlen=max(1, limit))
    try:
        with path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception:
        return []
    return list(rows)


def _load_config():
    try:
        return ConfigManager(CONFIG_PATH)
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _inference_log_dir() -> Path:
    return ROOT / "logs" / "inference"


def _capture_frame(force=False):
    if FRAME_CACHE.data is not None and not force:
        return
    cfg = _load_config()
    src = cfg.video.get("source")
    if not src:
        raise HTTPException(status_code=400, detail="video.source 未配置")
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise HTTPException(status_code=500, detail=f"无法打开视频源: {src}")
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        raise HTTPException(status_code=500, detail="视频源无法读取帧")
    success, buf = cv2.imencode(".jpg", frame)
    if not success:
        raise HTTPException(status_code=500, detail="帧编码失败")
    FRAME_CACHE.data = buf.tobytes()
    FRAME_CACHE.size = (frame.shape[1], frame.shape[0])


@app.get("/", response_class=HTMLResponse)
def index():
    if not TEMPLATE_PATH.exists():
        raise HTTPException(status_code=500, detail="Template missing.")
    return TEMPLATE_PATH.read_text(encoding="utf-8")


@app.get("/static/vue.global.prod.js")
def vue_bundle():
    path = ROOT / "web" / "static" / "vue.global.prod.js"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Vue bundle missing.")
    return FileResponse(path)


@app.get("/static/{name}")
def static_file(name: str):
    path = ROOT / "web" / "static" / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Static file missing.")
    return FileResponse(path)


@app.get("/zones")
def read_zones():
    cfg = _load_config()
    return cfg.zones


@app.post("/zones")
def update_zones(payload: ZonePayload):
    cfg = _load_config()
    cfg.data.setdefault("zones", {})
    cfg.data["zones"]["zone_a_detection"] = payload.zone_a_detection
    cfg.data["zones"]["zone_b_wash"] = payload.zone_b_wash
    cfg.data["zones"]["flow_vector"] = {
        "start": payload.flow_vector.start,
        "end": payload.flow_vector.end,
    }
    cfg.save()
    return {"status": "ok"}


@app.get("/frame_meta")
def frame_meta():
    if FRAME_CACHE.data is None:
        try:
            _capture_frame()
        except HTTPException:
            return {"available": False}
    w, h = FRAME_CACHE.size
    return {"available": True, "width": w, "height": h}


@app.get("/frame")
def get_frame():
    if FRAME_CACHE.data is None:
        _capture_frame()
    return Response(content=FRAME_CACHE.data, media_type="image/jpeg")


@app.post("/frame/reload")
def reload_frame():
    _capture_frame(force=True)
    return {"status": "ok"}


@app.get("/debug_frame_meta")
def debug_frame_meta():
    cfg = _load_config()
    path = _debug_frame_path(cfg)
    if not path or not path.exists():
        return {"available": False}
    try:
        stat = path.stat()
        return {"available": True, "path": str(path), "updated": stat.st_mtime}
    except Exception:
        return {"available": True, "path": str(path), "updated": 0}


@app.get("/debug_frame")
def debug_frame():
    cfg = _load_config()
    path = _debug_frame_path(cfg)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="调试帧不存在，请先在配置里启用 debug_frame_path。")
    try:
        data = path.read_bytes()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"无法读取调试帧: {exc}") from exc
    return Response(content=data, media_type="image/jpeg")


@app.get("/videos/per_id")
def list_per_id_videos(limit: int = 200):
    cfg = _load_config()
    root = _per_id_video_root(cfg)
    events_root = _events_root(cfg)
    if not root.exists():
        return {"videos": []}
    try:
        files = sorted(root.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return {"videos": []}
    items = []
    limit = max(1, min(int(limit), 500))
    for path in files[:limit]:
        try:
            stat = path.stat()
        except Exception:
            continue
        rel = str(path.relative_to(root))
        name = path.name
        stem = path.stem
        camera_id = None
        track_id = None
        meta = {}
        parts = stem.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            camera_id = parts[0]
            track_id = int(parts[1])
            key_prefix = f"{camera_id}_{track_id}"
        else:
            dash_parts = stem.rsplit("-", 2)
            if len(dash_parts) == 3 and dash_parts[2].isdigit():
                camera_id = dash_parts[0]
                track_id = int(dash_parts[2])
                key_prefix = f"{camera_id}_{track_id}"
            else:
                key_prefix = None
        if key_prefix:
            try:
                cand = sorted(events_root.glob(f"{key_prefix}_t5_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            except Exception:
                cand = []
            if cand:
                try:
                    with cand[0].open("r", encoding="utf-8") as f:
                        event = json.load(f)
                except Exception:
                    event = {}
                meta = {
                    "plateNumber": event.get("plateNumber") or "",
                    "captureTime": event.get("captureTime") or "",
                    "isAbnormal": bool(event.get("isAbnormal")),
                    "abnormalReason": event.get("abnormalReason") or "",
                    "vehicleType": event.get("vehicleType") or "",
                    "lane": event.get("lane") or "",
                }
        items.append({
            "file": name,
            "relativePath": rel,
            "cameraId": camera_id,
            "trackId": track_id,
            "meta": meta,
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "session": str(path.parent.relative_to(root)) if path.parent != root else "",
        })
    return {"videos": items}


@app.get("/videos/per_id/file")
def get_per_id_video(path: str):
    cfg = _load_config()
    root = _per_id_video_root(cfg)
    target = (root / path).resolve()
    try:
        root_resolved = root.resolve()
    except Exception:
        root_resolved = root
    if not str(target).startswith(str(root_resolved)):
        raise HTTPException(status_code=403, detail="无效路径")
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(str(target), media_type="video/mp4", filename=target.name)


@app.get("/config")
def read_config():
    cfg = _load_config()
    return cfg.data


@app.post("/config/save_as")
def save_config_as(payload: ConfigSaveAsPayload):
    name = str(payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    if any(sep in name for sep in ("/", "\\")):
        raise HTTPException(status_code=400, detail="文件名不能包含路径分隔符")
    if not name.lower().endswith(".json"):
        name = f"{name}.json"
    base = CONFIG_PATH.parent
    target = (base / name).resolve()
    if target.exists():
        raise HTTPException(status_code=409, detail="配置文件已存在")
    try:
        with target.open("w", encoding="utf-8") as f:
            json.dump(payload.data, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存失败: {exc}") from exc
    return {"saved_as": target.name}


@app.post("/config/save_as")
def save_config_as(payload: ConfigSaveAsPayload):
    name = str(payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    if any(sep in name for sep in ("/", "\\")):
        raise HTTPException(status_code=400, detail="文件名不能包含路径分隔符")
    if not name.lower().endswith(".json"):
        name = f"{name}.json"
    base = CONFIG_PATH.parent
    target = (base / name).resolve()
    if target.exists():
        raise HTTPException(status_code=409, detail="配置文件已存在")
    try:
        with target.open("w", encoding="utf-8") as f:
            json.dump(payload.data, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存失败: {exc}") from exc
    return {"saved_as": target.name}


def _available_config_files() -> List[Path]:
    base = CONFIG_PATH.parent
    files = list(base.glob("config*.json"))
    return sorted(files)


@app.get("/config/files")
def list_config_files():
    files = _available_config_files()
    return {
        "active": CONFIG_PATH.name,
        "files": [p.name for p in files],
    }


@app.post("/config/select")
def select_config(payload: ConfigSelectPayload):
    global CONFIG_PATH
    base = CONFIG_PATH.parent
    candidate = (base / payload.name).resolve()
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="配置文件不存在")
    if candidate.suffix.lower() != ".json":
        raise HTTPException(status_code=400, detail="仅支持 JSON 配置")
    CONFIG_PATH = candidate
    mgr = _get_default_inference_manager()
    mgr.set_config_path(candidate)
    FRAME_CACHE.clear()
    return {"active": candidate.name}

@app.post("/config")
def update_config(payload: ConfigPayload):
    cfg = _load_config()
    if payload.system:
        cfg.data.setdefault("system", {}).update(payload.system)
    if payload.video:
        cfg.data.setdefault("video", {}).update(payload.video)
        FRAME_CACHE.clear()
    if payload.logic:
        cfg.data.setdefault("logic", {}).update(payload.logic)
    cfg.save()
    return {"status": "ok"}


@app.post("/inference/start")
def start_inference(key: Optional[str] = None):
    mgr = _get_inference_manager_for_key(key or CONFIG_PATH.name)
    return mgr.start()


@app.post("/inference/stop")
def stop_inference(key: Optional[str] = None):
    mgr = _get_inference_manager_for_key(key or CONFIG_PATH.name)
    return mgr.stop()


@app.post("/inference/restart")
def restart_inference(key: Optional[str] = None):
    mgr = _get_inference_manager_for_key(key or CONFIG_PATH.name)
    return mgr.restart()


@app.post("/inference/auto_restart")
def set_auto_restart(enable: bool = True, key: Optional[str] = None):
    mgr = _get_inference_manager_for_key(key or CONFIG_PATH.name)
    mgr.set_auto_restart(enable)
    return {"auto_restart": enable}


@app.get("/inference/status")
def inference_status(key: Optional[str] = None):
    mgr = _get_inference_manager_for_key(key or CONFIG_PATH.name)
    return mgr.status()


@app.get("/logs/inference")
def inference_logs(lines: int = 200, key: Optional[str] = None):
    mgr = _get_inference_manager_for_key(key or CONFIG_PATH.name)
    return {"lines": mgr.logs(lines)}


@app.get("/logs/events")
def get_event_logs(lines: int = 50):
    cfg = _load_config()
    path = _event_log_path(cfg)
    if not path.exists():
        return {"available": False, "path": str(path)}
    rows = _read_csv_tail(path, lines)
    return {"available": True, "path": str(path), "rows": rows}


@app.get("/logs/detections")
def get_detection_logs(lines: int = 50):
    cfg = _load_config()
    csv_path = _detection_csv_path(cfg)
    if not csv_path or not csv_path.exists():
        return {"available": False, "path": str(csv_path) if csv_path else ""}
    rows = _read_csv_tail(csv_path, lines)
    return {"available": True, "path": str(csv_path), "rows": rows}


@app.get("/logs/files")
def list_inference_log_files():
    base = _inference_log_dir()
    if not base.exists():
        return {"files": []}
    items = []
    for p in sorted(base.glob("*.log")):
        try:
            st = p.stat()
        except OSError:
            continue
        items.append(
            {
                "name": p.name,
                "size": st.st_size,
                "mtime": st.st_mtime,
            }
        )
    return {"files": items}


@app.get("/logs/file/{name}")
def read_inference_log_file(name: str, lines: int = 400):
    if any(sep in name for sep in ("/", "\\")):
        raise HTTPException(status_code=400, detail="文件名非法")
    base = _inference_log_dir()
    path = (base / name).resolve()
    try:
        base_resolved = base.resolve()
    except Exception:
        base_resolved = base
    if not str(path).startswith(str(base_resolved)):
        raise HTTPException(status_code=400, detail="路径越界")
    if not path.exists():
        raise HTTPException(status_code=404, detail="日志文件不存在")
    max_lines = max(1, min(2000, int(lines)))
    buf = deque(maxlen=max_lines)
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                buf.append(line.rstrip("\n"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取失败: {exc}") from exc
    content = "\n".join(buf)
    return PlainTextResponse(content)


@app.get("/logs", response_class=HTMLResponse)
def logs_page():
    html = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>CleaningCar 日志监控</title>
  <style>
    body { font-family: "Segoe UI", "PingFang SC", sans-serif; margin: 0; background: #111; color: #eee; }
    header { padding: 12px 20px; background: #191d23; border-bottom: 1px solid #242931; }
    main { padding: 16px 20px 40px; display: grid; grid-template-columns: 260px 1fr; gap: 16px; }
    h1 { margin: 0; font-size: 18px; }
    .sidebar { background: #1c2129; border: 1px solid #2a313d; border-radius: 8px; padding: 10px; }
    .content { background: #0f1115; border: 1px solid #2a313d; border-radius: 8px; padding: 10px; }
    ul { list-style: none; padding: 0; margin: 0; max-height: 70vh; overflow-y: auto; }
    li { padding: 6px 8px; cursor: pointer; border-radius: 4px; font-size: 13px; }
    li:hover { background: #2a313d; }
    li.active { background: #345; }
    .meta { font-size: 11px; color: #9aa3b5; }
    pre { white-space: pre-wrap; word-break: break-all; font-size: 12px; line-height: 1.4; }
    .toolbar { margin-bottom: 8px; font-size: 13px; display: flex; gap: 8px; align-items: center; }
    input { background: #000; border-radius: 4px; border: 1px solid #2a313d; color: #eee; padding: 4px 6px; width: 80px; }
    button { border: none; border-radius: 4px; padding: 4px 10px; background: #2f7cf8; color: #fff; cursor: pointer; font-size: 13px; }
    button.secondary { background: #3b3f45; }
    a { color: #8ab4ff; text-decoration: none; }
  </style>
</head>
<body>
  <header>
    <h1>CleaningCar 日志监控</h1>
  </header>
  <main>
    <section class="sidebar">
      <div class="toolbar">
        <span>推理日志文件</span>
        <button class="secondary" onclick="loadFiles()">刷新</button>
      </div>
      <ul id="file-list"></ul>
    </section>
    <section class="content">
      <div class="toolbar">
        <span id="current-file">未选择文件</span>
        <span style="flex:1"></span>
        <label>尾部行数 <input id="line-count" type="number" min="50" max="2000" value="400"></label>
        <button onclick="reloadContent()">刷新内容</button>
        <a href="/" style="margin-left:8px;">返回配置控制台</a>
      </div>
      <pre id="log-content">选择左侧日志文件查看内容。</pre>
    </section>
  </main>
  <script>
    let current = null;
    async function loadFiles() {
      const ul = document.getElementById('file-list');
      ul.innerHTML = '<li>加载中…</li>';
      try {
        const res = await fetch('/logs/files');
        if (!res.ok) throw new Error('请求失败');
        const data = await res.json();
        const files = data.files || [];
        if (!files.length) {
          ul.innerHTML = '<li>暂无日志文件</li>';
          return;
        }
        ul.innerHTML = '';
        files.sort((a, b) => b.mtime - a.mtime);
        for (const f of files) {
          const li = document.createElement('li');
          li.textContent = f.name;
          li.onclick = () => selectFile(f.name, li);
          const meta = document.createElement('div');
          meta.className = 'meta';
          const date = new Date(f.mtime * 1000);
          meta.textContent = date.toLocaleString() + ' · ' + f.size + ' bytes';
          li.appendChild(meta);
          ul.appendChild(li);
        }
      } catch (err) {
        ul.innerHTML = '<li>加载失败: ' + err.message + '</li>';
      }
    }
    async function selectFile(name, li) {
      current = name;
      document.getElementById('current-file').textContent = name;
      const items = document.querySelectorAll('#file-list li');
      items.forEach(x => x.classList.remove('active'));
      li.classList.add('active');
      await reloadContent();
    }
    async function reloadContent() {
      const pre = document.getElementById('log-content');
      if (!current) {
        pre.textContent = '请选择日志文件。';
        return;
      }
      const n = document.getElementById('line-count').value || '400';
      pre.textContent = '加载中…';
      try {
        const res = await fetch('/logs/file/' + encodeURIComponent(current) + '?lines=' + encodeURIComponent(n));
        if (!res.ok) throw new Error('请求失败');
        const text = await res.text();
        pre.textContent = text || '(空文件)';
      } catch (err) {
        pre.textContent = '读取失败: ' + err.message;
      }
    }
    loadFiles();
  </script>
</body>
</html>
    """
    return HTMLResponse(html)


def parse_args():
    parser = argparse.ArgumentParser(description="CleaningCar FastAPI server")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="config.json path")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="enable uvicorn reload")
    return parser.parse_args()


def main():
    import uvicorn

    args = parse_args()
    global CONFIG_PATH
    CONFIG_PATH = Path(args.config).resolve()
    uvicorn.run(
        "web.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
