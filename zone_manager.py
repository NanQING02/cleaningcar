from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

import cv2
import numpy as np


def point_in_polygon(point: Tuple[float, float], polygon: Sequence[Tuple[float, float]]) -> bool:
    if len(polygon) < 3:
        return False
    x, y = point
    contour = np.array(polygon, dtype=np.float32)
    return cv2.pointPolygonTest(contour, (float(x), float(y)), False) >= 0


def polygon_mask(polygon: Sequence[Tuple[float, float]], size: Tuple[int, int]):
    mask = np.zeros(size, dtype=np.uint8)
    pts = np.array(polygon, dtype=np.int32)
    cv2.fillPoly(mask, [pts], 255)
    return mask


@dataclass
class ZoneState:
    inside_a: bool = False
    inside_b: bool = False
    enter_ratio: float = 0.0
    exit_ratio: float = 0.0
    entry_point: Tuple[float, float] = (0.0, 0.0)
    exit_point: Tuple[float, float] = (0.0, 0.0)
    last_anchor: Tuple[float, float] = (0.0, 0.0)


@dataclass
class ZoneManager:
    zone_a: List[Tuple[float, float]]
    zone_b: List[Tuple[float, float]]
    flow_vector: Tuple[Tuple[float, float], Tuple[float, float]]
    entry_hysteresis: int = 3
    exit_hysteresis: int = 3
    state_cache: dict = field(default_factory=dict)

    def __post_init__(self):
        ratios = []
        for pt in self.zone_a:
            ratios.append(self._relative_position(pt))
        if ratios:
            lo = min(ratios)
            hi = max(ratios)
            if hi - lo < 1e-6:
                self._zone_a_min = 0.25
                self._zone_a_max = 0.75
            else:
                self._zone_a_min = lo
                self._zone_a_max = hi
        else:
            self._zone_a_min = 0.25
            self._zone_a_max = 0.75

    def update_track(self, track_id: int, anchor_point: Tuple[float, float], frame_idx: int):
        st = self.state_cache.setdefault(track_id, {
            'state': ZoneState(),
            'entry_counter': 0,
            'exit_counter': 0,
            'last_anchor': anchor_point,
            'last_frame': frame_idx,
        })
        st['last_anchor'] = anchor_point
        st['last_frame'] = frame_idx
        state: ZoneState = st['state']
        state.last_anchor = anchor_point

        inside_a = point_in_polygon(anchor_point, self.zone_a)
        inside_b = point_in_polygon(anchor_point, self.zone_b)

        enter_a = inside_a and not state.inside_a
        exit_a = (not inside_a) and state.inside_a
        if enter_a:
            state.enter_ratio = self._relative_position(anchor_point)
            state.entry_point = anchor_point
        if exit_a:
            state.exit_ratio = self._relative_position(anchor_point)
            state.exit_point = anchor_point

        st['entry_counter'] = min(self.entry_hysteresis, st['entry_counter'] + 1) if inside_b else 0
        st['exit_counter'] = min(self.exit_hysteresis, st['exit_counter'] + 1) if (not inside_b) else 0

        enter_b = False
        exit_b = False
        if st['entry_counter'] >= self.entry_hysteresis and not state.inside_b:
            state.inside_b = True
            enter_b = True
        elif st['exit_counter'] >= self.exit_hysteresis and state.inside_b:
            state.inside_b = False
            exit_b = True

        state.inside_a = inside_a
        flags = {
            'enter_a': enter_a,
            'exit_a': exit_a,
            'enter_b': enter_b,
            'exit_b': exit_b,
        }
        return state, flags

    def cleanup(self, current_frame: int, max_age: int):
        for track_id in list(self.state_cache.keys()):
            if current_frame - self.state_cache[track_id]['last_frame'] > max_age:
                self.state_cache.pop(track_id, None)

    def drop_track(self, track_id: int):
        self.state_cache.pop(track_id, None)

    def resolve_direction(self, state: ZoneState) -> Tuple[int, str]:
        if state is None:
            return 0, ''
        flow_start, flow_end = self.flow_vector
        fx = flow_end[0] - flow_start[0]
        fy = flow_end[1] - flow_start[1]
        if fx == 0 and fy == 0:
            return 0, ''
        entry_ratio = state.enter_ratio
        exit_ratio = state.exit_ratio
        if entry_ratio <= 0.0 and state.entry_point != (0.0, 0.0):
            entry_ratio = self._relative_position(state.entry_point)
        exit_pt = state.exit_point if state.exit_point != (0.0, 0.0) else state.last_anchor
        if exit_ratio <= 0.0 and exit_pt != (0.0, 0.0):
            exit_ratio = self._relative_position(exit_pt)
        if entry_ratio <= 0.0 and exit_ratio <= 0.0:
            return 0, ''
        zone_min = getattr(self, '_zone_a_min', 0.25)
        zone_max = getattr(self, '_zone_a_max', 0.75)
        if zone_max - zone_min < 1e-6:
            zone_min = 0.25
            zone_max = 0.75
        mid = 0.5 * (zone_min + zone_max)
        entry_forward = entry_ratio <= mid
        exit_front = exit_ratio >= mid
        if entry_forward and exit_front:
            return 5, '正向前出'
        if (not entry_forward) and exit_front:
            return 7, '反向前出'
        if entry_forward and (not exit_front):
            return 6, '正向反出'
        return 8, '反向反出'

    def _relative_position(self, point: Tuple[float, float]) -> float:
        flow_start, flow_end = self.flow_vector
        fx = flow_end[0] - flow_start[0]
        fy = flow_end[1] - flow_start[1]
        seg_len_sq = fx * fx + fy * fy
        if seg_len_sq <= 1e-6:
            return 0.0
        px = point[0] - flow_start[0]
        py = point[1] - flow_start[1]
        proj = (px * fx + py * fy) / seg_len_sq
        return max(0.0, min(1.0, proj))
