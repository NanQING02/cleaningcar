import time
import os
import cv2
import argparse
from pathlib import Path
from utils.stream_reader import StreamManager
from utils.inference_engine import InferenceEngine
from utils.payload_factory import PayloadFactory
from utils.upload_queue import SQLiteUploadQueue
from utils.vision_utils import scale_polygon
import json

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--model', default='best.rknn')
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = json.load(f)

    stream_mgr = StreamManager(config['video']['source'], target_size=(640, 640), fps_limit=20)
    engine = InferenceEngine(args.model, core_mask=0b001) 
    
    payload_factory = PayloadFactory(
        config['system']['device_id'], 
        config['logic']['lane_name'],
        capture_mode=config['system']['api']['capture_mode']
    )
    uploader = SQLiteUploadQueue(Path('events/upload_queue.db'))

    stream_mgr.start()
    print("[Main] Optimized pipeline started.")

    try:
        while True:
            idx, frame_640 = stream_mgr.get_frame()
            if frame_640 is None or idx is None:
                time.sleep(0.005)
                continue

            t0 = time.time()
            outputs = engine.infer(frame_640)
            infer_time = time.time() - t0

            boxes, scores, classes = engine.post_process(outputs, 0.3, 0.45, (1080, 1920))
            
            if len(boxes) > 0:
                print(f"[Main] Detected {len(boxes)} objects. Latency: {infer_time*1000:.1f}ms")
                
            if idx % 100 == 0:
                print(f"[Main] Processed {idx} frames...")


    except KeyboardInterrupt:
        print("[Main] Stopping...")
        stream_mgr.stop()
        uploader.close()

if __name__ == "__main__":
    main()
