import json
import base64
import time
from datetime import datetime

class PayloadFactory:
    """
    Standardizes the JSON payload for event reporting to the甲方 API.
    Ensures all fixed fields are present and correctly formatted.
    """
    def __init__(self, camera_id, lane_name, capture_mode='base64'):
        self.camera_id = camera_id
        self.lane_name = lane_name
        self.capture_mode = capture_mode

    def create_payload(self, event, track_state, extra_info=None):
        evt_type = event.get('type')
        payload = {
            'id': event.get('id'),
            'type': evt_type,
            'captureTime': event.get('captureTime') or self._now(),
            'lane': self.lane_name,
            'plateNumber': event.get('plateNumber', ''),
            'vehicleType': event.get('vehicleType', ''),
            'plateIsGuess': bool(event.get('plateIsGuess', False))
        }

        # Handle Image
        img_data = event.get('captureImage', '')
        if self.capture_mode == 'base64' and img_data and not img_data.startswith('data:'):
            # If it's a file path, we should read it (this part usually handled outside or passed as bytes)
            # For now, we assume captureImage passed is already base64 or handled by uploader
            pass
        payload['captureImage'] = img_data

        # Event Specific Fields
        if evt_type == 1:
            payload.update({
                'plateConfidence': event.get('plateConfidence', 0.0),
                'plateColor': event.get('plateColor', ''),
                'plateColorConfidence': event.get('plateColorConfidence', 0.0),
                'vehicleTypeConfidence': event.get('vehicleTypeConfidence', 0.0),
            })
        
        elif evt_type == 5: # Completion
            payload.update({
                'washStartTime': event.get('washStartTime', ''),
                'washEndTime': event.get('washEndTime', ''),
                'videoEndTime': event.get('videoEndTime', ''),
                'totalWashDuration': round(event.get('totalWashDuration', 0.0), 2),
                'cleanliness': event.get('cleanliness', 0),
                'videoDuration': event.get('videoDuration', 0.0),
                'direction': event.get('direction', 0),
                'directionLabel': event.get('directionLabel', ''),
            })

        # Abnormal status
        if event.get('isAbnormal'):
            payload['isAbnormal'] = True
            payload['abnormalReason'] = event.get('abnormalReason', '')

        return payload

    def _now(self):
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    @staticmethod
    def encode_image(image_path):
        try:
            with open(image_path, 'rb') as f:
                return base64.b64encode(f.read()).decode('utf-8')
        except Exception:
            return ""
