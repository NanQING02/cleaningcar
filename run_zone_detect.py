import argparse
import base64
import csv
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from datetime import datetime, timedelta
from math import hypot
from pathlib import Path
from queue import Queue

from config_manager import ConfigManager, ConfigError
from zone_manager import ZoneManager, polygon_mask, point_in_polygon
from utils.disk_manager import DiskCleaner
from utils.perf_monitor import PerformanceMonitor
from utils.upload_queue import SQLiteUploadQueue

import cv2
import numpy as np
from rknnlite.api import RKNNLite

RGA_RESIZE_FUNC = None

try:
    import yaml
except ImportError:
    yaml = None

try:
    import psutil  # optional, for monitoring
except ImportError:
    psutil = None


REG_MAX = 16
PROJECT = np.arange(REG_MAX, dtype=np.float32)
STRIDES = [8, 16, 32]
LICENSE_CLASS = 8
PLATE_SIZE = (94, 24)
PLATE_EXPAND_DEFAULT = 0.25
CLASS_NAMES = [
    'car',
    'blue truck',
    'yellow truck',
    'dump truck',
    'wuxiao',
    'wheel',
    'cleaning table',
    'manual',
    'license',
]
CLASS_COLORS = {
    'vehicle': (0, 220, 0),
    'plate': (0, 255, 255),
    'wheel': (255, 255, 0),
    'water': (0, 160, 255),
}
VEHICLE_LABEL_CN = {
    'car': '小汽车',
    'blue truck': '蓝色卡车',
    'yellow truck': '黄色卡车',
    'dump truck': '渣土车',
    'wuxiao': '五小工程车',
}
CLEANING_LABEL_CN = {
    'cleaning table': '清洗台清洗',
    'manual': '人工清洗',
}
CLASS_NAME_TO_ID = {name: idx for idx, name in enumerate(CLASS_NAMES)}
CLASS_ALIAS_TO_ID = {name.lower(): idx for idx, name in enumerate(CLASS_NAMES)}
VEHICLE_CLASS_IDS = {0, 1, 2, 3, 4}
WATER_CLASS_IDS = {6, 7}
CLASS_THRESH = {
    0: 0.40,
    1: 0.40,
    2: 0.40,
    3: 0.40,
    4: 0.40,
    5: 0.50,
    6: 0.50,
    7: 0.50,
    8: 0.50,
}
PLATE_CAR_LINK_IOU = 0.02
CAR_PLATE_CACHE_TTL = 60
REPORT_MIN_FRAMES = 3
DIRECTION_MAP = {
    (1, 1): (5, '正向前出'),
    (1, -1): (6, '正向后出'),
    (-1, 1): (7, '反向前出'),
    (-1, -1): (8, '反向后出'),
}
LPR_CHARS = ['京', '沪', '津', '渝', '冀', '晋', '蒙', '辽', '吉', '黑',
             '苏', '浙', '皖', '闽', '赣', '鲁', '豫', '鄂', '湘', '粤',
             '桂', '琼', '川', '贵', '云', '藏', '陕', '甘', '青', '宁',
             '新',
             '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
             'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'J', 'K',
             'L', 'M', 'N', 'P', 'Q', 'R', 'S', 'T', 'U', 'V',
             'W', 'X', 'Y', 'Z', 'I', 'O', '-']
LPR_BLANK = len(LPR_CHARS) - 1
CLAHE = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(2, 2))
PROVINCE_CHARS = ''.join(['京','沪','津','渝','冀','晋','蒙','辽','吉','黑','苏','浙','皖','闽','赣','鲁','豫','鄂','湘','粤','桂','琼','川','贵','云','藏','陕','甘','青','宁','新'])
PLATE_REGEX = re.compile(rf'^[{PROVINCE_CHARS}][A-Z][A-Z0-9]{{5}}$')
PLATE_REGEX_NE = re.compile(rf'^[{PROVINCE_CHARS}][A-Z][A-Z0-9]{{6}}$')
ALNUM = set('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ')
FFMPEG_PIX_BYTES = {
    'bgr24': 3,
    'rgb24': 3,
}
ENABLE_PER_ID_LEGACY_WRITER = False
ENABLE_DISK_CLEANER = False


def select_box_color(label_name):
    if label_name in VEHICLE_LABEL_CN:
        return CLASS_COLORS['vehicle']
    if label_name == 'license':
        return CLASS_COLORS['plate']
    if label_name == 'wheel':
        return CLASS_COLORS['wheel']
    if label_name in ('cleaning table', 'manual'):
        return CLASS_COLORS['water']
    return (0, 255, 0)


def localize_vehicle(label):
    return VEHICLE_LABEL_CN.get(label, label)


def localize_cleaning(label):
    return CLEANING_LABEL_CN.get(label, label)


def load_config(path):
    cfg_path = Path(path or 'config.json')
    try:
        mgr = ConfigManager(cfg_path)
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc
    system = mgr.system
    video = mgr.video
    logic = mgr.logic
    zones = mgr.zones
    storage = mgr.storage
    shadow_cfg = logic.get('shadow_plate_pool', {})
    event_capture_dir = mgr.data.get('event_capture_dir', './captures')
    event_output_dir = mgr.data.get('event_output_dir', './events')
    merged = {
        'camera_id': system.get('device_id', 'RK3588'),
        'event_capture_dir': str(Path(event_capture_dir)),
        'event_output_dir': str(Path(event_output_dir)),
        'api_url': system.get('api', {}).get('url', ''),
        'api_token': system.get('api', {}).get('token', ''),
        'capture_mode': system.get('api', {}).get('capture_mode', 'path'),
        'monitor_interval': float(system.get('monitor_interval', 2.0)),
        'video': video,
        'logic': logic,
        'zones': zones,
        'storage': storage,
        'shadow_pool': shadow_cfg,
        'allowed_event_types': logic.get('allowed_event_types', [1, 2, 3, 4, 5]),
        'track_timeout_frames': int(logic.get('track_timeout_frames', 90)),
        'track_max_age': int(logic.get('track_max_age', 60)),
        'lane_name': logic.get('lane_name', '冲洗'),
        'stationary_speed_thresh': float(logic.get('stationary_speed_thresh', 8.0)),
        'vehicle_shrink_ratio': float(logic.get('vehicle_shrink_ratio', 0.35)),
        'vehicle_lock_min_votes': int(logic.get('vehicle_lock_min_votes', 80)),
        'vehicle_lock_on_confirm': bool(logic.get('vehicle_lock_on_confirm', True)),
        'default_plate_color': logic.get('default_plate_color', ''),
        'default_plate_color_conf': float(logic.get('default_plate_color_conf', 0.0)),
        'default_cleanliness': int(logic.get('default_cleanliness', 0)),
        'stationary_min_frames': int(logic.get('stationary_min_frames', 0)),
        'type34_min_interval_frames': int(logic.get('type34_min_interval_frames', 5)),
        'car_plate_cache_ttl': int(logic.get('car_plate_cache_ttl', 60)),
    }
    return merged


def apply_cli_overrides(args, config):
    defaults = getattr(args, '_defaults', None)

    def maybe_set(attr, value):
        if value is None:
            return
        if defaults is None:
            setattr(args, attr, value)
            return
        if getattr(args, attr) == getattr(defaults, attr):
            setattr(args, attr, value)

    video_cfg = (config or {}).get('video', {})
    maybe_set('video', video_cfg.get('source'))
    maybe_set('source_mode', video_cfg.get('source_mode'))
    maybe_set('hw_decode', video_cfg.get('hw_decode'))
    maybe_set('workers', video_cfg.get('workers'))
    maybe_set('core_mask', video_cfg.get('core_mask'))
    maybe_set('save_video', video_cfg.get('save_video'))
    maybe_set('csv', video_cfg.get('csv'))
    cfg = config or {}
    sys_cfg = cfg.get('system', {})
    monitor_interval = cfg.get('monitor_interval', sys_cfg.get('monitor_interval'))
    maybe_set('monitor_interval', monitor_interval)
    maybe_set('api_url', cfg.get('api_url'))
    maybe_set('api_token', cfg.get('api_token'))
    maybe_set('capture_mode', cfg.get('capture_mode'))
    logic = (config or {}).get('logic', {})
    maybe_set('plate_lock_frames', logic.get('plate_lock_frames'))
    maybe_set('no_draw', logic.get('no_draw'))
    maybe_set('is_detour', logic.get('is_detour'))
    maybe_set('lpr_core_mask', logic.get('lpr_core_mask'))


def apply_class_thresholds_from_config(config):
    custom = (config or {}).get('class_thresholds')
    if not custom:
        custom = (config or {}).get('logic', {}).get('class_thresholds')
    if not custom:
        return
    for key, value in custom.items():
        try:
            thresh = float(value)
        except (TypeError, ValueError):
            continue
        idx = None
        if isinstance(key, int):
            idx = key
        else:
            key_str = str(key).strip().lower()
            if key_str in CLASS_ALIAS_TO_ID:
                idx = CLASS_ALIAS_TO_ID[key_str]
            else:
                try:
                    idx = int(key_str)
                except ValueError:
                    idx = None
        if idx is None:
            continue
        if idx < 0 or idx >= len(CLASS_NAMES):
            continue
        CLASS_THRESH[idx] = thresh


class FfmpegH264Writer:
    def __init__(self, path, width, height, fps):
        self.path = str(path)
        p = Path(self.path)
        if p.suffix:
            temp_name = p.stem + '_temp' + p.suffix
        else:
            temp_name = p.name + '_temp'
        self._output_path = str(p.with_name(temp_name))
        self.width = int(width)
        self.height = int(height)
        self.fps = float(fps)
        self.proc = None
        self.stdin = None
        self.encoder = None
        self._opened = False
        self._frames_total = 0
        self._frames_since_log = 0
        self._start_time = time.time()
        self._last_log_time = self._start_time
        self._log_interval = 10.0
        self._start()

    def _build_cmd(self, encoder):
        base = [
            'ffmpeg',
            '-y',
            '-f',
            'rawvideo',
            '-pix_fmt',
            'bgr24',
            '-s',
            f'{self.width}x{self.height}',
            '-r',
            f'{self.fps}',
            '-i',
            '-',
            '-an',
        ]
        if encoder in ('h264_rkmpp', 'h264_v4l2m2m', 'h264_omx'):
            opts = [
                '-c:v',
                encoder,
                '-pix_fmt',
                'yuv420p',
            ]
        else:
            opts = [
                '-c:v',
                'libx264',
                '-preset',
                'ultrafast',
                '-tune',
                'zerolatency',
                '-crf',
                '28',
                '-threads',
                '2',
                '-pix_fmt',
                'yuv420p',
            ]
        tail = [
            '-movflags',
            '+faststart',
            self._output_path,
        ]
        return base + opts + tail

    def _try_start(self, encoder):
        cmd = self._build_cmd(encoder)
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.stdin = self.proc.stdin
            self.encoder = encoder
            time.sleep(0.2)
            if self.proc.poll() is not None:
                self.proc = None
                self.stdin = None
                self.encoder = None
                self._opened = False
                print(f'[per-id-video] encoder {encoder} exited immediately for {self.path}, falling back')
                return False
            self._opened = True
            print(f'[per-id-video] using ffmpeg encoder={encoder} path={self.path}')
            return True
        except Exception as exc:
            self.proc = None
            self.stdin = None
            self.encoder = None
            self._opened = False
            print(f'[per-id-video] failed to start ffmpeg encoder {encoder} for {self.path}: {exc}')
            return False

    def _start(self):
        for enc in ('libx264', 'h264_rkmpp', 'h264_v4l2m2m', 'h264_omx'):
            if self._try_start(enc):
                return
        print(f'[per-id-video] no available H.264 encoder for {self.path}')

    def is_opened(self):
        if not self._opened or not self.proc or not self.stdin:
            return False
        if self.proc.poll() is not None:
            return False
        return True

    def write(self, frame):
        if not self.is_opened():
            return
        if frame is None:
            return
        try:
            self.stdin.write(frame.tobytes())
            self._frames_total += 1
            self._frames_since_log += 1
            now = time.time()
            if self._log_interval > 0 and now - self._last_log_time >= self._log_interval:
                elapsed = now - self._last_log_time
                fps = self._frames_since_log / max(elapsed, 1e-6)
                print(f'[per-id-video] encoder={self.encoder} fps={fps:.2f} window={elapsed:.1f}s total_frames={self._frames_total} path={self.path}')
                self._frames_since_log = 0
                self._last_log_time = now
        except Exception as exc:
            print(f'[per-id-video] write failed for {self.path}: {exc}')
            self.release()

    def release(self):
        finalized = False
        if self.stdin:
            try:
                self.stdin.close()
            except Exception:
                pass
            self.stdin = None
        exit_code = None
        if self.proc:
            try:
                self.proc.wait(timeout=60.0)
                exit_code = self.proc.returncode
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        self._opened = False
        if getattr(self, '_output_path', None) and self.path:
            try:
                if os.path.exists(self._output_path):
                    if exit_code is None or exit_code != 0:
                        print(f'[per-id-video] ffmpeg exit code {exit_code} for {self._output_path}, not renaming')
                    else:
                        os.replace(self._output_path, self.path)
                        print(f'[per-id-video] finalized video: {self.path}')
                        finalized = True
            except Exception as exc:
                print(f'[per-id-video] rename failed {self._output_path} -> {self.path}: {exc}')
        return finalized
def parse_args():
    ap = argparse.ArgumentParser(description='Multithread RKNN detector demo.')
    ap.add_argument('--model', default='best.rknn')
    ap.add_argument('--video', help='Single video file to process.')
    ap.add_argument('--video_dir', help='Directory of videos to process sequentially.')
    ap.add_argument('--imgsz', type=int, default=640)
    ap.add_argument('--conf', type=float, default=0.30)
    ap.add_argument('--iou', type=float, default=0.45)
    ap.add_argument('--max_det', type=int, default=300)
    ap.add_argument('--workers', type=int, default=2, help='Number of inference workers.')
    ap.add_argument('--queue_size', type=int, default=32)
    ap.add_argument('--core_mask', default='all', help="Which NPU cores to use: e.g. '0-2', '0,2', '1', 'all', 'auto'.")
    ap.add_argument('--hw_decode', action='store_true', help='Use GStreamer + mpp hardware decode when available.')
    ap.add_argument('--save_video', help='Output annotated video path.')
    ap.add_argument('--csv', help='CSV path, append per detection.')
    ap.add_argument('--output_dir', help='When batch processing, auto-save mp4/csv into this directory using video stem names.')
    ap.add_argument('--no_draw', action='store_true', help='Do not draw boxes on frames.')
    ap.add_argument('--monitor_interval', type=float, default=0.0, help='Seconds between resource logs (0 disables).')
    ap.add_argument('--limit', type=int, default=0, help='Optional frame limit for quick tests.')
    ap.add_argument('--lpr_model', default='lprnet.rknn', help='Path to license plate recognition RKNN.')
    ap.add_argument('--plate_expand', type=float, default=PLATE_EXPAND_DEFAULT, help='Extra ratio padding for plate crops.')
    ap.add_argument('--plate_lock_frames', type=int, default=5, help='Frames required before plate text is locked.')
    ap.add_argument('--config', help='YAML config describing ROI/event logic.')
    ap.add_argument('--camera', help='当配置包含多个 camera 条目时，指定要运行的 key。')
    ap.add_argument('--debug_rois', action='store_true', help='Visualize stage lines on output frames.')
    ap.add_argument('--debug_tracks', action='store_true', help='Overlay per-track state info on frames.')
    ap.add_argument('--source_mode', choices=['auto', 'camera', 'file'], default='auto',
                    help='数据源类型：camera 为实时流（可自动重连），file 为本地视频（读到末尾即停止）。')
    ap.add_argument('--event_log', nargs='?', const='auto',
                    help='Optional path for事件日志; 不加参数时使用默认 events/event_log.csv。')
    ap.add_argument('--roi_setup', action='store_true', help='Launch ROI editor before running detection.')
    ap.add_argument('--api_url', help='远端车辆冲洗事件上报接口 URL (POST)。')
    ap.add_argument('--api_token', help='用于 HTTP Authorization: Bearer 的 Token。')
    ap.add_argument('--capture_mode', choices=['path', 'base64'], default='path',
                    help='事件截图在上报时的字段格式：文件路径或Base64。')
    ap.add_argument('--lane', help='覆盖事件上报中的 lane 字段。')
    ap.add_argument('--detect_roi_only', action='store_true', help='仅在配置的 detect_roi 多边形内进行检测。')
    ap.add_argument('--lpr_core_mask', default=None, help="LPRNet NPU核心掩码 (e.g. '4' for Core 2)")
    ap.add_argument('--is_detour', action='store_true', help='标记为绕行道模式，屏蔽冲洗逻辑')
    defaults = ap.parse_args(args=[])
    args = ap.parse_args()
    setattr(args, '_defaults', defaults)
    return args


def parse_core_mask(text: str):
    if text is None:
        return None
    s = str(text).strip().lower()
    if s in ('', 'auto', 'default'):
        return None
    if s == 'all':
        return 0b111
    bits = 0
    for part in s.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            try:
                a = int(a); b = int(b)
            except ValueError:
                continue
            for k in range(min(a, b), max(a, b) + 1):
                if 0 <= k <= 2:
                    bits |= (1 << k)
        else:
            try:
                k = int(part, 0)
            except ValueError:
                continue
            if 0 <= k <= 2:
                bits |= (1 << k)
    return bits or None


