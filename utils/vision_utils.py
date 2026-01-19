import numpy as np
import cv2

REG_MAX = 16
PROJECT = np.arange(REG_MAX, dtype=np.float32)
STRIDES = [8, 16, 32]

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

def is_normalized(points):
    if not points:
        return False
    return all(0.0 <= p[0] <= 1.0 and 0.0 <= p[1] <= 1.0 for p in points)

def scale_polygon(points, width, height):
    if not points:
        return []
    if is_normalized(points):
        return [(float(x) * width, float(y) * height) for x, y in points]
    return [(float(x), float(y)) for x, y in points]

def scale_point(point, width, height):
    if not point:
        return (0.0, 0.0)
    x, y = point
    if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
        return float(x) * width, float(y) * height
    return float(x), float(y)

def letterbox_cpu(im, new_shape=640, color=(114, 114, 114)):
    shape = im.shape[:2]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
    new_unpad = (int(round(shape[1] * r)), int(round(shape[0] * r)))
    dw = (new_shape[1] - new_unpad[0]) / 2
    dh = (new_shape[0] - new_unpad[1]) / 2
    if shape[::-1] != new_unpad:
        im = cv2.resize(im, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    im = cv2.copyMakeBorder(im, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)
    return im, (r, r), (dw, dh)
