import json
from pathlib import Path
from typing import Any, Dict, List, Tuple


class ConfigError(Exception):
    """Raised when config.json is missing or invalid."""


def _ensure_polygon(points: List[List[float]]) -> List[Tuple[float, float]]:
    if not isinstance(points, list) or len(points) < 3:
        raise ConfigError('Polygon requires at least 3 points')
    poly = []
    for item in points:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ConfigError(f'Invalid point format: {item}')
        poly.append((float(item[0]), float(item[1])))
    return poly


def _ensure_point(point: List[float]) -> Tuple[float, float]:
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        raise ConfigError(f'Invalid point: {point}')
    return float(point[0]), float(point[1])


class ConfigManager:
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.exists():
            raise ConfigError(f'config file not found: {self.path}')
        with self.path.open('r', encoding='utf-8') as f:
            self.data: Dict[str, Any] = json.load(f)
        self._validate()

    def _validate(self):
        system = self.data.setdefault('system', {})
        system.setdefault('device_id', 'RK3588')
        system.setdefault('monitor_interval', 2.0)
        system.setdefault('api', {})
        system['api'].setdefault('url', '')
        system['api'].setdefault('token', '')
        system['api'].setdefault('capture_mode', 'path')
        system.setdefault('metrics_path', '/dev/shm/cleaningcar_metrics.json')
        system.setdefault('cpu_mask', '')

        video = self.data.setdefault('video', {})
        if 'source' not in video:
            raise ConfigError('video.source missing')
        video.setdefault('source_mode', 'auto')
        video.setdefault('hw_decode', False)
        video.setdefault('workers', 2)
        video.setdefault('core_mask', '0-2')
        video.setdefault('save_video', '')
        video.setdefault('csv', '')
        video.setdefault('debug_frame_path', '/dev/shm/cleaningcar_debug.jpg')
        video.setdefault('debug_frame_interval', 30)
        video.setdefault('debug_frame_max_width', 960)
        video.setdefault('debug_frame_quality', 80)
        try:
            segment_minutes = int(video.get('segment_minutes', 60))
        except (TypeError, ValueError):
            segment_minutes = 0
        video['segment_minutes'] = max(0, segment_minutes)

        zones = self.data.setdefault('zones', {})
        zones['zone_a_detection'] = _ensure_polygon(zones.get('zone_a_detection', []))
        zones['zone_b_wash'] = _ensure_polygon(zones.get('zone_b_wash', []))
        flow_vec = zones.get('flow_vector', {})
        zones['flow_vector'] = {
            'start': _ensure_point(flow_vec.get('start', (0.0, 0.0))),
            'end': _ensure_point(flow_vec.get('end', (0.0, 1.0)))
        }

        logic = self.data.setdefault('logic', {})
        logic.setdefault('detection_anchor', 'bottom_center')
        logic.setdefault('zone_a_mask_enable', True)
        logic.setdefault('zone_b_entry_hysteresis', 3)
        logic.setdefault('zone_b_exit_hysteresis', 3)
        logic.setdefault('stationary_min_frames', 0)
        logic.setdefault('stationary_speed_thresh', 8.0)
        logic.setdefault('type34_min_interval_frames', 5)
        logic.setdefault('track_timeout_frames', 90)
        logic.setdefault('track_max_age', 120)
        logic.setdefault('vehicle_iou_threshold', 0.3)
        logic.setdefault('vehicle_center_gate_ratio', 0.0)
        logic['disable_plate_only_events'] = True
        logic['single_lifecycle_events'] = True
        logic.setdefault('min_zone_a_dwell_frames_for_type5', 25)
        logic.setdefault('min_track_frames_for_type1', 5)
        logic.setdefault('no_draw', False)
        logic.setdefault('require_vehicle_type_for_events', False)
        logic.setdefault('lane_name', '冲洗')
        logic.setdefault('vehicle_shrink_ratio', 0.35)
        logic.setdefault('vehicle_lock_min_votes', 40)
        logic.setdefault('vehicle_lock_on_confirm', True)
        logic.setdefault('plate_lock_frames', 6)
        logic.setdefault('default_plate_color', '')
        logic.setdefault('default_plate_color_conf', 0.0)
        logic.setdefault('default_cleanliness', 0)
        logic.setdefault('car_plate_cache_ttl', 60)
        logic.setdefault('allowed_event_types', [1, 2, 3, 4, 5, 6])
        logic.setdefault('anchor_offset_ratio', 0.0)
        logic.setdefault('zone_b_anchor_min_frames', 0)
        logic.setdefault('debug_overlay', False)
        logic.setdefault('debug_track_state', False)
        logic.setdefault('debug_anchor_points', False)
        logic.setdefault('debug_water_boxes', False)
        logic.setdefault('wash_duration_offset_seconds', 0.0)
        logic.setdefault('min_zone_b_dwell_frames_for_type4', 60)
        logic.setdefault('enable_global_video', False)
        logic.setdefault('enable_per_id_video', True)
        logic.setdefault('per_id_video_dir', './video_result/per_id')
        logic.setdefault('per_id_downscale_ratio', 1.0)
        logic.setdefault('per_id_frame_stride', 1)
        logic.setdefault('per_id_auto_adapt', False)
        logic.setdefault('per_id_auto_cpu_high', 75.0)
        logic.setdefault('per_id_auto_cpu_low', 50.0)
        logic.setdefault('per_id_max_frame_stride', 3)
        logic.setdefault('enable_event_disk', False)
        shadow = logic.setdefault('shadow_plate_pool', {})
        shadow.setdefault('max_candidates', 50)
        shadow.setdefault('max_age_frames', 120)

        storage = self.data.setdefault('storage', {})
        storage.setdefault('disk_threshold', 65.0)
        storage.setdefault('disk_target', 50.0)
        storage.setdefault('clean_interval_seconds', 600)

        self.data.setdefault('event_capture_quality', 85)

        cfg_name = self.path.stem or 'default'
        default_events = f'events/{cfg_name}'
        default_captures = f'captures/{cfg_name}'
        self.data.setdefault('event_output_dir', default_events)
        self.data.setdefault('event_capture_dir', default_captures)

    def save(self):
        with self.path.open('w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    @property
    def zones(self):
        return self.data['zones']

    @property
    def logic(self):
        return self.data['logic']

    @property
    def video(self):
        return self.data['video']

    @property
    def system(self):
        return self.data['system']

    @property
    def storage(self):
        return self.data['storage']