def open_video_capture(src, hw_decode=False):
    if hw_decode and isinstance(src, str):
        pipelines = []
        if src.startswith(('rtsp://', 'rtsps://')):
            pipelines.append((
                f"rtspsrc location=\"{src}\" latency=200 protocols=tcp ! "
                "rtph264depay ! h264parse ! mppvideodec ! videoconvert ! "
                "video/x-raw,format=BGR ! appsink sync=false drop=true",
                '[reader] Using GStreamer+mpp RTSP TCP pipeline for {src}',
            ))
        elif not src.startswith(('http://', 'https://')):
            pipelines.append((
                f"filesrc location=\"{src}\" ! qtdemux ! h264parse ! mppvideodec ! "
                "videoconvert ! video/x-raw,format=BGR ! appsink",
                '[reader] Using GStreamer+mpp file pipeline for {src}',
            ))
        for pipeline, msg in pipelines:
            cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
            if cap.isOpened():
                print(msg.format(src=src))
                return cap
        print(f'[reader] 硬解模式下 GStreamer+mpp 解码管道创建失败，源={src}，不回退软解，请检查 mpp 插件和视频源配置')
        return None
    if isinstance(src, str) and src.startswith(('rtsp://', 'rtsps://')):
        if 'OPENCV_FFMPEG_CAPTURE_OPTIONS' not in os.environ:
            os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = 'rtsp_transport;tcp'
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        if cap.isOpened():
            print(f'[reader] Using OpenCV FFmpeg RTSP TCP capture for {src}')
            return cap
    return cv2.VideoCapture(src)


def create_video_reader(path, args):
    """Wrapper so主流程统一调用，便于未来扩展到其他解码方式。"""
    hw = bool(getattr(args, 'hw_decode', False))
    cap = open_video_capture(path, hw_decode=hw)
    if not cap or not hasattr(cap, 'isOpened'):
        if hw:
            print(f'create_video_reader: 未能创建 GStreamer+mpp 硬解捕获对象，源={path}')
        else:
            print(f'create_video_reader: OpenCV 捕获对象创建失败，源={path}')
        return None
    if not cap.isOpened():
        if hw:
            print(f'create_video_reader: GStreamer+mpp 硬解捕获打开失败，源={path}')
        else:
            print(f'create_video_reader: OpenCV 捕获打开失败，源={path}')
        return None
    return cap


def detect_source_mode(path, override='auto'):
    mode = (override or 'auto').lower()
    if mode in ('camera', 'file'):
        return mode
    if isinstance(path, str):
        lower = path.lower()
        if lower.startswith(('rtsp://', 'rtsp:', 'rtmp://', 'rtp://', 'http://', 'https://')):
            return 'camera'
        try:
            if Path(path).exists():
                return 'file'
        except OSError:
            pass
    return 'camera'


def _resolve_runtime_path(path_value, base_dir):
    if path_value in (None, ''):
        return None
    if isinstance(path_value, Path):
        path = path_value
    else:
        path = Path(str(path_value))
    path = path.expanduser()
    if not path.is_absolute():
        base = base_dir if base_dir else Path.cwd()
        path = base / path
    return path.resolve()


def _collect_storage_directories(config, base_dir):
    directories = []

    def push(value, treat_as_file=False):
        resolved = _resolve_runtime_path(value, base_dir)
        if not resolved:
            return
        directories.append(resolved.parent if treat_as_file else resolved)

    video_cfg = config.get('video', {}) or {}
    push(video_cfg.get('save_video'), treat_as_file=True)
    base = base_dir if base_dir else Path.cwd()
    directories.append((base / 'video_result').resolve())

    push(config.get('event_capture_dir'))
    directories.append((base / 'captures').resolve())

    push(config.get('event_output_dir'))
    directories.append((base / 'events').resolve())

    push(video_cfg.get('csv'), treat_as_file=True)
    debug_frame = video_cfg.get('debug_frame_path')
    if debug_frame:
        debug_path = _resolve_runtime_path(debug_frame, base_dir)
        if debug_path:
            directories.append(debug_path.parent if debug_path.suffix else debug_path)
    unique = []
    seen = set()
    for directory in directories:
        if not directory:
            continue
        resolved = Path(directory).resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        unique.append(resolved)
    return unique


def _is_normalized(points):
    if not points:
        return False
    return all(0.0 <= p[0] <= 1.0 and 0.0 <= p[1] <= 1.0 for p in points)


def scale_polygon(points, width, height):
    if not points:
        return []
    if _is_normalized(points):
        return [(float(x) * width, float(y) * height) for x, y in points]
    return [(float(x), float(y)) for x, y in points]


def scale_point(point, width, height):
    if not point:
        return (0.0, 0.0)
    x, y = point
    if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
        return float(x) * width, float(y) * height
    return float(x), float(y)


def get_anchor_point(box, offset_ratio=0.0):
    if not box:
        return None
    x1, y1, x2, y2 = box
    cx = 0.5 * (x1 + x2)
    height = max(1.0, (y2 - y1))
    offset = float(offset_ratio)
    if offset < 0.0:
        offset = 0.0
    elif offset > 0.95:
        offset = 0.95
    cy = y2 - offset * height
    return (float(cx), float(cy))


def letterbox(im, new_shape=640, color=(114, 114, 114)):
    shape = im.shape[:2]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
    ratio = (r, r)
    new_unpad = (int(round(shape[1] * r)), int(round(shape[0] * r)))
    dw = new_shape[1] - new_unpad[0]
    dh = new_shape[0] - new_unpad[1]
    dw /= 2
    dh /= 2
    if shape[::-1] != new_unpad:
        im = resize_for_letterbox(im, new_unpad)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    im = cv2.copyMakeBorder(im, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)
    return im, ratio, (dw, dh)


def resize_for_letterbox(im, new_unpad):
    if RGA_RESIZE_FUNC is not None:
        try:
            return RGA_RESIZE_FUNC(im, new_unpad)
        except Exception as exc:
            print(f'[rga-resize] failed, fallback to cv2: {exc}')
    return cv2.resize(im, new_unpad, interpolation=cv2.INTER_LINEAR)


def draw_metrics_overlay(frame, lines):
    if frame is None or not lines:
        return
    h, w = frame.shape[:2]
    scale = max(0.45, min(w, h) / 960.0 * 0.6)
    thickness = max(1, int(scale * 2))
    line_gap = max(14, int(18 * scale))
    y = h - 10
    for text in reversed(lines):
        cv2.putText(frame, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), thickness, cv2.LINE_AA)
        y -= line_gap


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def softmax(x, axis):
    x = x - np.max(x, axis=axis, keepdims=True)
    ex = np.exp(x)
    return ex / np.sum(ex, axis=axis, keepdims=True)


def decode_scale(reg, cls, obj, stride):
    B, _, H, W = reg.shape
    reg = reg.reshape(B, 4, REG_MAX, H, W)
    reg = softmax(reg, axis=2)
    reg = (reg * PROJECT.reshape(1, 1, REG_MAX, 1, 1)).sum(axis=2)
    reg = reg * stride
    gy, gx = np.meshgrid(np.arange(H), np.arange(W), indexing='ij')
    gx = (gx + 0.5) * stride
    gy = (gy + 0.5) * stride
    x1 = gx - reg[:, 0]
    y1 = gy - reg[:, 1]
    x2 = gx + reg[:, 2]
    y2 = gy + reg[:, 3]
    boxes = np.stack([x1, y1, x2, y2], axis=-1).reshape(-1, 4)
    cls = sigmoid(cls)
    obj = sigmoid(obj)
    cls = cls.reshape(B, cls.shape[1], -1)
    obj = obj.reshape(B, 1, -1)
    scores = (cls * obj).transpose(0, 2, 1).reshape(-1, cls.shape[1])
    cls_ids = np.argmax(scores, axis=1)
    cls_scores = scores[np.arange(scores.shape[0]), cls_ids]
    cls_flat = cls.transpose(0, 2, 1).reshape(-1, cls.shape[1])
    cls_probs = cls_flat[np.arange(cls_flat.shape[0]), cls_ids]
    return boxes, cls_ids, cls_scores, cls_probs


def nms(boxes, scores, thresh, max_det):
    if boxes.size == 0:
        return np.array([], dtype=int)
    order = scores.argsort()[::-1]
    keep = []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    while order.size > 0 and len(keep) < max_det:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        denom = areas[i] + areas[order[1:]] - inter + 1e-6
        ovr = inter / denom
        inds = np.where(ovr <= thresh)[0]
        order = order[inds + 1]
    return np.array(keep, dtype=int)


def scale_boxes(boxes, ratio, pad, shape):
    dw, dh = pad
    boxes[:, [0, 2]] -= dw
    boxes[:, [1, 3]] -= dh
    boxes[:, [0, 2]] /= ratio[0]
    boxes[:, [1, 3]] /= ratio[1]
    boxes[:, 0::2] = boxes[:, 0::2].clip(0, shape[1] - 1)
    boxes[:, 1::2] = boxes[:, 1::2].clip(0, shape[0] - 1)
    return boxes


def box_iou(boxA, boxB):
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])
    interW = max(0.0, xB - xA)
    interH = max(0.0, yB - yA)
    inter = interW * interH
    if inter <= 0:
        return 0.0
    areaA = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1]) + 1e-6
    areaB = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1]) + 1e-6
    return inter / (areaA + areaB - inter)


def draw_line(frame, line_pts, color, label=None):
    if not line_pts or len(line_pts) < 2:
        return
    p1 = tuple(map(int, line_pts[0]))
    p2 = tuple(map(int, line_pts[1]))
    cv2.line(frame, p1, p2, color, 2)
    for p in [p1, p2]:
        cv2.circle(frame, p, 4, color, -1)
    if label:
        cv2.putText(frame, label, (p1[0], max(0, p1[1] - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)


def box_iou(boxA, boxB):
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])
    interW = max(0.0, xB - xA)
    interH = max(0.0, yB - yA)
    inter = interW * interH
    if inter <= 0:
        return 0.0
    areaA = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1]) + 1e-6
    areaB = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1]) + 1e-6
    return inter / (areaA + areaB - inter)


def point_in_box(pt, box):
    if box is None or pt is None:
        return False
    x, y = pt
    return (box[0] <= x <= box[2]) and (box[1] <= y <= box[3])


def extract_plate_patch(frame, box, expand_ratio):
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = box
    cx = 0.5 * (x1 + x2)
    cy = 0.5 * (y1 + y2)
    bw = (x2 - x1)
    bh = (y2 - y1)
    bw = max(bw, 4)
    bh = max(bh, 4)
    bw *= (1.0 + expand_ratio)
    bh *= (1.0 + expand_ratio * 0.5)
    nx1 = max(0, int(cx - bw / 2))
    ny1 = max(0, int(cy - bh / 2))
    nx2 = min(w - 1, int(cx + bw / 2))
    ny2 = min(h - 1, int(cy + bh / 2))
    if nx2 <= nx1 or ny2 <= ny1:
        return None
    return frame[ny1:ny2, nx1:nx2]


def enhance_plate_patch(patch):
    if patch is None or patch.size == 0:
        return None
    # Rotate if taller than wide (rare but possible after crop)
    if patch.shape[0] > patch.shape[1] * 1.2:
        patch = cv2.rotate(patch, cv2.ROTATE_90_CLOCKWISE)
    patch = cv2.resize(patch, PLATE_SIZE, interpolation=cv2.INTER_LINEAR)
    lab = cv2.cvtColor(patch, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = CLAHE.apply(l)
    lab = cv2.merge((l, a, b))
    patch = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    patch = cv2.convertScaleAbs(patch, alpha=1.15, beta=5)
    return patch


def decode_lpr_output(pred):
    if pred is None:
        return ''
    arr = pred
    if arr.ndim == 3:
        if arr.shape[1] != len(LPR_CHARS):
            arr = np.transpose(arr, (0, 2, 1))
        arr = arr[0]
    elif arr.ndim == 2:
        arr = arr
    else:
        return ''
    prev = LPR_BLANK
    chars = []
    for t in range(arr.shape[1]):
        c = int(np.argmax(arr[:, t]))
        if c == LPR_BLANK:
            prev = c
            continue
        if c != prev:
            chars.append(LPR_CHARS[c])
        prev = c
    return ''.join(chars)


def normalize_plate_text(text):
    if not text:
        return ''
    text = text.upper().replace('·', '').replace('.', '').replace('•', '').replace(' ', '')
    filtered = ''.join(ch for ch in text if ch in ALNUM or ch in PROVINCE_CHARS)
    return filtered


def is_valid_plate(text):
    if not text:
        return False
    if PLATE_REGEX.match(text):
        return True
    if PLATE_REGEX_NE.match(text):
        return True
    return False


class PlateTextTracker:
    def __init__(self, lock_frames=5, iou_thresh=0.4, max_age=30):
        self.lock_frames = max(1, lock_frames)
        self.iou_thresh = iou_thresh
        self.max_age = max_age
        self.tracks = {}
        self.next_id = 1

    def _iou(self, boxA, boxB):
        xA = max(boxA[0], boxB[0])
        yA = max(boxA[1], boxB[1])
        xB = min(boxA[2], boxB[2])
        yB = min(boxA[3], boxB[3])
        interW = max(0.0, xB - xA)
        interH = max(0.0, yB - yA)
        inter = interW * interH
        if inter <= 0:
            return 0.0
        areaA = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1]) + 1e-6
        areaB = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1]) + 1e-6
        return inter / (areaA + areaB - inter)

    def update(self, frame_idx, detections):
        results = []
        det_boxes = [np.array(det['box'], dtype=float) for det in detections]
        det_texts = [normalize_plate_text(det.get('text', '')) for det in detections]
        track_ids = list(self.tracks.keys())
        iou_matrix = None
        if track_ids and det_boxes:
            iou_matrix = np.zeros((len(track_ids), len(det_boxes)), dtype=np.float32)
            for ti, tid in enumerate(track_ids):
                tbox = self.tracks[tid]['box']
                for di, dbox in enumerate(det_boxes):
                    iou_matrix[ti, di] = self._iou(tbox, dbox)
        assigned_tracks = {}
        assigned_dets = set()
        # direct assignments when detection already has track id
        for det_idx, det in enumerate(detections):
            tid = det.get('track_id', -1)
            if tid and tid > 0:
                assigned_dets.add(det_idx)
                assigned_tracks[tid] = det_idx
                if tid not in self.tracks:
                    self.tracks[tid] = {
                        'box': det_boxes[det_idx],
                        'last_seen': frame_idx,
                        'age': 0,
                        'history': [],
                        'locked': '',
                    }
                    if tid >= self.next_id:
                        self.next_id = tid + 1
                if iou_matrix is not None and tid in track_ids:
                    ti = track_ids.index(tid)
                    iou_matrix[ti, :] = -1
                    iou_matrix[:, det_idx] = -1
        if iou_matrix is not None:
            while True:
                idx = np.unravel_index(np.argmax(iou_matrix), iou_matrix.shape)
                max_iou = iou_matrix[idx]
                if max_iou < self.iou_thresh:
                    break
                ti, di = idx
                tid = track_ids[ti]
                assigned_tracks[tid] = di
                assigned_dets.add(di)
                iou_matrix[ti, :] = -1
                iou_matrix[:, di] = -1
        # update matched tracks
        for tid, det_idx in assigned_tracks.items():
            det_box = det_boxes[det_idx]
            det_text = det_texts[det_idx]
            track = self.tracks[tid]
            track['box'] = det_box
            track['last_seen'] = frame_idx
            track['age'] = 0
            if is_valid_plate(det_text):
                track['history'].append(det_text)
                if len(track['history']) > 30:
                    track['history'].pop(0)
                counts = Counter(track['history'])
                best_text, cnt = counts.most_common(1)[0]
                if cnt >= self.lock_frames:
                    track['locked'] = best_text
        # create new tracks for unmatched dets
        for di, det_box in enumerate(det_boxes):
            if di in assigned_dets:
                continue
            tid = self.next_id
            self.next_id += 1
            det_text = det_texts[di]
            history = []
            locked = ''
            if is_valid_plate(det_text):
                history.append(det_text)
            self.tracks[tid] = {
                'box': det_box,
                'last_seen': frame_idx,
                'age': 0,
                'history': history,
                'locked': locked,
            }
            assigned_tracks[tid] = di
        # age unmatched tracks
        to_delete = []
        for tid, track in self.tracks.items():
            if tid in assigned_tracks:
                continue
            track['age'] += 1
            if track['age'] > self.max_age:
                to_delete.append(tid)
        for tid in to_delete:
            self.tracks.pop(tid, None)
        # prepare results
        for det_idx, det in enumerate(detections):
            tid = None
            for track_id, d_idx in assigned_tracks.items():
                if d_idx == det_idx:
                    tid = track_id
                    break
            if tid is None:
                results.append({'track_id': -1, 'text': normalize_plate_text(det.get('text', '')), 'is_guess': False})
                continue
            track = self.tracks.get(tid)
            text = track.get('locked') or ''
            is_guess = False
            if not text:
                history = track.get('history') or []
                if history:
                    counts = Counter(history)
                    text, _ = counts.most_common(1)[0]
                    is_guess = True
            results.append({'track_id': tid, 'text': text, 'is_guess': is_guess})
        return results


