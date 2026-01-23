# backup/v1.7-dev-rtsp-未验证-不参考

- 分支：
- tip：（2026-01-20）
- 定位：开发增强线（RTSP/跟踪/车牌识别改进）

相对上一档（backup/v1.6-client-verified）主要变化：
- RTSP 跟踪稳定性提升、部分路径去 RGA
- ByteTrack 标准化 + early lifecycle gating 收紧
- 斜视角车牌预处理优化
- LPRNet 多帧融合（提升识别稳定性）

重要声明：
- 本分支改动较大，**未在甲方实际机器上运行/验证**，不作为部署基线。
- 如需启用，请在 RK3588 现场完成：性能（FPS/CPU/内存）+ 功能（双路/上传/抓拍）全量验证。
