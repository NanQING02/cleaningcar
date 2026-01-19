import cv2
import time
import numpy as np
import multiprocessing as mp
from multiprocessing import shared_memory
import os

class StreamReader(mp.Process):
    """
    High-performance RTSP Stream Reader using Multiprocessing and Shared Memory.
    Performs CPU-based resizing in the child process to save main process resources.
    """
    def __init__(self, rtsp_url, target_size=(640, 640), fps_limit=None, shm_name=None):
        super().__init__(daemon=True)
        self.rtsp_url = rtsp_url
        self.target_w, self.target_h = target_size
        self.fps_limit = fps_limit
        self.shm_name = shm_name
        
        # Shared flags
        self.running = mp.Value('b', True)
        self.new_frame_evt = mp.Event()
        self.frame_idx = mp.Value('L', 0)
        
        # Buffer for raw BGR frame
        self.img_size = self.target_w * self.target_h * 3
        
    def run(self):
        # Set CPU affinity to Little Cores (0-3) to save Big Cores for NPU
        try:
            if hasattr(os, 'sched_setaffinity'):
                os.sched_setaffinity(0, {0, 1, 2, 3})
        except Exception:
            pass

        # Create Shared Memory if not provided
        shm = shared_memory.SharedMemory(name=self.shm_name)
        shared_img = np.ndarray((self.target_h, self.target_w, 3), dtype=np.uint8, buffer=shm.buf)

        cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        # Low latency options for FFmpeg
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        
        last_frame_time = time.time()
        
        while self.running.value:
            ret, frame = cap.read()
            if not ret:
                print(f"[Reader] Stream disconnected: {self.rtsp_url}. Reconnecting...")
                cap.release()
                time.sleep(2)
                cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
                continue

            # FPS Limiting
            if self.fps_limit:
                now = time.time()
                if now - last_frame_time < 1.0 / self.fps_limit:
                    continue
                last_frame_time = now

            resized = cv2.resize(frame, (self.target_w, self.target_h), interpolation=cv2.INTER_NEAREST)
            resized_rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
            
            shared_img[:] = resized_rgb[:]
            self.frame_idx.value += 1
            self.new_frame_evt.set()

        cap.release()
        shm.close()

class StreamManager:
    def __init__(self, rtsp_url, target_size=(640, 640), fps_limit=25):
        self.target_w, self.target_h = target_size
        self.img_size = self.target_w * self.target_h * 3
        
        # Allocate Shared Memory
        self.shm = shared_memory.SharedMemory(create=True, size=self.img_size)
        self.reader = StreamReader(rtsp_url, target_size, fps_limit, self.shm.name)
        self.shared_img = np.ndarray((self.target_h, self.target_w, 3), dtype=np.uint8, buffer=self.shm.buf)
        
    def start(self):
        self.reader.start()
        
    def get_frame(self):
        if self.reader.new_frame_evt.is_set():
            self.reader.new_frame_evt.clear()
            return self.reader.frame_idx.value, self.shared_img.copy()
        return None, None

    def stop(self):
        self.reader.running.value = False
        self.reader.join(timeout=2)
        self.shm.close()
        self.shm.unlink()

if __name__ == "__main__":
    # Test block
    URL = "rtsp://admin:yanya999@192.168.1.111:554/stream1"
    mgr = StreamManager(URL)
    mgr.start()
    try:
        while True:
            idx, img = mgr.get_frame()
            if img is not None:
                print(f"Captured frame {idx}")
                # cv2.imshow("test", img)
                # if cv2.waitKey(1) & 0xFF == ord('q'): break
            time.sleep(0.01)
    except KeyboardInterrupt:
        mgr.stop()