class _KalmanFilterXYAH:
    def __init__(self):
        self._motion_mat = np.eye(8, dtype=np.float32)
        for i in range(4):
            self._motion_mat[i, i + 4] = 1.0
        self._update_mat = np.eye(4, 8, dtype=np.float32)
        self._std_weight_position = 1.0 / 20.0
        self._std_weight_velocity = 1.0 / 160.0

    def initiate(self, measurement):
        mean_pos = measurement
        mean_vel = np.zeros_like(mean_pos)
        mean = np.r_[mean_pos, mean_vel].astype(np.float32)
        std = [
            2.0 * self._std_weight_position * measurement[3],
            2.0 * self._std_weight_position * measurement[3],
            1e-2,
            2.0 * self._std_weight_position * measurement[3],
            10.0 * self._std_weight_velocity * measurement[3],
            10.0 * self._std_weight_velocity * measurement[3],
            1e-5,
            10.0 * self._std_weight_velocity * measurement[3],
        ]
        covariance = np.diag(np.square(std)).astype(np.float32)
        return mean, covariance

    def predict(self, mean, covariance):
        std_pos = [
            self._std_weight_position * mean[3],
            self._std_weight_position * mean[3],
            1e-2,
            self._std_weight_position * mean[3],
        ]
        std_vel = [
            self._std_weight_velocity * mean[3],
            self._std_weight_velocity * mean[3],
            1e-5,
            self._std_weight_velocity * mean[3],
        ]
        motion_cov = np.diag(np.square(np.r_[std_pos, std_vel])).astype(np.float32)
        mean = np.dot(self._motion_mat, mean)
        covariance = np.linalg.multi_dot((self._motion_mat, covariance, self._motion_mat.T)) + motion_cov
        return mean, covariance

    def project(self, mean, covariance):
        std = [
            self._std_weight_position * mean[3],
            self._std_weight_position * mean[3],
            1e-1,
            self._std_weight_position * mean[3],
        ]
        innovation_cov = np.diag(np.square(std)).astype(np.float32)
        mean = np.dot(self._update_mat, mean)
        covariance = np.linalg.multi_dot((self._update_mat, covariance, self._update_mat.T)) + innovation_cov
        return mean, covariance

    def update(self, mean, covariance, measurement):
        projected_mean, projected_cov = self.project(mean, covariance)
        kalman_gain = np.dot(np.dot(covariance, self._update_mat.T), np.linalg.inv(projected_cov))
        innovation = measurement - projected_mean
        new_mean = mean + np.dot(kalman_gain, innovation)
        new_covariance = covariance - np.linalg.multi_dot((kalman_gain, projected_cov, kalman_gain.T))
        return new_mean.astype(np.float32), new_covariance.astype(np.float32)


def _tlbr_to_tlwh(tlbr):
    x1, y1, x2, y2 = tlbr
    return np.array([x1, y1, x2 - x1, y2 - y1], dtype=np.float32)


def _tlwh_to_tlbr(tlwh):
    x, y, w, h = tlwh
    return np.array([x, y, x + w, y + h], dtype=np.float32)


def _tlwh_to_xyah(tlwh):
    x, y, w, h = tlwh
    cx = x + w / 2.0
    cy = y + h / 2.0
    a = w / max(h, 1e-6)
    return np.array([cx, cy, a, h], dtype=np.float32)


def _xyah_to_tlwh(xyah):
    cx, cy, a, h = xyah
    w = a * h
    x = cx - w / 2.0
    y = cy - h / 2.0
    return np.array([x, y, w, h], dtype=np.float32)


def _iou_cost_matrix(tracks_tlbr, dets_tlbr):
    if len(tracks_tlbr) == 0 or len(dets_tlbr) == 0:
        return np.zeros((len(tracks_tlbr), len(dets_tlbr)), dtype=np.float32)
    tracks = np.asarray(tracks_tlbr, dtype=np.float32)
    dets = np.asarray(dets_tlbr, dtype=np.float32)
    iou = np.zeros((tracks.shape[0], dets.shape[0]), dtype=np.float32)
    for i in range(tracks.shape[0]):
        x1, y1, x2, y2 = tracks[i]
        area1 = max(1.0, float((x2 - x1) * (y2 - y1)))
        for j in range(dets.shape[0]):
            xx1, yy1, xx2, yy2 = dets[j]
            area2 = max(1.0, float((xx2 - xx1) * (yy2 - yy1)))
            ix1 = max(x1, xx1)
            iy1 = max(y1, yy1)
            ix2 = min(x2, xx2)
            iy2 = min(y2, yy2)
            iw = max(0.0, ix2 - ix1)
            ih = max(0.0, iy2 - iy1)
            inter = iw * ih
            if inter <= 0.0:
                iou[i, j] = 0.0
            else:
                iou[i, j] = float(inter) / float(area1 + area2 - inter)
    return 1.0 - iou


