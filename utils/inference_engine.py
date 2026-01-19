import time
import numpy as np
from rknnlite.api import RKNNLite
from utils.vision_utils import decode_scale, nms, scale_boxes, STRIDES, CLASS_THRESH

class InferenceEngine:
    """
    Handles RKNN initialization and inference for YOLO11.
    Optimized for NPU with CPU-based post-processing.
    """
    def __init__(self, model_path, core_mask=None):
        self.rk = RKNNLite()
        if self.rk.load_rknn(model_path) != 0:
            raise RuntimeError(f"Failed to load RKNN model: {model_path}")
        
        init_kwargs = {}
        if core_mask is not None:
            # On RK3588, core_mask 1, 2, 4 map to different NPU cores
            init_kwargs['core_mask'] = core_mask
            
        if self.rk.init_runtime(**init_kwargs) != 0:
            raise RuntimeError("Failed to initialize RKNN runtime")
            
    def infer(self, img_rgb):
        """
        Runs inference and returns raw outputs.
        """
        # NHWC format expected by RKNN typically
        outputs = self.rk.inference(inputs=[np.expand_dims(img_rgb, 0)], data_format=['nhwc'])
        return outputs

    def post_process(self, outputs, conf_thresh, iou_thresh, orig_shape, imgsz=640):
        """
        Decodes RKNN outputs into bounding boxes.
        """
        if not outputs:
            return [], [], []

        boxes_list = []
        scores_list = []
        classes_list = []
        
        # Calculate ratio and pad for scaling back to original image
        h, w = orig_shape
        r = min(imgsz / h, imgsz / w)
        pad_w = (imgsz - w * r) / 2
        pad_h = (imgsz - h * r) / 2
        
        for i, stride in enumerate(STRIDES):
            reg = outputs[i * 3 + 0]
            cls = outputs[i * 3 + 1]
            obj = outputs[i * 3 + 2]
            
            boxes, cls_ids, cls_scores, _ = decode_scale(reg, cls, obj, stride)
            
            # Filter by confidence
            keep = cls_scores >= conf_thresh
            if not np.any(keep):
                continue
                
            boxes_list.append(boxes[keep])
            scores_list.append(cls_scores[keep])
            classes_list.append(cls_ids[keep])
            
        if not boxes_list:
            return [], [], []
            
        boxes = np.concatenate(boxes_list, axis=0)
        scores = np.concatenate(scores_list, axis=0)
        classes = np.concatenate(classes_list, axis=0)
        
        # Scale back to original image coordinates
        boxes = scale_boxes(boxes, (r, r), (pad_w, pad_h), orig_shape)
        
        # NMS
        keep = nms(boxes, scores, iou_thresh, 300)
        
        return boxes[keep], scores[keep], classes[keep]
