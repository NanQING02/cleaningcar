import os
import ctypes
import numpy as np
import cv2


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SO_PATH = os.path.join(BASE_DIR, "rga_wrapper", "libcleaningcar_rga.so")

try:
    _lib = ctypes.CDLL(SO_PATH)
    _lib.rga_resize_bgr.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    _lib.rga_resize_bgr.restype = ctypes.c_int
    RGA_OK = True
    print("[rga-resize-plugin] loaded", SO_PATH)
except Exception as e:
    print("[rga-resize-plugin] load so failed:", e)
    _lib = None
    RGA_OK = False


def rga_resize(im, new_unpad):
    tw, th = new_unpad
    h, w = im.shape[:2]
    if w == tw and h == th:
        return im
    if not RGA_OK or _lib is None:
        return cv2.resize(im, (tw, th), interpolation=cv2.INTER_LINEAR)
    src = np.ascontiguousarray(im)
    dst = np.empty((th, tw, 3), dtype=np.uint8)
    ret = _lib.rga_resize_bgr(
        src.ctypes.data,
        w,
        h,
        dst.ctypes.data,
        tw,
        th,
    )
    if ret != 0:
        print("[rga-resize-plugin] rga_resize_bgr failed:", ret)
        return cv2.resize(im, (tw, th), interpolation=cv2.INTER_LINEAR)
    return dst