def _linear_assignment(cost_matrix, cost_limit):
    if cost_matrix.size == 0:
        return [], list(range(cost_matrix.shape[0])), list(range(cost_matrix.shape[1]))
    cost = np.asarray(cost_matrix, dtype=np.float32)
    n_rows, n_cols = cost.shape
    transposed = False
    if n_rows > n_cols:
        cost = cost.T
        n_rows, n_cols = cost.shape
        transposed = True
    u = np.zeros(n_rows + 1, dtype=np.float32)
    v = np.zeros(n_cols + 1, dtype=np.float32)
    p = np.zeros(n_cols + 1, dtype=np.int32)
    way = np.zeros(n_cols + 1, dtype=np.int32)
    for i in range(1, n_rows + 1):
        p[0] = i
        j0 = 0
        minv = np.full(n_cols + 1, np.inf, dtype=np.float32)
        used = np.zeros(n_cols + 1, dtype=np.bool_)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = np.inf
            j1 = 0
            for j in range(1, n_cols + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1, j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(0, n_cols + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    match_col = np.full(n_rows + 1, -1, dtype=np.int32)
    for j in range(1, n_cols + 1):
        if p[j] != 0:
            match_col[p[j]] = j
    matches = []
    for i in range(1, n_rows + 1):
        j = match_col[i]
        if j <= 0:
            continue
        c = cost[i - 1, j - 1]
        if c <= cost_limit:
            if transposed:
                matches.append((j - 1, i - 1))
            else:
                matches.append((i - 1, j - 1))
    if transposed:
        row_assigned = {j for j, _ in matches}
        col_assigned = {i for _, i in matches}
        unmatched_rows = [i for i in range(cost_matrix.shape[0]) if i not in row_assigned]
        unmatched_cols = [j for j in range(cost_matrix.shape[1]) if j not in col_assigned]
    else:
        row_assigned = {i for i, _ in matches}
        col_assigned = {j for _, j in matches}
        unmatched_rows = [i for i in range(cost_matrix.shape[0]) if i not in row_assigned]
        unmatched_cols = [j for j in range(cost_matrix.shape[1]) if j not in col_assigned]
    return matches, unmatched_rows, unmatched_cols


class _STrack:
    Tracked = 1
    Lost = 2
    Removed = 3

    def __init__(self, tlbr, score, cls_id):
        self.tlwh = _tlbr_to_tlwh(tlbr)
        self.score = float(score)
        self.cls_id = int(cls_id)
        self.track_id = -1
        self.state = _STrack.Tracked
        self.is_activated = False
        self.frame_id = 0
        self.start_frame = 0
        self.time_since_update = 0
        self.mean = None
        self.covariance = None

    def activate(self, kf, frame_id, track_id):
        self.track_id = int(track_id)
        self.mean, self.covariance = kf.initiate(_tlwh_to_xyah(self.tlwh))
        self.frame_id = int(frame_id)
        self.start_frame = int(frame_id)
        self.time_since_update = 0
        self.state = _STrack.Tracked
        self.is_activated = True

    def predict(self, kf):
        if self.mean is None or self.covariance is None:
            return
        self.mean, self.covariance = kf.predict(self.mean, self.covariance)
        self.tlwh = _xyah_to_tlwh(self.mean[:4])
        self.time_since_update += 1

    def update(self, kf, tlbr, score, cls_id, frame_id):
        self.tlwh = _tlbr_to_tlwh(tlbr)
        if self.mean is not None and self.covariance is not None:
            self.mean, self.covariance = kf.update(self.mean, self.covariance, _tlwh_to_xyah(self.tlwh))
            self.tlwh = _xyah_to_tlwh(self.mean[:4])
        self.score = float(score)
        self.cls_id = int(cls_id)
        self.frame_id = int(frame_id)
        self.time_since_update = 0
        self.state = _STrack.Tracked
        self.is_activated = True

    def mark_lost(self):
        self.state = _STrack.Lost

    def mark_removed(self):
        self.state = _STrack.Removed

    def tlbr(self):
        return _tlwh_to_tlbr(self.tlwh)


class ByteTrackTracker:
    def __init__(self, track_thresh=0.5, low_thresh=0.1, match_thresh=0.3, track_buffer=60):
        self.track_thresh = float(track_thresh)
        self.low_thresh = float(low_thresh)
        self.match_thresh = float(match_thresh)
        self.track_buffer = int(track_buffer)
        self.kf = _KalmanFilterXYAH()
        self.tracked = []
        self.lost = []
        self.removed = []
        self.frame_id = 0
        self.next_id = 1

    def _new_id(self):
        tid = self.next_id
        self.next_id += 1
        return tid

    def update(self, frame_idx, detections):
        self.frame_id = int(frame_idx)
        dets = []
        for det in detections:
            tlbr = np.array(det['box'], dtype=np.float32)
            score = float(det.get('score', 0.0))
            cls_id = int(det.get('cls', -1))
            if score < self.low_thresh:
                continue
            dets.append((tlbr, score, cls_id))
        high = [d for d in dets if d[1] >= self.track_thresh]
        low = [d for d in dets if self.low_thresh <= d[1] < self.track_thresh]

        for t in self.tracked:
            t.predict(self.kf)
        for t in self.lost:
            t.predict(self.kf)

        strack_pool = [t for t in self.tracked if t.state == _STrack.Tracked] + [t for t in self.lost if t.state == _STrack.Lost]

        matches = []
        unmatched_tracks = list(range(len(strack_pool)))
        unmatched_high = list(range(len(high)))
        if strack_pool and high:
            track_boxes = [t.tlbr() for t in strack_pool]
            det_boxes = [d[0] for d in high]
            cost = _iou_cost_matrix(track_boxes, det_boxes)
            matches, unmatched_tracks, unmatched_high = _linear_assignment(cost, 1.0 - self.match_thresh)

        activated = []
        refind = []
        for ti, di in matches:
            trk = strack_pool[ti]
            tlbr, score, cls_id = high[di]
            trk.update(self.kf, tlbr, score, cls_id, self.frame_id)
            if trk in self.lost:
                self.lost.remove(trk)
                refind.append(trk)
            else:
                activated.append(trk)

        remaining_tracked = [strack_pool[i] for i in unmatched_tracks]
        low_matches = []
        unmatched_low = list(range(len(low)))
        if remaining_tracked and low:
            track_boxes = [t.tlbr() for t in remaining_tracked]
            det_boxes = [d[0] for d in low]
            cost = _iou_cost_matrix(track_boxes, det_boxes)
            low_matches, u_trk2, unmatched_low = _linear_assignment(cost, 1.0 - self.match_thresh)
            still_unmatched = []
            for idx in u_trk2:
                still_unmatched.append(remaining_tracked[idx])
            remaining_tracked = still_unmatched
        for ti, di in low_matches:
            trk = remaining_tracked[ti]
            tlbr, score, cls_id = low[di]
            trk.update(self.kf, tlbr, score, cls_id, self.frame_id)
            if trk in self.lost:
                self.lost.remove(trk)
                refind.append(trk)
            else:
                activated.append(trk)

        for trk in remaining_tracked:
            if trk.state != _STrack.Tracked:
                continue
            trk.mark_lost()
            self.lost.append(trk)

        new_tracks = []
        for di in unmatched_high:
            tlbr, score, cls_id = high[di]
            trk = _STrack(tlbr, score, cls_id)
            trk.activate(self.kf, self.frame_id, self._new_id())
            new_tracks.append(trk)

        self.tracked = [t for t in self.tracked if t.state == _STrack.Tracked]
        self.tracked.extend([t for t in activated if t not in self.tracked])
        self.tracked.extend([t for t in refind if t not in self.tracked])
        self.tracked.extend(new_tracks)

        kept_lost = []
        for t in self.lost:
            if (self.frame_id - t.frame_id) <= self.track_buffer:
                kept_lost.append(t)
            else:
                t.mark_removed()
                self.removed.append(t)
        self.lost = kept_lost

        assignments = [-1] * len(detections)
        det_to_tid = {}
        for idx, det in enumerate(detections):
            score = float(det.get('score', 0.0))
            if score < self.low_thresh:
                continue
            tlbr = np.array(det['box'], dtype=np.float32)
            det_to_tid[idx] = None
        if detections:
            active_tracks = {t.track_id: t for t in self.tracked if t.state == _STrack.Tracked and t.is_activated}
            if active_tracks:
                t_ids = list(active_tracks.keys())
                t_boxes = [active_tracks[tid].tlbr() for tid in t_ids]
                d_boxes = [np.array(det['box'], dtype=np.float32) for det in detections]
                cost = _iou_cost_matrix(t_boxes, d_boxes)
                m, _, _ = _linear_assignment(cost, 1.0 - self.match_thresh)
                for ti, di in m:
                    assignments[di] = t_ids[ti]
        return assignments

    def get_active_tracks(self):
        return {t.track_id: {'box': [int(x) for x in t.tlbr().tolist()], 'cls': t.cls_id, 'score': t.score} for t in self.tracked if t.state == _STrack.Tracked}


VehicleTracker = ByteTrackTracker


class EventUploader:
    def __init__(self, url=None, token=None, timeout=8.0, queue_path=None,
                 max_retries=10, base_delay=1.0, max_delay=60.0):
        self.url = (url or '').strip()
        self.token = token
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.base_delay = max(0.5, float(base_delay))
        self.max_delay = max(self.base_delay, float(max_delay))
        self.db = None
        self.thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        if self.url:
            db_path = Path(queue_path) if queue_path else (Path('./events') / 'upload_queue.db')
            self.db = SQLiteUploadQueue(db_path)
            self.thread = threading.Thread(target=self._worker, daemon=True)
            self.thread.start()

    def enqueue(self, payload):
        if not self.db or payload is None:
            return
        self.db.enqueue(payload)
        self._wake.set()

    def _worker(self):
        while not self._stop.is_set():
            job = self.db.next_job() if self.db else None
            if not job:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            job_id, payload, retries = job
            try:
                self._send(payload)
            except Exception as exc:
                delay = min(self.base_delay * (2 ** retries), self.max_delay)
                if retries + 1 > self.max_retries:
                    print(f'[uploader] drop event after {retries} retries: {exc}')
                    if self.db:
                        self.db.mark_success(job_id)
                else:
                    print(f'[uploader] failed to send event (retry in {delay:.1f}s): {exc}')
                    if self.db:
                        self.db.mark_failure(job_id, retries + 1, delay)
                continue
            if self.db:
                self.db.mark_success(job_id)

    def _send(self, data):
        body = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(self.url, data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        if self.token:
            req.add_header('Authorization', f'Bearer {self.token}')
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()

    def close(self):
        if not self.thread:
            if self.db:
                self.db.close()
            return
        self._stop.set()
        self._wake.set()
        self.thread.join(timeout=2.0)
        if self.db:
            pending = self.db.pending()
            if pending:
                print(f'[uploader] pending events retained in queue ({pending})')
            self.db.close()


class EventManager:
    def __init__(self, config, fps, frame_size, zone_manager, event_log_path=None, uploader=None, capture_mode='path', is_detour=False):
        self.config = config
        self.is_detour = is_detour
        self.logic = config.get('logic', {})
        self.zone_mgr = zone_manager
        self.fps = fps
        self.frame_w, self.frame_h = frame_size
        self.camera_id = config.get('camera_id', 'CAM')
        self.capture_dir = Path(config.get('event_capture_dir', './captures'))
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir = Path(config.get('event_output_dir', './events'))
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.tracks = {}
        self.timeout_frames = int(config.get('track_timeout_frames', 60))
        self.base_time = datetime.now()
        self.stationary_min_frames = int(config.get('stationary_min_frames', 0))
        self.stationary_speed_thresh = float(config.get('stationary_speed_thresh', 8.0))
        self.min_water_hit_frames_for_wash = int(self.logic.get('min_water_hit_frames_for_wash', 30))
        self.water_window_size = int(self.logic.get('water_window_size', 20))
        self.water_window_min_hits = int(self.logic.get('water_window_min_hits', 3))
        self.vehicle_shrink_ratio = float(config.get('vehicle_shrink_ratio', 0.35))
        self.vehicle_lock_min_votes = int(config.get('vehicle_lock_min_votes', 80))
        self.vehicle_lock_on_confirm = bool(config.get('vehicle_lock_on_confirm', True))
        shadow_cfg = config.get('shadow_pool', {})
        self.shadow_max = int(shadow_cfg.get('max_candidates', 50))
        self.shadow_max_age = int(shadow_cfg.get('max_age_frames', 120))
        self.shadow_pool = {}
        self.event_log_path = Path(event_log_path) if event_log_path else None
        if self.event_log_path:
            self.event_log_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.event_log_path.exists():
                with self.event_log_path.open('w', encoding='utf-8') as f:
                    f.write('camera_id,track_id,type,capture_time,frame_idx,stationary_frames,'
                            'wash_duration,plate,vehicle,direction_code,direction_label,plate_is_guess,anchor_dwell_frames\n')
        self.uploader = uploader
        self.capture_mode = capture_mode
        self.lane_name = config.get('lane_name', '冲洗')
        self.default_plate_color = config.get('default_plate_color', '')
        self.default_plate_color_conf = float(config.get('default_plate_color_conf', 0.0))
        self.default_cleanliness = int(config.get('default_cleanliness', 0))
        self.anchor_offset_ratio = float(self.logic.get('anchor_offset_ratio', 0.0))
        if self.anchor_offset_ratio < 0.0:
            self.anchor_offset_ratio = 0.0
        elif self.anchor_offset_ratio > 0.95:
            self.anchor_offset_ratio = 0.95
        self.zone_b_anchor_min_frames = int(self.logic.get('zone_b_anchor_min_frames', 0))
        if self.zone_b_anchor_min_frames < 0:
            self.zone_b_anchor_min_frames = 0
        self.min_type5_zone_a_dwell = int(self.logic.get('min_zone_a_dwell_frames_for_type5', 0))
        if self.min_type5_zone_a_dwell < 0:
            self.min_type5_zone_a_dwell = 0

        self.min_type1_track_frames = int(self.logic.get('min_track_frames_for_type1', 10))
        if self.min_type1_track_frames < 10:
            self.min_type1_track_frames = 10

        self.min_type2_track_frames = int(self.logic.get('min_track_frames_for_type2', 6))
        if self.min_type2_track_frames < 1:
            self.min_type2_track_frames = 1
        self.min_type2_vehicle_votes = int(self.logic.get('min_vehicle_votes_for_type2', 3))
        if self.min_type2_vehicle_votes < 0:
            self.min_type2_vehicle_votes = 0
        self.wash_dwell_offset = 0.0
        self.min_type4_zone_b_dwell = int(self.logic.get('min_zone_b_dwell_frames_for_type4', 60))
        if self.min_type4_zone_b_dwell < 0:
            self.min_type4_zone_b_dwell = 0
        self.allowed_events = {1, 2, 3, 4, 5, 6}
        if self.is_detour:
            # For detour lane, we don't need type 3 (wash start) and 4 (wash end)
            self.allowed_events = {1, 2, 5, 6}
        self.disable_plate_only_events = True
        self.single_lifecycle_events = True
        self.require_vehicle_type_for_events = bool(self.logic.get('require_vehicle_type_for_events', False))
        self.max_per_id_video_seconds = 600.0
        self.pending_events = {}
        self.upload_buffer = {}
        self.upload_qualified = set()
        self.upload_log_full = None
        self.upload_log_sent = None
        if self.uploader:
            self.upload_log_full = self.events_dir / 'upload_log_full.csv'
            self.upload_log_sent = self.events_dir / 'upload_log_sent.csv'
            if not self.upload_log_full.exists():
                with self.upload_log_full.open('w', encoding='utf-8') as f:
                    f.write('capture_time,id,type,sent,payload\n')
            if not self.upload_log_sent.exists():
                with self.upload_log_sent.open('w', encoding='utf-8') as f:
                    f.write('capture_time,id,type,payload\n')
        self.frame_timing = {}

    def record_frame_timing(self, frame_idx, capture_ts, infer_ts):
        if capture_ts is None or infer_ts is None:
            return
        try:
            self.frame_timing[int(frame_idx)] = (float(capture_ts), float(infer_ts))
        except Exception:
            return

    def frame_timestamp(self, frame_idx):
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def update_track(self, track_id, plate_box, vehicle_box, plate_text, frame_idx, frame,
                     water_boxes, water_active, is_plate, vehicle_label, vehicle_conf,
                     plate_conf, confirmed, cleaning_label='', anchor_point=None, plate_is_guess=False):
        if track_id <= 0:
            return
        # Avoid creating a parallel lifecycle keyed only by plate_id.
        if self.disable_plate_only_events and is_plate and track_id not in self.tracks and vehicle_box is None:
            return
        st = self.tracks.setdefault(track_id, {
            'events': set(),
            'stationary_frames': 0,
            'speed_buf': deque(maxlen=6),
            'wash_duration': 0.0,
            'washing': False,
            'washing_candidate': False,
            'washing_confirmed': False,
            'water_detected': False,
            'water_hit_frames': 0,
            'water_window': deque(maxlen=20),
            'effective_wash_frames': 0,
            'last_frame_idx': frame_idx,
            'last_frame': None,
            'plate_text': '',
            'plate_is_guess': False,
            'vehicle_cls': '',
            'last_plate_box': None,
            'last_vehicle_box': None,
            'confirmed': False,
            'plate_conf_history': [],
            'vehicle_conf_history': [],
            'wash_start_time': None,
            'wash_end_time': None,
            'lane': self.lane_name,
            'last_cleaning': '',
            'vehicle_cls_frozen': False,
            'class_counts': {},
            'vehicle_cls_locked': '',
            'last_vehicle_label': '',
            'zone_state': None,
            'last_anchor': None,
            'last_type3_frame': -1,
            'last_type4_frame': -1,
            'zone_b_enter_frame': -1,
            'zone_b_dwell_frames': 0,
            'zone_a_enter_frame': -1,
            'zone_a_dwell_frames': 0,
            'track_frame_count': 0,
            'abnormal_reasons': set(),
        })
        if self.single_lifecycle_events and st.get('closed'):
            st['last_frame_idx'] = frame_idx
            return
        st['track_frame_count'] = st.get('track_frame_count', 0) + 1
        st['last_frame_idx'] = frame_idx
        if frame is not None:
            st['last_frame'] = frame.copy()
        freeze_label = st.get('vehicle_cls_frozen', False)
        if vehicle_box is not None and st.get('last_vehicle_box') is not None:
            prev = st['last_vehicle_box']
            prev_area = max(1.0, (prev[2] - prev[0]) * (prev[3] - prev[1]))
            new_area = max(1.0, (vehicle_box[2] - vehicle_box[0]) * (vehicle_box[3] - vehicle_box[1]))
            if new_area < prev_area * self.vehicle_shrink_ratio:
                freeze_label = True
        counts = st.get('class_counts') or {}
        if vehicle_label and not freeze_label:
            counts[vehicle_label] = counts.get(vehicle_label, 0) + 1
            st['class_counts'] = counts
            locked, locked_count = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
            st['vehicle_cls_locked'] = locked
            st['vehicle_cls'] = locked
            if locked_count >= self.vehicle_lock_min_votes:
                st['vehicle_cls_frozen'] = True
        elif vehicle_label and st.get('vehicle_cls_locked') and vehicle_label == st['vehicle_cls_locked']:
            counts[vehicle_label] = counts.get(vehicle_label, 0) + 1
            st['class_counts'] = counts
        elif not st.get('vehicle_cls') and st.get('vehicle_cls_locked'):
            st['vehicle_cls'] = st['vehicle_cls_locked']
        if vehicle_label:
            st['last_vehicle_label'] = vehicle_label
            if not st.get('vehicle_cls'):
                st['vehicle_cls'] = vehicle_label
        if freeze_label and st.get('vehicle_cls_locked'):
            st['vehicle_cls_frozen'] = True
        if vehicle_box is not None:
            st['last_vehicle_box'] = vehicle_box
        if plate_box is not None:
            st['last_plate_box'] = plate_box
        normalized_plate = normalize_plate_text(plate_text)
        if normalized_plate:
            st['plate_text'] = normalized_plate
            st['plate_is_guess'] = bool(plate_is_guess)
            self._add_shadow_candidate(track_id, normalized_plate, plate_conf, frame_idx)
        elif plate_conf and plate_conf > 0.0:
            self._add_shadow_candidate(track_id, plate_text, plate_conf, frame_idx)
        if confirmed:
            st['confirmed'] = True
            if self.vehicle_lock_on_confirm and st.get('vehicle_cls_locked'):
                st['vehicle_cls_frozen'] = True
        if vehicle_conf is not None:
            history = st.get('vehicle_conf_history') or []
            history.append(float(vehicle_conf))
            if len(history) > 60:
                history.pop(0)
            st['vehicle_conf_history'] = history
        if plate_conf is not None:
            history = st.get('plate_conf_history') or []
            history.append(float(plate_conf))
            if len(history) > 60:
                history.pop(0)
            st['plate_conf_history'] = history
        if cleaning_label:
            st['last_cleaning'] = cleaning_label
        ref_box = vehicle_box or plate_box or st.get('last_vehicle_box') or st.get('last_plate_box')
        prev_box = st.get('last_vehicle_box')
        dist = 0.0
        if ref_box is not None and prev_box is not None:
            cx = 0.5 * (ref_box[0] + ref_box[2])
            cy = 0.5 * (ref_box[1] + ref_box[3])
            px = 0.5 * (prev_box[0] + prev_box[2])
            py = 0.5 * (prev_box[1] + prev_box[3])
            dist = hypot(cx - px, cy - py)
        st['speed_buf'].append(dist)
        avg_speed = sum(st['speed_buf']) / max(len(st['speed_buf']), 1)
        speed_thresh = self.stationary_speed_thresh
        if vehicle_box is None and plate_box is not None:
            speed_thresh *= 1.5
        if avg_speed <= speed_thresh:
            st['stationary_frames'] = min(st['stationary_frames'] + 1, 100000)
        else:
            st['stationary_frames'] = max(st['stationary_frames'] - 1, 0)

        if anchor_point is None:
            anchor_point = get_anchor_point(vehicle_box or plate_box or st.get('last_vehicle_box') or st.get('last_plate_box'),
                                            self.anchor_offset_ratio)
        zone_flags = {'enter_a': False, 'exit_a': False, 'enter_b': False, 'exit_b': False}
        zone_state = st.get('zone_state')
        if anchor_point:
            st['last_anchor'] = anchor_point
        zone_state, zone_flags = self.zone_mgr.update_track(track_id, anchor_point, frame_idx)
        st['zone_state'] = zone_state

        timestamp = self.frame_timestamp(frame_idx)
        water_hit = self.water_contact(ref_box, water_boxes)
        water_signal = bool(water_hit or water_active or bool(water_boxes))
        inside_a = bool(zone_state and zone_state.inside_a)
        inside_b = bool(zone_state and zone_state.inside_b)
        if zone_flags.get('enter_a'):
            st['zone_a_enter_frame'] = frame_idx
            st['zone_a_dwell_frames'] = 0
        enter_a_frame = st.get('zone_a_enter_frame', -1)
        zone_a_elapsed = 0
        if inside_a:
            if enter_a_frame < 0:
                st['zone_a_enter_frame'] = frame_idx
                enter_a_frame = frame_idx
            zone_a_elapsed = frame_idx - enter_a_frame
            st['zone_a_dwell_frames'] = zone_a_elapsed
        else:
            if enter_a_frame >= 0:
                zone_a_elapsed = frame_idx - enter_a_frame
                st['zone_a_dwell_frames'] = zone_a_elapsed
            st['zone_a_enter_frame'] = -1
        if zone_flags.get('enter_b'):
            st['zone_b_enter_frame'] = frame_idx
            st['zone_b_dwell_frames'] = 0
            st['water_detected'] = False
            st['wash_duration'] = 0.0
            st['water_hit_frames'] = 0
            win = st.get('water_window')
            if isinstance(win, deque):
                win.clear()
            else:
                win = deque(maxlen=self.water_window_size)
            st['water_window'] = win
            st['effective_wash_frames'] = 0
        enter_frame = st.get('zone_b_enter_frame', -1)
        anchor_elapsed = 0
        if inside_b:
            if enter_frame < 0:
                st['zone_b_enter_frame'] = frame_idx
                enter_frame = frame_idx
            anchor_elapsed = frame_idx - enter_frame
            st['zone_b_dwell_frames'] = anchor_elapsed
        else:
            if enter_frame >= 0:
                anchor_elapsed = frame_idx - enter_frame
                st['zone_b_dwell_frames'] = anchor_elapsed
            st['zone_b_enter_frame'] = -1
        meets_anchor_delay = (not inside_b) or (anchor_elapsed >= self.zone_b_anchor_min_frames)
        candidate_active = bool(inside_b and meets_anchor_delay)
        event_enabled = bool(
            st.get('zone_a_dwell_frames', 0) > 0
            or inside_a
            or zone_flags.get('enter_a')
            or zone_flags.get('exit_a')
        )
        if not candidate_active:
            st['washing_candidate'] = False
        else:
            st['washing_candidate'] = True
        hit = 1 if inside_b and water_boxes else 0
        if inside_b:
            st['water_hit_frames'] = st.get('water_hit_frames', 0) + hit
            win = st.get('water_window')
            if not isinstance(win, deque):
                win = deque(maxlen=self.water_window_size)
            if win.maxlen != self.water_window_size:
                win = deque(win, maxlen=self.water_window_size)
            win.append(hit)
            st['water_window'] = win
            if sum(win) >= self.water_window_min_hits:
                st['effective_wash_frames'] = st.get('effective_wash_frames', 0) + 1
        water_ready = st.get('water_hit_frames', 0) >= self.min_water_hit_frames_for_wash
        stationary_ready = (self.stationary_min_frames > 0 and
                            st['stationary_frames'] >= self.stationary_min_frames)
        water_seconds = st.get('effective_wash_frames', 0) / max(self.fps, 1e-6)
        trigger_ready = bool(candidate_active and water_seconds >= 5.0)
        if water_signal:
            st['water_detected'] = True
        was_washing = bool(st.get('washing'))
        just_confirmed = False
        if candidate_active and trigger_ready and not st.get('washing_confirmed'):
            st['washing_confirmed'] = True
            just_confirmed = True
        if not candidate_active and not was_washing:
            st['washing_confirmed'] = False
        washing_now = bool(candidate_active and st.get('washing_confirmed'))

        if just_confirmed:
            st['wash_start_time'] = timestamp

        if self.disable_plate_only_events and is_plate and (vehicle_box is None and st.get('last_vehicle_box') is None):
            return
        can_type1 = True
        if self.min_type1_track_frames > 0:
            if st.get('zone_a_dwell_frames', 0) < self.min_type1_track_frames:
                can_type1 = False
        if bool(zone_state and zone_state.inside_a) and 1 not in st['events'] and 1 in self.allowed_events and can_type1:
            self.emit_event(track_id, 1, frame_idx, frame, {'captureTime': timestamp}, st)
            st['events'].add(1)
        if event_enabled and zone_flags.get('enter_b') and 2 not in st['events'] and 2 in self.allowed_events:
            can_type2 = True
            if self.min_type2_track_frames > 0:
                if st.get('track_frame_count', 0) < self.min_type2_track_frames:
                    can_type2 = False
            if self.min_type2_vehicle_votes > 0:
                counts = st.get('class_counts') or {}
                top_votes = 0
                if counts:
                    try:
                        top_votes = max(int(v) for v in counts.values())
                    except Exception:
                        top_votes = 0
                if top_votes < self.min_type2_vehicle_votes:
                    can_type2 = False

            if can_type2:
                if 1 in self.allowed_events and 1 not in st['events']:
                    backfill_type1 = True
                    if self.min_type1_track_frames > 0:
                        if st.get('zone_a_dwell_frames', 0) < self.min_type1_track_frames:
                            backfill_type1 = False
                    if backfill_type1:
                        self.emit_event(track_id, 1, frame_idx, frame, {'captureTime': timestamp}, st)
                        st['events'].add(1)
                self.emit_event(track_id, 2, frame_idx, frame, {'captureTime': timestamp}, st)
                st['events'].add(2)
        if event_enabled and just_confirmed and 3 in self.allowed_events:
            self.emit_event(track_id, 3, frame_idx, frame, {
                'captureTime': timestamp,
                'washStartTime': timestamp,
            }, st)
            st['events'].add(3)
        if washing_now:
            st['wash_duration'] = self._compute_effective_wash_duration(st, frame_idx)
        can_type4 = False
        if zone_flags.get('exit_b'):
            if st.get('zone_b_dwell_frames', 0) >= self.min_type4_zone_b_dwell:
                can_type4 = True
        if event_enabled and can_type4 and 4 in self.allowed_events:
            st['wash_end_time'] = st.get('wash_end_time') or timestamp
            duration_val = self._compute_effective_wash_duration(st, frame_idx)
            st['wash_duration'] = duration_val
            self.emit_event(track_id, 4, frame_idx, frame, {
                'captureTime': timestamp,
                'washDuration': round(duration_val, 2),
            }, st)
            st['events'].add(4)
        can_type5 = True
        if self.min_type5_zone_a_dwell > 0:
            if st.get('zone_a_dwell_frames', 0) < self.min_type5_zone_a_dwell:
                can_type5 = False
        if zone_flags.get('exit_a') and 5 in self.allowed_events and 5 not in st['events'] and can_type5:
            st['wash_end_time'] = st.get('wash_end_time') or timestamp
            duration_val = self._compute_effective_wash_duration(st, frame_idx)
            st['wash_duration'] = duration_val
            reasons = st.get('abnormal_reasons')
            if reasons is None:
                reasons = set()
                st['abnormal_reasons'] = reasons
            if 2 not in st['events']:
                reasons.add('MISSING_TYPE2')
            if st.get('water_detected') and 3 not in st['events']:
                reasons.add('MISSING_TYPE3')
            if st.get('zone_b_dwell_frames', 0) > 0 and 4 not in st['events']:
                reasons.add('MISSING_TYPE4')
            self.emit_event(track_id, 5, frame_idx, frame, {
                'captureTime': timestamp,
                'washDuration': round(duration_val, 2),
            }, st)
            st['events'].add(5)
            if self.single_lifecycle_events:
                st['closed'] = True

        st['washing'] = washing_now
        if not st['washing']:
            st['washing_confirmed'] = False
        st['debug'] = {
            'state': 'washing' if st.get('washing') else 'idle',
            'stationary': st['stationary_frames'],
            'speed': round(avg_speed, 1),
            'wash_duration': round(st.get('wash_duration', 0.0), 1),
            'water': bool(washing_now),
            'plate': st.get('plate_text', ''),
            'zone_a': bool(zone_state and zone_state.inside_a),
            'zone_b': bool(zone_state and zone_state.inside_b),
            'zone_a_elapsed': zone_a_elapsed,
            'zone_b_elapsed': anchor_elapsed,
            'water_detected': bool(st.get('water_detected')),
        }

        elapsed_seconds = st.get('track_frame_count', 0) / max(self.fps, 1e-6)
        if elapsed_seconds >= self.max_per_id_video_seconds and not st.get('closed'):
            reasons = st.get('abnormal_reasons')
            if reasons is None:
                reasons = set()
                st['abnormal_reasons'] = reasons
            if 'OVER_10_MINUTES' not in reasons:
                reasons.add('OVER_10_MINUTES')
            if st.get('record_start_frame') is not None and st.get('record_stop_frame') is None:
                extra_frames = int(max(self.fps, 1.0) * 5.0)
                last_idx = st.get('last_frame_idx', frame_idx)
                stop_frame = last_idx + extra_frames
                prev_stop = st.get('record_stop_frame')
                if prev_stop is None or stop_frame > prev_stop:
                    st['record_stop_frame'] = stop_frame
            if self.uploader:
                track_key = f'{self.camera_id}_{track_id}'
                buffer = self.upload_buffer.pop(track_key, [])
                if buffer:
                    updated = []
                    for p in buffer:
                        payload = dict(p)
                        payload['isAbnormal'] = True
                        old_reason = str(payload.get('abnormalReason') or '').strip()
                        if old_reason:
                            parts = set(r for r in old_reason.split('|') if r)
                        else:
                            parts = set()
                        parts.add('OVER_10_MINUTES')
                        payload['abnormalReason'] = '|'.join(sorted(parts))
                        updated.append(payload)
                    self.upload_qualified.add(track_key)
                    for payload in updated:
                        sent_now = False
                        try:
                            self.uploader.enqueue(payload)
                            sent_now = True
                        except Exception:
                            sent_now = False
                        if self.upload_log_sent and sent_now:
                            try:
                                text = json.dumps(payload, ensure_ascii=False)
                                with self.upload_log_sent.open('a', encoding='utf-8') as f:
                                    f.write(f"{self.frame_timestamp(st.get('last_frame_idx', frame_idx))},{track_key},0,{text}\n")
                            except Exception:
                                pass
                        if self.upload_log_full:
                            try:
                                text = json.dumps(payload, ensure_ascii=False)
                                with self.upload_log_full.open('a', encoding='utf-8') as f:
                                    f.write(f"{self.frame_timestamp(st.get('last_frame_idx', frame_idx))},{track_key},0,1,{text}\n")
                            except Exception:
                                pass
                else:
                    self.upload_qualified.add(track_key)
            st['closed'] = True

    def flush_inactive(self, active_ids, frame_idx, on_track_timeout=None):
        active_ids = active_ids or set()
        to_remove = []
        for tid, st in self.tracks.items():
            if tid in active_ids:
                continue
            if frame_idx - st.get('last_frame_idx', frame_idx) >= self.timeout_frames:
                event_enabled = bool(st.get('zone_a_dwell_frames', 0) > 0)
                if 4 not in st['events'] and 4 in self.allowed_events and event_enabled and st.get('water_detected') and st.get('zone_b_dwell_frames', 0) > 0:
                    last_frame = st.get('last_frame_idx', frame_idx)
                    st['wash_end_time'] = st.get('wash_end_time') or self.frame_timestamp(last_frame)
                    duration_val = self._compute_effective_wash_duration(st, last_frame)
                    st['wash_duration'] = duration_val
                    self.emit_event(tid, 4, last_frame, st.get('last_frame'), {
                        'captureTime': self.frame_timestamp(last_frame),
                        'washDuration': round(duration_val, 2),
                    }, st)
                    st['events'].add(4)
                can_type5 = True
                if self.min_type5_zone_a_dwell > 0:
                    if st.get('zone_a_dwell_frames', 0) < self.min_type5_zone_a_dwell:
                        can_type5 = False
                if 5 not in st['events'] and 5 in self.allowed_events and can_type5 and event_enabled:
                    timestamp = self.frame_timestamp(st.get('last_frame_idx', frame_idx))
                    st['wash_end_time'] = st.get('wash_end_time') or timestamp
                    duration_val = self._compute_effective_wash_duration(st, st.get('last_frame_idx', frame_idx))
                    st['wash_duration'] = duration_val
                    extras = {
                        'captureTime': timestamp,
                        'washDuration': round(duration_val, 2),
                    }
                    reasons = st.get('abnormal_reasons')
                    if reasons is None:
                        reasons = set()
                        st['abnormal_reasons'] = reasons
                    if 2 not in st['events']:
                        reasons.add('MISSING_TYPE2')
                    if st.get('water_detected') and 3 not in st['events']:
                        reasons.add('MISSING_TYPE3')
                    if st.get('zone_b_dwell_frames', 0) > 0 and 4 not in st['events']:
                        reasons.add('MISSING_TYPE4')
                    self.emit_event(tid, 5, st.get('last_frame_idx', frame_idx), st.get('last_frame'), extras, st)
                    st['events'].add(5)
                if st.get('record_start_frame') is not None and st.get('record_stop_frame') is None:
                    extra_frames = int(max(self.fps, 1.0) * 5.0)
                    last_idx = st.get('last_frame_idx', frame_idx)
                    st['record_stop_frame'] = last_idx + extra_frames
                if self.single_lifecycle_events and 5 in st['events']:
                    st['closed'] = True
                if on_track_timeout is not None:
                    try:
                        on_track_timeout(tid, st)
                    except Exception:
                        pass
                to_remove.append(tid)
        for tid in to_remove:
            self.shadow_pool.pop(tid, None)
            self.zone_mgr.drop_track(tid)
            self.pending_events.pop(tid, None)
            key = f'{self.camera_id}_{tid}'
            self.upload_buffer.pop(key, None)
            self.tracks.pop(tid, None)

    def emit_event(self, track_id, event_type, frame_idx, frame, payload, track_state):
        if event_type not in self.allowed_events:
            return
        vehicle_type = payload.get('vehicleType') or self._resolve_vehicle_type(track_state)
        if self.require_vehicle_type_for_events and event_type in (3, 4, 5):
            if not (vehicle_type and str(vehicle_type).strip()):
                pending = self.pending_events.setdefault(track_id, [])
                pending.append({
                    'event_type': event_type,
                    'frame_idx': frame_idx,
                    'frame': frame.copy() if frame is not None else None,
                    'payload': dict(payload),
                })
                return
        if self.require_vehicle_type_for_events and vehicle_type and str(vehicle_type).strip():
            self._flush_pending_events(track_id, vehicle_type, track_state)
        self._emit_event_core(track_id, event_type, frame_idx, frame, payload, track_state, vehicle_type)

    def _emit_event_core(self, track_id, event_type, frame_idx, frame, payload, track_state, vehicle_type):
        anchor_dwell = 0
        dbg = track_state.get('debug', {})
        if dbg:
            anchor_dwell = int(dbg.get('zone_b_elapsed', 0) or 0)
        capture_time = payload.get('captureTime') or self.frame_timestamp(frame_idx)
        try:
            track_state['last_event_capture_time'] = capture_time
        except Exception:
            pass
        if event_type == 1:
            prev_type1_time = track_state.get('type1_capture_time')
            if not prev_type1_time:
                track_state['type1_capture_time'] = capture_time
                try:
                    dt = datetime.strptime(capture_time, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    dt = datetime.now()
                ts_str = dt.strftime("%Y%m%d%H%M")
                device_name = self.config.get('system', {}).get('device_id') or self.camera_id
                session_id = f"{device_name}-{ts_str}-{track_id}"
                track_state['session_id'] = session_id
        session_id = track_state.get('session_id')
        if not session_id:
            base_time = track_state.get('type1_capture_time') or capture_time
            try:
                dt = datetime.strptime(base_time, "%Y-%m-%d %H:%M:%S")
            except Exception:
                dt = datetime.now()
            ts_str = dt.strftime("%Y%m%d%H%M")
            device_name = self.config.get('system', {}).get('device_id') or self.camera_id
            session_id = f"{device_name}-{ts_str}-{track_id}"
            track_state['session_id'] = session_id
        # Start per-id recording when the track becomes meaningful (type2).
        # We'll prebuffer a short window so the clip includes a bit of context.
        if event_type == 2:
            prev_start = track_state.get('record_start_frame')
            prebuffer = int(max(0, int(self.logic.get('per_id_prebuffer_frames', int(max(self.fps, 1.0) * 2.0)))))
            start_frame = max(0, frame_idx - prebuffer)
            if prev_start is None or start_frame < prev_start:
                track_state['record_start_frame'] = start_frame
        if track_state.get('record_start_frame') is None and track_state.get('record_stop_frame') is not None:
            track_state['record_start_frame'] = frame_idx
        if event_type == 5:
            extra_frames = int(max(self.fps, 1.0) * 5.0)
            stop_frame = frame_idx + extra_frames
            prev_stop = track_state.get('record_stop_frame')
            if prev_stop is None or stop_frame > prev_stop:
                track_state['record_stop_frame'] = stop_frame
        try:
            track_state[f'last_event_t{event_type}_capture_time'] = capture_time
        except Exception:
            pass
        capture_path = None
        if frame is not None:
            fname = f'{self.camera_id}_{track_id}_t{event_type}_{frame_idx}.jpg'
            capture_path = str(self.capture_dir / fname)
            try:
                h, w = frame.shape[:2]
                target_w, target_h = 1920, 1080
                if w != target_w or h != target_h:
                    frame_to_save = cv2.resize(frame, (target_w, target_h))
                else:
                    frame_to_save = frame
                params = [int(cv2.IMWRITE_JPEG_QUALITY), 85]
                cv2.imwrite(capture_path, frame_to_save, params)
            except Exception:
                capture_path = None
        event = {
            'id': session_id,
            'trackId': track_id,
            'type': event_type,
            'captureTime': capture_time,
            'plateNumber': payload.get('plateNumber') or '',
            'vehicleType': vehicle_type,
            'washDuration': payload.get('washDuration', 0.0),
            'washStartTime': payload.get('washStartTime', ''),
            'captureImage': capture_path or '',
            'lane': self.lane_name,
            'anchorDwellFrames': anchor_dwell,
        }
        reasons = track_state.get('abnormal_reasons') if track_state else None
        if reasons:
            if isinstance(reasons, set):
                reasons_list = sorted(reasons)
            else:
                reasons_list = sorted(str(x) for x in reasons if x)
            if reasons_list:
                event['isAbnormal'] = True
                event['abnormalReason'] = '|'.join(reasons_list)
        plate_text, is_guess = self._resolve_plate_with_shadow(track_id, track_state, frame_idx)
        if not event['plateNumber']:
            event['plateNumber'] = plate_text
        if not event['plateNumber']:
            event['isAbnormal'] = True
            reason = 'PLATE_MISSING'
            if 'abnormalReason' in event and event['abnormalReason']:
                parts = set(str(x).strip() for x in str(event['abnormalReason']).split('|') if x)
                parts.add(reason)
                event['abnormalReason'] = '|'.join(sorted(parts))
            else:
                event['abnormalReason'] = reason
        event['plateIsGuess'] = bool(is_guess and event['plateNumber'])
        dir_code = 0
        dir_label = ''
        if event_type == 5:
            dir_code, dir_label = self._resolve_direction(track_state)
            event['direction'] = dir_code
            event['directionLabel'] = dir_label
        plate_color, plate_color_conf = self._infer_plate_color(track_state)
        event['plateColor'] = plate_color
        event['plateColorConfidence'] = plate_color_conf
        if event_type == 5:
            event['washEndTime'] = track_state.get('wash_end_time') or event['captureTime']
            event['videoEndTime'] = self.frame_timestamp(track_state.get('last_frame_idx', frame_idx))
            event['totalWashDuration'] = round(track_state.get('wash_duration', 0.0), 2)
            video_duration = 0.0
            type1_time = track_state.get('type1_capture_time')
            if type1_time:
                try:
                    dt1 = datetime.strptime(type1_time, "%Y-%m-%d %H:%M:%S")
                    dt5 = datetime.strptime(event['captureTime'], "%Y-%m-%d %H:%M:%S")
                    delta = (dt5 - dt1).total_seconds()
                    if delta > 0:
                        video_duration = round(delta, 2)
                except Exception:
                    video_duration = 0.0
            event['videoDuration'] = video_duration
            event['cleanliness'] = self.default_cleanliness
        if track_state.get('wash_start_time') and not event.get('washStartTime'):
            event['washStartTime'] = track_state.get('wash_start_time')
        capture_ts_val = None
        infer_ts_val = None
        if hasattr(self, 'frame_timing'):
            t = self.frame_timing.get(int(frame_idx))
            if t:
                capture_ts_val, infer_ts_val = t
        if capture_ts_val is not None and infer_ts_val is not None:
            now_ts = time.time()
            decode_to_infer = max(0.0, infer_ts_val - capture_ts_val)
            infer_to_event = max(0.0, now_ts - infer_ts_val)
            total_latency = max(0.0, now_ts - capture_ts_val)
            try:
                cap_str = datetime.fromtimestamp(capture_ts_val).strftime("%H:%M:%S.%f")[:-3]
                infer_str = datetime.fromtimestamp(infer_ts_val).strftime("%H:%M:%S.%f")[:-3]
                event_str = datetime.fromtimestamp(now_ts).strftime("%H:%M:%S.%f")[:-3]
                print(f"[latency] 帧={frame_idx} 轨迹={track_id} 类型={event_type} 捕获={cap_str} 推理完成={infer_str} 告警发送={event_str} 解码→推理={decode_to_infer*1000:.1f}ms 推理→告警={infer_to_event*1000:.1f}ms 总时延={total_latency*1000:.1f}ms")
            except Exception:
                pass
        event_path = self.events_dir / f'{self.camera_id}_{track_id}_t{event_type}_{frame_idx}.json'
        try:
            with event_path.open('w', encoding='utf-8') as f:
                json.dump(event, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        print(f"[EVENT] cam={self.camera_id} track={track_id} type={event_type} time={event['captureTime']}")
        if self.event_log_path:
            try:
                with self.event_log_path.open('a', encoding='utf-8') as f:
                    f.write(f"{self.camera_id},{track_id},{event_type},{event['captureTime']},{frame_idx},"
                            f"{track_state.get('stationary_frames',0)},{round(track_state.get('wash_duration',0.0),2)},"
                            f"{event['plateNumber']},{event['vehicleType']},{dir_code},{dir_label},"
                            f"{int(event['plateIsGuess'])},{anchor_dwell}\n")
            except Exception:
                pass
        if self.uploader:
            api_payload = self._build_api_payload(event, track_state, frame_idx)
            if api_payload:
                track_key = event['id']
                sent_now = False
                is_abnormal = bool(api_payload.get('isAbnormal'))
                if event_type == 1:
                    buffer = self.upload_buffer.setdefault(track_key, [])
                    buffer.append(api_payload)
                elif event_type == 2:
                    buffer = self.upload_buffer.pop(track_key, [])
                    buffer.append(api_payload)
                    self.upload_qualified.add(track_key)
                    for p in buffer:
                        try:
                            self.uploader.enqueue(p)
                        except Exception:
                            continue
                        sent_now = True
                        if self.upload_log_sent:
                            try:
                                text = json.dumps(p, ensure_ascii=False)
                                with self.upload_log_sent.open('a', encoding='utf-8') as f:
                                    f.write(f"{event['captureTime']},{track_key},{event_type},{text}\n")
                            except Exception:
                                pass
                elif track_key in self.upload_qualified:
                    try:
                        self.uploader.enqueue(api_payload)
                        sent_now = True
                        if self.upload_log_sent:
                            try:
                                text = json.dumps(api_payload, ensure_ascii=False)
                                with self.upload_log_sent.open('a', encoding='utf-8') as f:
                                    f.write(f"{event['captureTime']},{track_key},{event_type},{text}\n")
                            except Exception:
                                pass
                    except Exception:
                        sent_now = False
                else:
                    buffer = self.upload_buffer.setdefault(track_key, [])
                    buffer.append(api_payload)
                if self.upload_log_full:
                    try:
                        text = json.dumps(api_payload, ensure_ascii=False)
                        with self.upload_log_full.open('a', encoding='utf-8') as f:
                            f.write(f"{event['captureTime']},{track_key},{event_type},{int(sent_now)},{text}\n")
                    except Exception:
                        pass
        if event_type == 3:
            track_state['last_type3_frame'] = frame_idx
        elif event_type == 4:
            track_state['last_type4_frame'] = frame_idx

    def _flush_pending_events(self, track_id, vehicle_type, track_state):
        entries = self.pending_events.pop(track_id, None)
        if not entries:
            return
        for entry in entries:
            et = entry.get('event_type')
            fi = entry.get('frame_idx')
            fr = entry.get('frame')
            payload = dict(entry.get('payload') or {})
            if not payload.get('vehicleType'):
                payload['vehicleType'] = vehicle_type
            self._emit_event_core(track_id, et, fi, fr, payload, track_state, vehicle_type)

    def _add_shadow_candidate(self, track_id, text, conf, frame_idx):
        text = normalize_plate_text(text)
        if not text:
            return
        pool = self.shadow_pool.setdefault(track_id, deque())
        pool.append({
            'text': text,
            'conf': float(conf) if conf is not None else 0.5,
            'frame': frame_idx,
        })
        while len(pool) > self.shadow_max:
            pool.popleft()
        while pool and frame_idx - pool[0]['frame'] > self.shadow_max_age:
            pool.popleft()

    def _resolve_plate_with_shadow(self, track_id, track_state, frame_idx):
        text = track_state.get('plate_text', '')
        if text:
            return text, bool(track_state.get('plate_is_guess', False))
        pool = self.shadow_pool.get(track_id)
        if not pool:
            return '', False
        total = len(pool)
        best_text = ''
        best_score = 0.0
        unique = set(entry['text'] for entry in pool if entry['text'])
        for candidate in unique:
            conf_sum = 0.0
            count = 0
            for entry in pool:
                if entry['text'] != candidate:
                    continue
                decay = max(0.2, 1.0 - (frame_idx - entry['frame']) / max(self.shadow_max_age, 1))
                conf_sum += (entry['conf'] or 0.5) * decay
                count += 1
            if count == 0:
                continue
            score = (conf_sum / count) * (count / total)
            if score > best_score:
                best_score = score
                best_text = candidate
        return best_text, bool(best_text)

    def _compute_effective_wash_duration(self, track_state, frame_idx):
        if not track_state.get('water_detected'):
            return 0.0
        hits = track_state.get('water_hit_frames', 0)
        if hits < self.min_water_hit_frames_for_wash:
            return 0.0
        frames = track_state.get('effective_wash_frames', 0)
        if frames <= 0:
            return 0.0
        seconds = frames / max(self.fps, 1e-6)
        return max(0.0, seconds)

    def _avg(self, values):
        if not values:
            return 0.0
        return float(sum(values) / len(values))

    def _infer_plate_color(self, track_state):
        vehicle = (track_state.get('vehicle_cls') or '').lower()
        plate = track_state.get('plate_text', '') or ''
        length = len(plate)
        if vehicle == 'car':
            if length == 8:
                return '绿色', 0.9
            return '蓝色', 0.9
        if vehicle == 'blue truck':
            return '蓝色', 0.9
        if vehicle in ('yellow truck', 'dump truck'):
            return '黄色', 0.9
        if vehicle == 'wuxiao':
            return '蓝色', 0.9
        if self.default_plate_color:
            return self.default_plate_color, self.default_plate_color_conf
        return '', 0.0

    def _resolve_vehicle_type(self, track_state, fallback=''):
        if not track_state:
            return fallback or ''
        locked = track_state.get('vehicle_cls_locked', '')
        if locked:
            return locked
        current = track_state.get('vehicle_cls', '')
        if current:
            return current
        counts = track_state.get('class_counts') or {}
        if counts:
            locked = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
            if locked:
                return locked
        return fallback or track_state.get('last_vehicle_label', '') or ''

    def get_locked_vehicle(self, track_id):
        st = self.tracks.get(track_id)
        if not st:
            return ''
        return st.get('vehicle_cls_locked') or st.get('vehicle_cls', '')

    def get_track_debug(self, track_id):
        st = self.tracks.get(track_id)
        if not st:
            return None
        dbg = st.get('debug', {})
        return {
            'state': dbg.get('state', 'idle'),
            'stationary': dbg.get('stationary', 0),
            'speed': dbg.get('speed', 0.0),
            'water': dbg.get('water', False),
            'wash_duration': dbg.get('wash_duration', 0.0),
            'plate': dbg.get('plate', ''),
            'zone_a': dbg.get('zone_a', False),
            'zone_b': dbg.get('zone_b', False),
        }

    def _resolve_vehicle_type(self, track_state, fallback=''):
        if not track_state:
            return fallback or ''
        locked = track_state.get('vehicle_cls_locked', '')
        if locked:
            return locked
        current = track_state.get('vehicle_cls', '')
        if current:
            return current
        counts = track_state.get('class_counts') or {}
        if counts:
            locked = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
            if locked:
                return locked
        return fallback or track_state.get('last_vehicle_label', '') or ''

    def _prepare_capture_image(self, capture_path):
        if not capture_path:
            return ''
        if self.capture_mode == 'base64':
            try:
                with open(capture_path, 'rb') as f:
                    return base64.b64encode(f.read()).decode('utf-8')
            except Exception:
                return ''
        return capture_path

    def _build_api_payload(self, event, track_state, frame_idx):
        if not self.uploader:
            return None
        evt_type = event['type']
        raw_vehicle_type = event.get('vehicleType') or self._resolve_vehicle_type(track_state)
        vehicle_type_cn = VEHICLE_LABEL_CN.get(raw_vehicle_type, raw_vehicle_type or '')
        capture_time = event.get('captureTime') or self.frame_timestamp(frame_idx)
        lane = self.lane_name
        plate_number = event.get('plateNumber', '')
        if not plate_number and track_state:
            plate_number = track_state.get('plate_text', '')

        if evt_type == 6:
            return {
                'id': event['id'],
                'type': evt_type,
                'lane': lane,
                'captureTime': capture_time,
                'plateNumber': plate_number,
                'vehicleType': vehicle_type_cn,
                'isAbnormal': False,
                'abnormalReason': ''
            }
        
        plate_conf = round(self._avg(track_state.get('plate_conf_history')), 3) if track_state else 0.0
        vehicle_conf = round(self._avg(track_state.get('vehicle_conf_history')), 3) if track_state else 0.0
        capture_image = self._prepare_capture_image(event.get('captureImage'))
        plate_color = event.get('plateColor', self.default_plate_color)
        plate_color_conf = event.get('plateColorConfidence', self.default_plate_color_conf)
        plate_is_guess = event.get('plateIsGuess', False)
        reasons = track_state.get('abnormal_reasons') if track_state else None
        wash_start_time = track_state.get('wash_start_time') if track_state else None
        dir_code = 0
        dir_label = ''
        wash_end_time = None
        video_end_time = None
        total_wash_duration = None
        cleanliness = None
        video_duration = None
        if evt_type == 5:
            dir_code, dir_label = self._resolve_direction(track_state)
            wash_end_time = track_state.get('wash_end_time') or capture_time
            video_end_time = self.frame_timestamp(track_state.get('last_frame_idx', frame_idx))
            if self.is_detour:
                total_wash_duration = 0.0
            else:
                total_wash_duration = round(track_state.get('wash_duration', 0.0), 2)
            cleanliness = self.default_cleanliness
            video_duration = event.get('videoDuration')
        payload = {}
        payload['id'] = event['id']
        payload['type'] = evt_type
        payload['captureTime'] = capture_time
        payload['captureImage'] = capture_image
        if evt_type == 1:
            payload['lane'] = lane
            payload['plateNumber'] = plate_number
            payload['plateConfidence'] = plate_conf
            payload['plateColor'] = plate_color
            payload['plateColorConfidence'] = plate_color_conf
            payload['vehicleType'] = vehicle_type_cn
            payload['vehicleTypeConfidence'] = vehicle_conf
            payload['plateIsGuess'] = plate_is_guess
            if wash_start_time:
                payload['washStartTime'] = wash_start_time
        elif evt_type in (2, 3, 4):
            payload['lane'] = lane
            payload['plateNumber'] = plate_number
            payload['plateConfidence'] = plate_conf
            payload['plateColor'] = plate_color
            payload['plateColorConfidence'] = plate_color_conf
            payload['vehicleType'] = vehicle_type_cn
            payload['vehicleTypeConfidence'] = vehicle_conf
            payload['plateIsGuess'] = plate_is_guess
            if wash_start_time:
                payload['washStartTime'] = wash_start_time
        elif evt_type == 5:
            payload['washEndTime'] = wash_end_time
            payload['videoEndTime'] = video_end_time
            payload['totalWashDuration'] = total_wash_duration
            payload['cleanliness'] = cleanliness
            payload['videoDuration'] = video_duration
            payload['plateNumber'] = plate_number
            payload['plateConfidence'] = plate_conf
            payload['plateColor'] = plate_color
            payload['plateColorConfidence'] = plate_color_conf
            payload['vehicleType'] = vehicle_type_cn
            payload['vehicleTypeConfidence'] = vehicle_conf
            payload['lane'] = lane
            payload['plateIsGuess'] = plate_is_guess
            if wash_start_time:
                payload['washStartTime'] = wash_start_time
            payload['direction'] = dir_code
            payload['directionLabel'] = dir_label
        else:
            payload['lane'] = lane
            payload['plateNumber'] = plate_number
            payload['plateConfidence'] = plate_conf
            payload['plateColor'] = plate_color
            payload['plateColorConfidence'] = plate_color_conf
            payload['vehicleType'] = vehicle_type_cn
            payload['vehicleTypeConfidence'] = vehicle_conf
            payload['plateIsGuess'] = plate_is_guess
            if wash_start_time:
                payload['washStartTime'] = wash_start_time
        payload['isAbnormal'] = False
        payload['abnormalReason'] = ''
        if reasons:
            if isinstance(reasons, set):
                reasons_list = sorted(reasons)
            else:
                reasons_list = sorted(str(x) for x in reasons if x)
            if reasons_list:
                payload['isAbnormal'] = True
                payload['abnormalReason'] = '|'.join(reasons_list)
        return payload

    def _resolve_direction(self, track_state):
        state = None
        if track_state:
            state = track_state.get('zone_state')
        return self.zone_mgr.resolve_direction(state)

    def water_contact(self, box, water_boxes):
        if not water_boxes or box is None:
            return False
        for wb in water_boxes:
            if box_iou(box, wb) >= 0.02:
                return True
        return False


class DetectWorker(threading.Thread):
    def __init__(self, idx, args, core_mask, task_q, result_q, detect_mask=None):
        super().__init__(daemon=True)
        self.idx = idx
        self.args = args
        self.core_mask = core_mask
        self.task_q = task_q
        self.result_q = result_q
        self.detect_mask = detect_mask
        self.rk = RKNNLite()
        if self.rk.load_rknn(args.model) != 0:
            raise RuntimeError('load_rknn failed')
        init_kwargs = {}
        if core_mask is not None:
            init_kwargs['core_mask'] = core_mask
        if self.rk.init_runtime(**init_kwargs) != 0:
            raise RuntimeError('init_runtime failed')
        self.frames = 0
        self.infer_time = 0.0
        self.stop = False
        self.lpr = None
        self.plate_expand = max(0.0, getattr(args, 'plate_expand', PLATE_EXPAND_DEFAULT))
        lpr_path = getattr(args, 'lpr_model', None)
        if lpr_path:
            lpr_path = Path(lpr_path)
            if lpr_path.exists():
                lpr = RKNNLite()
                lpr_mask = parse_core_mask(getattr(args, 'lpr_core_mask', None))
                lpr_init_kwargs = {}
                if lpr_mask is not None:
                    lpr_init_kwargs['core_mask'] = lpr_mask
                if lpr.load_rknn(str(lpr_path)) == 0 and lpr.init_runtime(**lpr_init_kwargs) == 0:
                    self.lpr = lpr
                    print(f'Worker {idx}: LPR model loaded from {lpr_path} with core_mask={lpr_mask}')
                else:
                    print(f'Worker {idx}: failed to init LPR model {lpr_path}')
            else:
                print(f'Worker {idx}: LPR model not found at {lpr_path}')

    def run(self):
        while True:
            item = self.task_q.get()
            if item is None:
                self.task_q.task_done()
                break
            if len(item) == 2:
                frame_idx, frame = item
                capture_ts = None
            else:
                frame_idx, frame, capture_ts = item
            proc_frame = frame
            if self.detect_mask is not None:
                proc_frame = cv2.bitwise_and(frame, frame, mask=self.detect_mask)
            lb_img, ratio, pad = letterbox(proc_frame, self.args.imgsz)
            img = cv2.cvtColor(lb_img, cv2.COLOR_BGR2RGB).astype(np.uint8)
            t0 = time.time()
            outputs = self.rk.inference(inputs=[np.expand_dims(img, 0)], data_format=['nhwc'])
            infer_time = time.time() - t0
            self.infer_time += infer_time
            self.frames += 1
            if not outputs or len(outputs) != 9:
                self.result_q.put((frame_idx, capture_ts, frame, [], []))
                self.task_q.task_done()
                continue
            boxes_list = []
            scores_list = []
            classes_list = []
            cls_prob_list = []
            for i, stride in enumerate(STRIDES):
                reg = outputs[i * 3 + 0]
                cls = outputs[i * 3 + 1]
                obj = outputs[i * 3 + 2]
                boxes, cls_ids, cls_scores, cls_probs = decode_scale(reg, cls, obj, stride)
                per_class_conf = np.array([CLASS_THRESH.get(int(c), self.args.conf) for c in cls_ids])
                keep = cls_scores >= per_class_conf
                if not np.any(keep):
                    continue
                boxes_list.append(boxes[keep])
                scores_list.append(cls_scores[keep])
                classes_list.append(cls_ids[keep])
                cls_prob_list.append(cls_probs[keep])
            if not boxes_list:
                self.result_q.put((frame_idx, capture_ts, frame, [], []))
                self.task_q.task_done()
                continue
            boxes = np.concatenate(boxes_list, axis=0)
            scores = np.concatenate(scores_list, axis=0)
            classes = np.concatenate(classes_list, axis=0)
            cls_probs = np.concatenate(cls_prob_list, axis=0)
            boxes = scale_boxes(boxes, ratio, pad, frame.shape)
            keep = nms(boxes, scores, self.args.iou, self.args.max_det)
            boxes = boxes[keep]
            scores = scores[keep]
            classes = classes[keep]
            cls_probs = cls_probs[keep]

            csv_rows = []
            det_payload = []
            base_frame = frame
            draw_frame = frame if self.args.no_draw else frame.copy()
            for box, score, cls_id, cls_prob in zip(boxes, scores, classes, cls_probs):
                x1, y1, x2, y2 = box.astype(int)
                plate_text = ''
                if cls_id == LICENSE_CLASS and self.lpr is not None:
                    plate_text = self.recognize_plate(base_frame, (x1, y1, x2, y2))
                label_name = CLASS_NAMES[cls_id]
                label = f'{label_name} {cls_prob:.2f}'
                draw_now = True
                if label_name in VEHICLE_LABEL_CN:
                    draw_now = False  # 延后到跟踪阶段，根据锁定车型决定是否绘制
                if draw_now and not self.args.no_draw:
                    color = select_box_color(label_name)
                    cv2.rectangle(draw_frame, (x1, y1), (x2, y2), color, 2)
                    cv2.putText(draw_frame, label, (x1, max(0, y1 - 12)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 2, cv2.LINE_AA)
                csv_rows.append([frame_idx, label_name, f'{score:.4f}', x1, y1, x2, y2, -1, '', plate_text])
                det_payload.append({
                    'cls': int(cls_id),
                    'score': float(score),
                    'box': [int(x1), int(y1), int(x2), int(y2)],
                    'text': plate_text,
                    'row_idx': len(csv_rows) - 1,
                    'label': label_name,
                })
            self.result_q.put((frame_idx, capture_ts, draw_frame, csv_rows, det_payload))
            self.task_q.task_done()
        self.result_q.put(None)

    def recognize_plate(self, frame, box):
        try:
            crop = extract_plate_patch(frame, box, self.plate_expand)
            patch = enhance_plate_patch(crop)
            if patch is None:
                return ''
            outputs = self.lpr.inference(inputs=[np.expand_dims(patch, 0)], data_format=['nhwc'])
            if not outputs:
                return ''
            text = decode_lpr_output(outputs[0])
            return text
        except Exception:
            return ''


def monitor_loop(interval, stop_event):
    if interval <= 0:
        return
    if psutil is None:
        print('[monitor] psutil not installed, monitoring disabled.')
        return
    proc = None
    try:
        proc = psutil.Process(os.getpid())
    except Exception:
        proc = None
    started = time.time()
    count = 0
    sum_cpu = 0.0
    sum_mem = 0.0
    max_cpu = 0.0
    max_mem = 0.0
    while not stop_event.is_set():
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        rss_mb = 0.0
        if proc is not None:
            try:
                rss_mb = proc.memory_info().rss / (1024 * 1024)
            except Exception:
                rss_mb = 0.0
        count += 1
        sum_cpu += cpu
        sum_mem += mem
        if cpu > max_cpu:
            max_cpu = cpu
        if mem > max_mem:
            max_mem = mem
        avg_cpu = sum_cpu / max(1, count)
        avg_mem = sum_mem / max(1, count)
        uptime = time.time() - started
        msg = (
            f'[monitor] cpu={cpu:.1f}% mem={mem:.1f}% rss={rss_mb:.1f}MB '
            f'avg_cpu={avg_cpu:.1f}% max_cpu={max_cpu:.1f}% '
            f'avg_mem={avg_mem:.1f}% max_mem={max_mem:.1f}% '
            f'uptime={uptime:.0f}s'
        )
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                first = next(iter(temps.values()))
                if first:
                    msg += f' temp={first[0].current:.1f}C'
        except Exception:
            pass
        print(msg, flush=True)
        stop_event.wait(interval)


def process_video(path, args):
    cap = create_video_reader(path, args)
    if cap is None or not hasattr(cap, 'isOpened') or not cap.isOpened():
        print(f'failed to open {path}')
        return
    fps = None
    if hasattr(cap, 'get'):
        try:
            fps = cap.get(cv2.CAP_PROP_FPS)
        except AttributeError:
            fps = None
    if not fps:
        fps = getattr(cap, 'fps', None)
    if not fps:
        fps = 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    config = getattr(args, '_config', load_config(None))
    video_cfg = config.get('video', {})
    logic_cfg = config.get('logic', {})
    system_cfg = config.get('system', {})
    reader_fail_threshold = max(1, int(config.get('reader_fail_threshold', 5)))
    reader_reconnect_delay = max(0.0, float(config.get('reader_reconnect_delay', 2.0)))
    reader_max_reconnect = max(0, int(config.get('reader_max_reconnect', 0)))
    source_mode = detect_source_mode(path, getattr(args, 'source_mode', 'auto'))
    is_file_input = (source_mode == 'file')
    if is_file_input:
        reader_max_reconnect = 0
    else:
        source_mode = 'camera'
    print(f'[reader] source_mode={source_mode}')
    consecutive_fails = 0
    reconnect_count = 0

    base_dir = getattr(args, '_config_dir', Path.cwd())
    metrics_path_conf = system_cfg.get('metrics_path', '/dev/shm/cleaningcar_metrics.json')
    metrics_path = None
    if metrics_path_conf:
        metrics_path = _resolve_runtime_path(metrics_path_conf, base_dir)
    zones_cfg = config.get('zones', {})
    logic_cfg = config.get('logic', {})
    anchor_offset_ratio = float(logic_cfg.get('anchor_offset_ratio', 0.0))
    if anchor_offset_ratio < 0.0:
        anchor_offset_ratio = 0.0
    elif anchor_offset_ratio > 0.95:
        anchor_offset_ratio = 0.95
    zone_b_anchor_min_frames = int(logic_cfg.get('zone_b_anchor_min_frames', 0))
    if zone_b_anchor_min_frames < 0:
        zone_b_anchor_min_frames = 0
    zone_a_pts = scale_polygon(zones_cfg.get('zone_a_detection', []), width, height)
    zone_b_pts = scale_polygon(zones_cfg.get('zone_b_wash', []), width, height)
    flow_vec = zones_cfg.get('flow_vector', {})
    flow_start = scale_point(flow_vec.get('start', (0.0, 0.0)), width, height)
    flow_end = scale_point(flow_vec.get('end', (0.0, 1.0)), width, height)
    zone_mgr = ZoneManager(
        zone_a_pts,
        zone_b_pts,
        (flow_start, flow_end),
        entry_hysteresis=int(logic_cfg.get('zone_b_entry_hysteresis', 3)),
        exit_hysteresis=int(logic_cfg.get('zone_b_exit_hysteresis', 3)),
        zone_a_entry_hysteresis=int(logic_cfg.get('zone_a_entry_hysteresis', 3)),
        zone_a_exit_hysteresis=int(logic_cfg.get('zone_a_exit_hysteresis', 3)),
    )
    detect_mask = None
    if logic_cfg.get('zone_a_mask_enable', True) and len(zone_a_pts) >= 3:
        detect_mask = polygon_mask(zone_a_pts, (height, width))
        print('zone_a_mask: 启用，仅在Zone A内检测')

    def dual_anchor_for(box):
        if not box:
            return None, None
        head = get_anchor_point(box, anchor_offset_ratio)
        if not head:
            return None, None
        fx = flow_end[0] - flow_start[0]
        fy = flow_end[1] - flow_start[1]
        norm = (fx * fx + fy * fy) ** 0.5
        if norm <= 1e-6:
            return head, head
        height = max(1.0, (box[3] - box[1]))
        shift = 0.3 * height
        ux = fx / norm
        uy = fy / norm
        tail = (head[0] - ux * shift, head[1] - uy * shift)
        return head, tail

    def anchor_point_for(box):
        head, tail = dual_anchor_for(box)
        return tail or head

    output_dir = getattr(args, 'output_dir', None)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    video_writer = None
    csv_writer = None
    csv_f = None
    plate_lock_frames = int(getattr(args, 'plate_lock_frames', 2))
    plate_tracker = PlateTextTracker(
        lock_frames=plate_lock_frames,
        max_age=max(int(config.get('track_timeout_frames', 60)) * 2, plate_lock_frames * 6)
    )
    # ByteTrack thresholds (keep legacy config keys for backwards compatibility)
    bt_track_thresh = float(config.get('bt_track_thresh', float(config.get('vehicle_track_thresh', 0.45))))
    bt_low_thresh = float(config.get('bt_low_thresh', float(config.get('vehicle_low_thresh', 0.10))))
    bt_match_thresh = float(config.get('bt_match_thresh', float(config.get('vehicle_iou_threshold', 0.30))))
    if bt_match_thresh < 0.0:
        bt_match_thresh = 0.0
    elif bt_match_thresh > 1.0:
        bt_match_thresh = 1.0
    bt_buffer = int(config.get('track_max_age', 60))
    vehicle_tracker = VehicleTracker(
        track_thresh=bt_track_thresh,
        low_thresh=bt_low_thresh,
        match_thresh=bt_match_thresh,
        track_buffer=bt_buffer,
    )
    default_event_log = os.path.join(config.get('event_output_dir', './events'), 'event_log.csv')
    event_log_arg = getattr(args, 'event_log', None)
    event_log_path = default_event_log if (not event_log_arg or event_log_arg == 'auto') else event_log_arg
    uploader = None
    if getattr(args, 'api_url', None):
        queue_db = Path(config.get('event_output_dir', './events')) / 'upload_queue.db'
        uploader = EventUploader(args.api_url, getattr(args, 'api_token', None), queue_path=queue_db)
    capture_mode = getattr(args, 'capture_mode', 'path')
    is_detour = getattr(args, 'is_detour', False)
    event_manager = EventManager(config, fps, (width, height), zone_mgr, event_log_path, uploader=uploader,
                                 capture_mode=capture_mode, is_detour=is_detour)
    if args.csv or output_dir:
        csv_path = args.csv
        if csv_path and os.path.isdir(csv_path):
            csv_path = os.path.join(csv_path, Path(path).stem + '.csv')
        if not csv_path and output_dir:
            csv_path = os.path.join(output_dir, Path(path).stem + '.csv')
        csv_dir = os.path.dirname(csv_path)
        if csv_dir:
            os.makedirs(csv_dir, exist_ok=True)
        csv_f = open(csv_path, 'w', newline='', encoding='utf-8')
        csv_writer = csv.writer(csv_f)
        csv_writer.writerow(['frame', 'class', 'score', 'x1', 'y1', 'x2', 'y2', 'track_id', 'text', 'raw_text'])

    debug_overlay_flag = bool(logic_cfg.get('debug_overlay', False))
    debug_tracks_cfg = bool(logic_cfg.get('debug_track_state', False))
    debug_anchor_points = bool(logic_cfg.get('debug_anchor_points', False) or debug_overlay_flag)
    debug_water_boxes = bool(logic_cfg.get('debug_water_boxes', False) or debug_overlay_flag)
    debug_rois = getattr(args, 'debug_rois', False) or debug_overlay_flag
    debug_tracks = getattr(args, 'debug_tracks', False) or debug_tracks_cfg or debug_overlay_flag

    debug_frame_path_conf = str(video_cfg.get('debug_frame_path', '') or '').strip()
    debug_frame_file = None
    if not debug_frame_path_conf:
        shm = Path('/dev/shm')
        if shm.exists() and os.access(shm, os.W_OK):
            debug_frame_path_conf = str(shm / 'cleaningcar_debug.jpg')
    if debug_frame_path_conf:
        debug_frame_file = _resolve_runtime_path(debug_frame_path_conf, base_dir)
        if debug_frame_file.is_dir():
            debug_frame_file = debug_frame_file / 'latest.jpg'
        try:
            debug_frame_file.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    debug_frame_interval = max(1, int(video_cfg.get('debug_frame_interval', 30)))

    task_q = Queue(maxsize=args.queue_size)
    result_q = Queue()
    core_mask = parse_core_mask(args.core_mask)
    workers = [DetectWorker(i, args, core_mask, task_q, result_q, detect_mask) for i in range(args.workers)]
    for w in workers:
        w.start()

    monitor_stop = threading.Event()
    monitor_thread = None
    if args.monitor_interval > 0:
        monitor_thread = threading.Thread(target=monitor_loop, args=(args.monitor_interval, monitor_stop), daemon=True)
        monitor_thread.start()

    start = time.time()
    total_frames = 0
    next_frame_to_write = 0
    pending = {}
    finished_workers = 0
    reader_log_interval = float(config.get('reader_fps_log_interval', 10.0))
    reader_log_last_time = start
    reader_log_frames = 0
    worker_last_frames = [0 for _ in workers]
    worker_last_infer = [0.0 for _ in workers]

    car_plate_cache = {}
    car_plate_cache_ttl = int(config.get('car_plate_cache_ttl', CAR_PLATE_CACHE_TTL))
    alias_confirm = {}
    alias_timeout = int(config.get('track_timeout_frames', 60))
    enable_per_id_video = bool(logic_cfg.get('enable_per_id_video', False))
    per_id_video_dir = Path(logic_cfg.get('per_id_video_dir', './video_result/per_id'))
    per_id_video_dir.mkdir(parents=True, exist_ok=True)
    per_id_writers = {}
    per_id_downscale_ratio = float(logic_cfg.get('per_id_downscale_ratio', 1.0) or 1.0)
    if per_id_downscale_ratio <= 0.0:
        per_id_downscale_ratio = 1.0
    per_id_target_width = width
    per_id_target_height = height
    if per_id_downscale_ratio < 0.999:
        per_id_target_width = max(1, int(width * per_id_downscale_ratio))
        per_id_target_height = max(1, int(height * per_id_downscale_ratio))
    target_w = int(logic_cfg.get('per_id_target_width', 1920) or 1920)
    target_h = int(logic_cfg.get('per_id_target_height', 1080) or 1080)
    per_id_target_width = target_w
    per_id_target_height = target_h
    per_id_output_fps = float(logic_cfg.get('per_id_fps', 20.0) or 20.0)
    per_id_frame_stride = int(logic_cfg.get('per_id_frame_stride', 1) or 1)
    if per_id_frame_stride < 1:
        per_id_frame_stride = 1

    def close_per_id_writer(track_id, track_state):
        writer = per_id_writers.pop(track_id, None)
        if writer is None:
            return
        finalized = False
        try:
            finalized = bool(writer.release())
        except Exception:
            finalized = False
        if not finalized:
            return
        if not track_state:
            track_state = {}
        frame_idx = track_state.get('record_stop_frame')
        if frame_idx is None:
            frame_idx = track_state.get('last_frame_idx', 0)
        frame = track_state.get('last_frame')
        try:
            event_manager.emit_event(track_id, 6, frame_idx, frame, {}, track_state)
        except Exception:
            return

    def finalize_per_id_for_track(track_id, track_state):
        close_per_id_writer(track_id, track_state)

    def mark_alias_confirm(alias_id, has_plate_text, frame_idx, require_text):
        if alias_id <= 0:
            return False
        state = alias_confirm.setdefault(alias_id, {'frames': 0, 'confirmed': False, 'last_seen': frame_idx})
        state['frames'] += 1
        state['last_seen'] = frame_idx
        if not state['confirmed']:
            if has_plate_text:
                state['confirmed'] = True
            elif not require_text and state['frames'] >= REPORT_MIN_FRAMES:
                state['confirmed'] = True
        return state['confirmed']

    def cleanup_alias_confirm(frame_idx):
        for aid in list(alias_confirm.keys()):
            state = alias_confirm[aid]
            if (frame_idx - state.get('last_seen', frame_idx)) > alias_timeout or aid not in event_manager.tracks:
                alias_confirm.pop(aid, None)

    def annotate_locked_label(track_id, det_ref, rows_ref, frame_img):
        if det_ref is None or det_ref.get('cls') not in VEHICLE_CLASS_IDS:
            return
        locked = event_manager.get_locked_vehicle(track_id)
        row_idx = det_ref.get('row_idx', -1)
        if row_idx is not None and 0 <= row_idx < len(rows_ref):
            if locked:
                rows_ref[row_idx][1] = locked
        label_now = det_ref.get('label')
        mismatch = bool(locked and label_now and locked != label_now)
        if mismatch:
            return
        if not args.no_draw and frame_img is not None:
            x1, y1, x2, y2 = det_ref['box']
            color = select_box_color(label_now or locked or '')
            cv2.rectangle(frame_img, (x1, y1), (x2, y2), color, 2)
            disp_label = locked or label_now or ''
            if disp_label:
                text_cn = localize_vehicle(disp_label)
                cv2.putText(frame_img, text_cn, (x1, max(0, y1 - 18)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 2, cv2.LINE_AA)
            if debug_anchor_points:
                anchor_pt = anchor_point_for(det_ref['box'])
                if anchor_pt:
                    ax, ay = int(anchor_pt[0]), int(anchor_pt[1])
                    cv2.circle(frame_img, (ax, ay), 4, (255, 140, 0), -1)
                    cv2.putText(frame_img, f'A{track_id}', (ax + 4, ay - 4),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)

    def drain_results(block=True):
        nonlocal next_frame_to_write, finished_workers, car_plate_cache, per_id_writers, enable_per_id_video
        try:
            item = result_q.get(block=block, timeout=1 if block else 0)
        except Exception:
            return False
        if item is None:
            finished_workers += 1
        else:
            if len(item) == 4:
                idx, frame_out, rows, det_payload = item
                capture_ts = None
            else:
                idx, capture_ts, frame_out, rows, det_payload = item
            pending[idx] = (frame_out, rows, det_payload, capture_ts)
            while next_frame_to_write in pending:
                frame_out, rows, det_payload, capture_ts = pending.pop(next_frame_to_write)
                if capture_ts is not None:
                    try:
                        event_manager.record_frame_timing(next_frame_to_write, capture_ts, time.time())
                    except Exception:
                        pass
                vehicle_dets = []
                vehicle_payload_refs = []
                car_boxes = {}
                water_boxes = []
                cleaning_label = ''
                if det_payload:
                    for det in det_payload:
                        if det.get('cls') in VEHICLE_CLASS_IDS:
                            vehicle_dets.append({'box': det['box'], 'score': det['score'], 'cls': det['cls'], 'row_idx': det.get('row_idx')})
                            vehicle_payload_refs.append(det)
                        elif det.get('cls') in WATER_CLASS_IDS:
                            water_boxes.append(det['box'])
                            name = CLASS_NAMES[det['cls']]
                            if name == 'manual':
                                cleaning_label = 'manual'
                            elif not cleaning_label:
                                cleaning_label = name
                if debug_water_boxes and not args.no_draw and water_boxes and frame_out is not None:
                    for wb in water_boxes:
                        wx1, wy1, wx2, wy2 = wb
                        cv2.rectangle(frame_out, (wx1, wy1), (wx2, wy2), CLASS_COLORS.get('water', (0, 160, 255)), 2)
                        cv2.putText(frame_out, 'Water', (wx1, max(0, wy1 - 6)),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 160, 255), 1, cv2.LINE_AA)
                assignments = vehicle_tracker.update(next_frame_to_write, vehicle_dets) if vehicle_dets else []
                for det_ref, track_id in zip(vehicle_payload_refs, assignments):
                    det_ref['track_id'] = track_id
                    car_boxes[track_id] = det_ref['box']
                    row_idx = det_ref.get('row_idx', -1)
                    if row_idx is not None and 0 <= row_idx < len(rows):
                        rows[row_idx][7] = track_id
                    if track_id > 0 and video_writer:
                        x1, y1, _, _ = det_ref['box']
                        cv2.putText(frame_out, f'CID:{track_id}', (x1, y1 - 5),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 0), 1, cv2.LINE_AA)
                license_dets = [d for d in det_payload if d.get('cls') == LICENSE_CLASS] if det_payload else []
                if license_dets and car_boxes:
                    for det in license_dets:
                        plate_box = det['box']
                        px1, py1, px2, py2 = plate_box
                        pcenter = ((px1 + px2) * 0.5, (py1 + py2) * 0.5)
                        best_id = None
                        best_score = 0.0
                        for car_id, cbox in car_boxes.items():
                            score = box_iou(plate_box, cbox)
                            if point_in_box(pcenter, cbox):
                                score = max(score, 1.0)
                            if score > best_score:
                                best_score = score
                                best_id = car_id
                        if best_id is not None and (best_score >= PLATE_CAR_LINK_IOU or point_in_box(pcenter, car_boxes[best_id])):
                            det['car_track'] = best_id
                            det['vehicle_box'] = car_boxes[best_id]
                car_to_plate = {}
                plate_to_car = {}
                plate_track_info = {}
                updates = plate_tracker.update(next_frame_to_write, license_dets)
                for det, upd in zip(license_dets, updates):
                    plate_id = upd.get('track_id', -1)
                    text_val = upd.get('text', '')
                    is_guess = bool(upd.get('is_guess', False))
                    row_idx = det.get('row_idx', -1)
                    if row_idx is not None and 0 <= row_idx < len(rows):
                        rows[row_idx][-1] = det.get('text', '')
                        if text_val:
                            rows[row_idx][-2] = text_val
                        track_val = det.get('car_track', -1)
                        rows[row_idx][7] = track_val if track_val > 0 else plate_id
                    if plate_id > 0:
                        plate_track_info[plate_id] = {
                            'box': det['box'],
                            'text': text_val,
                            'is_guess': is_guess,
                            'vehicle_box': det.get('vehicle_box'),
                            'score': float(det.get('score', 0.0)),
                        }
                        car_id = det.get('car_track', -1)
                        if car_id > 0:
                            car_to_plate[car_id] = plate_id
                            plate_to_car[plate_id] = car_id
                        if car_id > 0 and det.get('vehicle_box') is not None:
                            car_boxes.setdefault(car_id, det['vehicle_box'])
                        if car_id > 0:
                            car_plate_cache[car_id] = {'plate_id': plate_id, 'age': 0}
                        if video_writer and text_val:
                            x1, y1, _, _ = det['box']
                            cv2.putText(frame_out, f'PID:{plate_id} {text_val}', (x1, y1 - 10),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1, cv2.LINE_AA)
                alias_seen = set()
                plates_with_updates = set()
                for plate_id, info in plate_track_info.items():
                    car_id = plate_to_car.get(plate_id, -1)
                    track_key = car_id if car_id > 0 else plate_id
                    vehicle_box = info.get('vehicle_box')
                    if vehicle_box is None:
                        if car_id > 0 and car_id in car_boxes:
                            vehicle_box = car_boxes[car_id]
                        else:
                            for cid, p_id in car_to_plate.items():
                                if p_id == plate_id and cid in car_boxes:
                                    vehicle_box = car_boxes[cid]
                                    break
                    anchor_pt = anchor_point_for(vehicle_box or info['box'])
                    confirmed_alias = mark_alias_confirm(track_key, bool(info.get('text')), next_frame_to_write, True)
                    event_manager.update_track(
                        track_key,
                        info['box'],
                        vehicle_box,
                        info.get('text', ''),
                        next_frame_to_write,
                        frame_out,
                        water_boxes,
                        bool(water_boxes),
                        is_plate=True,
                        vehicle_label=None,
                        vehicle_conf=None,
                        plate_conf=info.get('score'),
                        confirmed=confirmed_alias,
                        cleaning_label=cleaning_label,
                        anchor_point=anchor_pt,
                        plate_is_guess=bool(info.get('is_guess', False)),
                    )
                    alias_seen.add(track_key)
                    plates_with_updates.add(track_key)
                active_car_ids = set()
                for det_ref in vehicle_payload_refs:
                    car_id = det_ref.get('track_id', -1)
                    if car_id <= 0:
                        continue
                    active_car_ids.add(car_id)
                    alias_plate_id = car_to_plate.get(car_id)
                    if alias_plate_id:
                        row_idx = det_ref.get('row_idx', -1)
                        if row_idx is not None and 0 <= row_idx < len(rows):
                            rows[row_idx][7] = car_id
                        car_plate_cache[car_id] = {'plate_id': alias_plate_id, 'age': 0}
                        if car_id not in plates_with_updates:
                            known_text = ''
                            track_state = event_manager.tracks.get(car_id)
                            if track_state:
                                known_text = track_state.get('plate_text', '')
                            confirmed_alias = mark_alias_confirm(car_id, bool(known_text), next_frame_to_write, True)
                            anchor_pt = anchor_point_for(det_ref['box'])
                            event_manager.update_track(
                                car_id,
                                None,
                                det_ref['box'],
                                '',
                                next_frame_to_write,
                                frame_out,
                                water_boxes,
                                bool(water_boxes),
                                is_plate=True,
                                vehicle_label=det_ref.get('label', ''),
                                vehicle_conf=det_ref.get('score'),
                                plate_conf=None,
                                confirmed=confirmed_alias,
                                cleaning_label=cleaning_label,
                                anchor_point=anchor_pt,
                            )
                        annotate_locked_label(car_id, det_ref, rows, frame_out)
                        alias_seen.add(car_id)
                        continue
                    cache_entry = car_plate_cache.get(car_id)
                    if cache_entry and cache_entry.get('age', 0) <= car_plate_cache_ttl:
                        cache_entry['age'] = cache_entry.get('age', 0) + 1
                        row_idx = det_ref.get('row_idx', -1)
                        if row_idx is not None and 0 <= row_idx < len(rows):
                            rows[row_idx][7] = car_id
                        known_text = ''
                        track_state = event_manager.tracks.get(car_id)
                        if track_state:
                            known_text = track_state.get('plate_text', '')
                        confirmed_alias = mark_alias_confirm(car_id, bool(known_text), next_frame_to_write, True)
                        anchor_pt = anchor_point_for(det_ref['box'])
                        event_manager.update_track(
                            car_id,
                            None,
                            det_ref['box'],
                            '',
                            next_frame_to_write,
                            frame_out,
                            water_boxes,
                            bool(water_boxes),
                            is_plate=True,
                            vehicle_label=det_ref.get('label', ''),
                            vehicle_conf=det_ref.get('score'),
                            plate_conf=None,
                            confirmed=confirmed_alias,
                            cleaning_label=cleaning_label,
                            anchor_point=anchor_pt,
                        )
                        alias_seen.add(car_id)
                        continue
                    fallback_id = car_id
                    row_idx = det_ref.get('row_idx', -1)
                    if row_idx is not None and 0 <= row_idx < len(rows):
                        rows[row_idx][7] = fallback_id
                    confirmed_alias = mark_alias_confirm(fallback_id, False, next_frame_to_write, True)
                    anchor_pt = anchor_point_for(det_ref['box'])
                    event_manager.update_track(
                        fallback_id,
                        None,
                        det_ref['box'],
                        '',
                        next_frame_to_write,
                        frame_out,
                        water_boxes,
                        bool(water_boxes),
                        is_plate=False,
                        vehicle_label=det_ref.get('label', ''),
                        vehicle_conf=det_ref.get('score'),
                        plate_conf=None,
                        confirmed=confirmed_alias,
                        cleaning_label=cleaning_label,
                        anchor_point=anchor_pt,
                    )
                    annotate_locked_label(fallback_id, det_ref, rows, frame_out)
                    alias_seen.add(fallback_id)
                for car_id in list(car_plate_cache.keys()):
                    if car_id in active_car_ids:
                        continue
                    car_plate_cache[car_id]['age'] = car_plate_cache[car_id].get('age', 0) + 1
                    if car_plate_cache[car_id]['age'] > car_plate_cache_ttl:
                        car_plate_cache.pop(car_id, None)
                if debug_tracks:
                    for det_ref in vehicle_payload_refs:
                        car_id = det_ref.get('track_id', -1)
                        if car_id <= 0:
                            continue
                        info = event_manager.get_track_debug(car_id)
                        if not info:
                            continue
                        x1, y1, _, _ = det_ref['box']
                        text = (f"ID:{car_id} {info['state']} sf:{info['stationary']} "
                                f"spd:{info['speed']:.1f} water:{'Y' if info['water'] else 'N'} "
                                f"dur:{info['wash_duration']:.1f} zb:{info.get('zone_b_elapsed',0)}")
                        cv2.putText(frame_out, text, (x1, max(0, y1 - 25)),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 200, 255), 1, cv2.LINE_AA)
                if debug_rois:
                    if len(zone_a_pts) >= 3:
                        roi_np = np.array(zone_a_pts, dtype=np.int32)
                        cv2.polylines(frame_out, [roi_np], True, (0, 200, 0), 2, cv2.LINE_AA)
                        anchor = tuple(map(int, roi_np[0]))
                        cv2.putText(frame_out, 'Zone A', (anchor[0], max(0, anchor[1] - 8)),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 0), 2, cv2.LINE_AA)
                    if len(zone_b_pts) >= 3:
                        roi_np = np.array(zone_b_pts, dtype=np.int32)
                        cv2.polylines(frame_out, [roi_np], True, (0, 0, 255), 2, cv2.LINE_AA)
                        anchor = tuple(map(int, roi_np[0]))
                        cv2.putText(frame_out, 'Zone B', (anchor[0], max(0, anchor[1] - 8)),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2, cv2.LINE_AA)
                    flow_start_int = tuple(map(int, flow_start))
                    flow_end_int = tuple(map(int, flow_end))
                    cv2.arrowedLine(frame_out, flow_start_int, flow_end_int, (255, 0, 0), 2, tipLength=0.08)
                if debug_frame_file and frame_out is not None and (next_frame_to_write % debug_frame_interval == 0):
                    try:
                        ts_now = datetime.now()
                        ts_str = ts_now.strftime("%Y-%m-%d %H:%M:%S")
                        latency_ms = None
                        if capture_ts is not None:
                            try:
                                latency_ms = int((ts_now.timestamp() - float(capture_ts)) * 1000.0)
                            except Exception:
                                latency_ms = None
                        text = ts_str
                        if latency_ms is not None and latency_ms >= 0:
                            text = f"{ts_str} Δ{latency_ms}ms"
                        h_dbg, w_dbg = frame_out.shape[:2]
                        margin = 10
                        base = min(w_dbg, h_dbg)
                        scale = max(0.5, base / 960.0 * 0.7)
                        thick_outline = max(2, int(scale * 3))
                        thick_text = max(1, int(scale * 1.5))
                        cv2.putText(
                            frame_out,
                            text,
                            (margin, h_dbg - margin),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            scale,
                            (0, 0, 0),
                            thick_outline,
                            cv2.LINE_AA,
                        )
                        cv2.putText(
                            frame_out,
                            text,
                            (margin, h_dbg - margin),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            scale,
                            (255, 255, 255),
                            thick_text,
                            cv2.LINE_AA,
                        )
                        cv2.imwrite(str(debug_frame_file), frame_out)
                    except Exception:
                        pass
                if enable_per_id_video and frame_out is not None:
                    for tid, st in event_manager.tracks.items():
                        start_f = st.get('record_start_frame')
                        stop_f = st.get('record_stop_frame')
                        if start_f is None:
                            continue
                        if next_frame_to_write < start_f:
                            continue
                        # Only create the per-id video file after type2 (meaningful record) has been emitted.
                        # This avoids generating lots of short clips for spurious IDs.
                        if 2 not in (st.get('events') or set()):
                            continue
                        writer = per_id_writers.get(tid)
                        if writer is None:
                            st_capture_time = st.get('type1_capture_time')
                            if not st_capture_time:
                                for ev_type in (5, 4, 3, 2, 1):
                                    ev_key = f'last_event_t{ev_type}_capture_time'
                                    val = st.get(ev_key)
                                    if val:
                                        st_capture_time = val
                                        break
                            if not st_capture_time:
                                st_capture_time = event_manager.frame_timestamp(start_f)
                            try:
                                dt = datetime.strptime(st_capture_time, "%Y-%m-%d %H:%M:%S")
                            except Exception:
                                dt = datetime.now()
                            session_id = st.get('session_id')
                            if not session_id:
                                ts_str = dt.strftime("%Y%m%d%H%M")
                                device_name = config.get('system', {}).get('device_id') or event_manager.camera_id
                                session_id = f"{device_name}-{ts_str}-{tid}"
                            fname = f"{session_id}.mp4"
                            date_dir = dt.strftime('%Y%m%d')
                            hour_dir = dt.strftime('%H')
                            base_dir = per_id_video_dir / date_dir / hour_dir
                            base_dir.mkdir(parents=True, exist_ok=True)
                            path = base_dir / fname
                            writer_obj = FfmpegH264Writer(str(path), per_id_target_width, per_id_target_height, per_id_output_fps)
                            if writer_obj.is_opened():
                                per_id_writers[tid] = writer_obj
                                writer = writer_obj
                            else:
                                print(f"[per-id-video] H.264 writer init failed, per-id video disabled for this run: {path}")
                                enable_per_id_video = False
                                writer = None
                                break
                        if writer is not None:
                            frame_to_write = frame_out
                            if frame_to_write is not None:
                                h, w = frame_to_write.shape[:2]
                                if w != per_id_target_width or h != per_id_target_height:
                                    frame_to_write = cv2.resize(frame_to_write, (per_id_target_width, per_id_target_height))
                                if next_frame_to_write % per_id_frame_stride == 0:
                                    writer.write(frame_to_write)
                if csv_writer and rows:
                    csv_writer.writerows(rows)
                next_frame_to_write += 1
                event_manager.flush_inactive(alias_seen, next_frame_to_write, finalize_per_id_for_track)
                cleanup_alias_confirm(next_frame_to_write)
            return True

    frame_limit = args.limit if args.limit and args.limit > 0 else None

    while True:
        if frame_limit is not None and total_frames >= frame_limit:
            break
        ret, frame = cap.read()
        if not ret or frame is None:
            consecutive_fails += 1
            if is_file_input:
                print('[reader] local file reached EOF or failed, stopping.')
                break
            if consecutive_fails < reader_fail_threshold:
                time.sleep(0.05)
                continue
            reconnect_count += 1
            consecutive_fails = 0
            print(f'[reader] capture stalled, reconnect attempt #{reconnect_count}')
            try:
                cap.release()
            except Exception:
                pass
            time.sleep(reader_reconnect_delay)
            cap = create_video_reader(path, args)
            if cap and hasattr(cap, 'isOpened') and cap.isOpened():
                continue
            print('[reader] reconnect failed.')
            if reader_max_reconnect and reconnect_count >= reader_max_reconnect:
                print('[reader] max reconnect attempts reached, aborting stream.')
                break
            time.sleep(reader_reconnect_delay)
            continue
        consecutive_fails = 0
        capture_ts = time.time()
        task_q.put((total_frames, frame, capture_ts))
        total_frames += 1
        reader_log_frames += 1
        now = time.time()
        if reader_log_interval > 0 and now - reader_log_last_time >= reader_log_interval:
            elapsed_window = now - reader_log_last_time
            decode_fps_window = reader_log_frames / max(elapsed_window, 1e-6)
            pipeline_frames_window = 0
            worker_msgs = []
            for i, w in enumerate(workers):
                frames_delta = max(0, w.frames - worker_last_frames[i])
                infer_delta = max(0.0, w.infer_time - worker_last_infer[i])
                worker_last_frames[i] = w.frames
                worker_last_infer[i] = w.infer_time
                if frames_delta > 0 and elapsed_window > 0:
                    worker_fps = frames_delta / elapsed_window
                else:
                    worker_fps = 0.0
                if frames_delta > 0 and infer_delta > 0.0:
                    infer_ms = infer_delta * 1000.0 / frames_delta
                else:
                    infer_ms = 0.0
                pipeline_frames_window += frames_delta
                worker_msgs.append(f'w{i}:{worker_fps:.2f}fps/{infer_ms:.1f}ms')
            pipeline_fps_window = pipeline_frames_window / max(elapsed_window, 1e-6) if pipeline_frames_window > 0 else 0.0
            workers_str = ', '.join(worker_msgs)
            print(f'[perf] 解码FPS={decode_fps_window:.2f} 管线FPS={pipeline_fps_window:.2f} 窗口={elapsed_window:.1f}s 总帧数={total_frames} 工人[{workers_str}]')
            reader_log_frames = 0
            reader_log_last_time = now
        while result_q.qsize() > args.queue_size // 2:
            drain_results(block=False)

    for _ in workers:
        task_q.put(None)
    task_q.join()
    while finished_workers < len(workers):
        drain_results(block=True)

    if enable_per_id_video and per_id_writers:
        for tid in list(per_id_writers.keys()):
            track_state = event_manager.tracks.get(tid) or {}
            close_per_id_writer(tid, track_state)

    if video_writer:
        video_writer.release()
    if csv_f:
        csv_f.close()
    cap.release()
    monitor_stop.set()
    if monitor_thread:
        monitor_thread.join(timeout=0.5)
    event_manager.flush_inactive(set(), total_frames + int(config.get('track_timeout_frames', 60)) + 1, finalize_per_id_for_track)
    cleanup_alias_confirm(total_frames + alias_timeout + 1)
    if uploader:
        uploader.close()

    elapsed = time.time() - start
    if total_frames:
        print(f'Video {path}: frames={total_frames} elapsed={elapsed:.2f}s ({total_frames/elapsed:.2f} FPS)')
    agg_frames = sum(w.frames for w in workers)
    agg_infer = sum(w.infer_time for w in workers)
    if agg_frames:
        print(f'Average inference {agg_infer/agg_frames*1000:.2f} ms over {agg_frames} frames')
    for w in workers:
        if w.frames:
            rate = w.frames / elapsed
            print(f'Worker {w.idx}: frames={w.frames} infer_ms={w.infer_time*1000/w.frames:.2f} throughput={rate:.2f} FPS')


def iter_videos(args):
    if args.video:
        yield args.video
    if args.video_dir:
        for name in sorted(os.listdir(args.video_dir)):
            if name.lower().endswith(('.mp4', '.mov', '.avi', '.mkv')):
                yield os.path.join(args.video_dir, name)


def main():
    args = parse_args()
    config_path = getattr(args, 'config', None)
    config_dir = None
    if config_path:
        try:
            config_dir = Path(config_path).expanduser().resolve().parent
        except Exception:
            config_dir = Path.cwd()
    try:
        config = load_config(config_path)
    except ValueError as exc:
        print(exc)
        return
    apply_cli_overrides(args, config)
    apply_class_thresholds_from_config(config)
    setattr(args, '_config', config)
    setattr(args, '_config_dir', config_dir or Path.cwd())
    videos = list(iter_videos(args))
    if not videos:
        print('No videos specified.')
        return
    disk_cleaner = None
    storage_cfg = config.get('storage', {}) or {}
    disk_dirs = _collect_storage_directories(config, config_dir) if ENABLE_DISK_CLEANER else []
    if disk_dirs:
        try:
            threshold = float(storage_cfg.get('disk_threshold', 65.0))
            target = float(storage_cfg.get('disk_target', max(5.0, threshold - 10.0)))
            if target >= threshold:
                target = max(5.0, threshold - 10.0)
            interval = int(storage_cfg.get('clean_interval_seconds', 600))
            root_candidate = disk_dirs[0]
            root = Path(root_candidate.anchor or str(root_candidate))
            disk_cleaner = DiskCleaner(root, disk_dirs, threshold=threshold, target=target,
                                       interval_seconds=max(60, interval))
            disk_cleaner.start()
        except Exception as exc:
            disk_cleaner = None
            print(f'[disk-cleaner] init failed: {exc}')
    try:
        for path in videos:
            process_video(path, args)
    finally:
        if disk_cleaner:
            disk_cleaner.stop()


if __name__ == '__main__':
    main()
