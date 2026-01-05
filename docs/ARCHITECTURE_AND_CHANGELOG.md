# CleaningCar 项目总览与变更记录

本文件提供 `cleaningcar_v2` 的架构说明、运行与配置指南、关键模块与数据流、Web 控制台能力，以及一个持续维护的变更记录。后续所有改动请在「变更记录」段落按格式追加。

## 1. 项目目的与范围
- 面向洗车/冲洗场景的边缘端推理程序，完成车辆与车牌检测、进入/退出区域判定、冲洗阶段识别、水雾触发、事件上报与本地留存。
- 主要入口：`run_zone_detect.py`，唯一配置入口：`config.json`。
- 配套远程调参与守护：`web/server.py`（FastAPI + Vue 单页，含 Guardian 管理推理子进程）。

## 2. 架构总览
- 推理主流程
  - 入口脚本：`run_zone_detect.py`，参数解析见 `run_zone_detect.py:236`。
  - 视频读取：优先 GStreamer+mpp 硬解，失败回退 OpenCV，见 `run_zone_detect.py:311`。
  - 多线程推理：若干 `DetectWorker` 线程并行执行 RKNN，见 `run_zone_detect.py:1611`。
  - 跟踪与状态：`VehicleTracker`（`run_zone_detect.py:859`）、`PlateTextTracker`（`run_zone_detect.py:730`），融合到 `EventManager`（`run_zone_detect.py:1014`）。
- 区域与方向
  - 区域判定与迟滞：`ZoneManager.update_track`，见 `zone_manager.py:43`。
  - 掩膜：`polygon_mask` 生成 Zone A 检测掩膜，见 `zone_manager.py:16`；主流程启用逻辑见 `run_zone_detect.py:1823`。
  - 方向计算：与流向矢量的余弦判定，见 `zone_manager.py:97`。
- 事件与上传
  - 事件构建与落盘：`EventManager.emit_event`（JSON + CSV），见 `run_zone_detect.py:1331`。
  - 上报缓冲与重试：`EventUploader`（`run_zone_detect.py:941`） + `SQLiteUploadQueue`（`utils/upload_queue.py:8`）。
- 存储与清理
  - 分段录像：`SegmentedVideoWriter`，见 `run_zone_detect.py:338`。
  - 磁盘清理：`DiskCleaner`，见 `utils/disk_manager.py:29`；入口初始化见 `run_zone_detect.py:2357`。
- 远程控制台
  - FastAPI 路由与 Guardian：`web/server.py`，推理进程管理类见 `web/server.py:76`；路由汇总见 `web/server.py:352` 附近。
  - 前端模板：`web/templates/zone_editor.html`，支持画布编辑 Zone/流向、日志、实时调试帧，并提供“多实例管理”面板，可同时启动/停止多个配置对应的推理进程。

## 3. 运行方式
- 命令行运行
  - 环境：`pip install -r requirements.txt`；RKNN Lite 2 请用配套 wheel 安装（`requirements.txt` 注释说明）。
  - 推理：`python3 run_zone_detect.py --config /home/hinlink/cleaningcar_v2/config.json`。
  - 输出目录：事件 JSON/CSV → `events/`，截图 → `captures/`，录像/检测 CSV → `video_result/`。
- Web 控制台
  - 启动：`python3 web/server.py --config /home/hinlink/cleaningcar_v2/config.json --host 0.0.0.0 --port 8000`。
  - 功能：配置编辑、Zone/流向绘制、实时调试帧、事件/检测日志、推理 Guardian 的 start/stop/restart/status。
  - 单次文件源：Guardian 自动识别 `video.source_mode=file`，跑完即停（`web/server.py:114`、`web/server.py:197`）。
- systemd 示例见 `README.md` 中「systemd 服务部署示例」。

## 4. 配置说明（config.json）
- `system`
  - `device_id`：摄像头/设备标识；用于事件 `id` 前缀。
  - `api.url` / `api.token` / `api.capture_mode`：事件上报接口、鉴权、截图字段格式（当前配置为 `base64`，即对 JPG 二进制做标准 Base64 编码后作为纯字符串上报，不带 `data:image/jpeg;base64,` 前缀）。
  - `monitor_interval`：资源监控日志周期（秒），见 `run_zone_detect.py:1743`。
  - `cpu_mask`：Guardian 拉起推理子进程时设置的 CPU 亲和力（如 `0-3,8`），便于多实例在 RK3588 大小核之间划分资源，避免互相争抢。
- `video`
  - `source` / `source_mode`：数据源（RTSP/文件路径）；自动/指定 `camera|file`（`run_zone_detect.py:1789`）。
  - `hw_decode`：启用 GStreamer+mpp 硬解（`run_zone_detect.py:311`）。
  - `workers` / `core_mask`：RKNN 并行 worker 数与 NPU 绑定（`run_zone_detect.py:1611`、`run_zone_detect.py:279`）。
  - `save_video` / `csv`：录像与检测 CSV 输出路径；分段控制 `segment_minutes`（`run_zone_detect.py:1839`）。
  - `debug_frame_path` / `debug_frame_interval`：调试帧位置与周期（`run_zone_detect.py:1891`）。
- `zones`
  - `zone_a_detection` / `zone_b_wash`：多边形（支持归一化坐标），缩放见 `run_zone_detect.py:480`。
  - `flow_vector.start|end`：流向起止点（可归一化），缩放见 `run_zone_detect.py:488`。
- `logic`
  - 锚点与迟滞：`anchor_offset_ratio`（车辆框底部向上偏移比例，`run_zone_detect.py:496`）、`zone_b_entry_hysteresis`/`zone_b_exit_hysteresis`（进入/退出 Zone B 需满足的连帧计数，`zone_manager.py:68`）。
  - 静止阈值与帧数：`stationary_speed_thresh`、`stationary_min_frames`；触发 Type3 的另一个条件（`run_zone_detect.py:1226`）。
  - 事件间隔与追踪：`type34_min_interval_frames`、`track_timeout_frames`/`track_max_age`、`vehicle_iou_threshold`（`run_zone_detect.py:1495`、`run_zone_detect.py:1296`、`run_zone_detect.py:859`）。
  - 影子车牌池：`shadow_plate_pool.max_candidates|max_age_frames`（`run_zone_detect.py:1038`）。
  - 其他：`vehicle_shrink_ratio`/`vehicle_lock_min_votes`（车型锁定策略，`run_zone_detect.py:1119`、`run_zone_detect.py:1128`），`wash_duration_offset_seconds`（冲洗时长偏移，`run_zone_detect.py:1060`），调试开关。
- `storage`
  - `disk_threshold` / `disk_target` / `clean_interval_seconds`：磁盘占用触发与回落阈值、清理周期（`utils/disk_manager.py:76`、`run_zone_detect.py:2357`）。

## 5. 区域与方向判定
- 锚点选择：默认取车辆框底部中心，按 `anchor_offset_ratio` 上移（`run_zone_detect.py:496`）。
- Zone 判定与迟滞
  - 每帧更新锚点是否在 Zone A/B 内；Zone B 进入/退出需累计连帧数达到阈值（`zone_manager.py:68`）。
  - `zone_a_mask_enable=true` 时，仅在 Zone A 内参与检测（`run_zone_detect.py:1823`）。
- 方向编码
  - 对比车辆进入/退出点与 `flow_vector` 的夹角余弦，正向为 5，反向为 7（`zone_manager.py:97`）。
  - Type5 事件携带 `direction` 与 `directionLabel`（`run_zone_detect.py:1365`）。

## 6. 事件模型与上报
- 事件类型：type=1~5，字段详解参见 `VehicleWash_API.md`。
- 触发逻辑（`EventManager.update_track`，`run_zone_detect.py:1073`）
  - Type1：进入 Zone A 首次锁定车辆。
  - Type2：确认进入 Zone B。
  - Type3：Zone B 候选态首次满足“静止或水雾接触”（`run_zone_detect.py:1226` → `run_zone_detect.py:1253`）。
  - Type4：冲洗结束或离开 Zone B（`run_zone_detect.py:1260`）。
  - Type5：离开 Zone A（轨迹超时也会补发，`run_zone_detect.py:1296`）。
- 上报与重试
  - `EventUploader` 将 payload 入队 SQLite，后台线程指数退避重试（`run_zone_detect.py:966`、`utils/upload_queue.py:42`）。
  - 成功删除队列项，失败按 `retries` 与 `next_retry` 更新（`utils/upload_queue.py:58`）。

## 7. Web 控制台与 Guardian
- 关键路由（`web/server.py:352` 附近）
  - `GET /`：返回前端模板。
  - `GET/POST /zones`：获取/保存 Zone A/B 与流向（`web/server.py:359`、`web/server.py:363`）。
  - `GET /config`、`POST /config`：读取/写入配置（`web/server.py:429`、`web/server.py:435`）。
  - `POST /inference/start|stop|restart|auto_restart` 与 `GET /inference/status`：推理子进程管理（`web/server.py:449`~`web/server.py:474`）。
  - `GET /frame|/debug_frame` 与 `GET /logs/events|/logs/detections|/logs/inference`：帧/日志读取。
- 前端画布（`web/templates/zone_editor.html`）
  - 归一化坐标绘制；命中顶点与拖拽、流向起止点调整、撤销等交互（如 `zone_editor.html:575`、`zone_editor.html:733`）。
- 多实例调试/监控
  - 「多实例管理」面板可对任意配置（多路摄像头）执行 Guardian start/stop/restart，并为每个子进程设置独立的 `system.cpu_mask`。
  - 实时调试画面/性能监控支持以实例下拉框选择数据来源，所有 `/frame*`、`/debug_frame*`、`/metrics/runtime` 请求都会附带 `key=`，便于在不切换配置文件的情况下查看其它实例的画面与指标。

## 8. 数据目录与文件
- 事件 JSON：`events/*.json`；事件 CSV 汇总：`events/event_log.csv`（`web/server.py:24`）。
- 截图：`captures/*.jpg`；录像与检测 CSV：`video_result/*.mp4|*.csv`。
- 调试帧：默认 `/dev/shm/cleaningcar_debug.jpg`，定期覆盖（`run_zone_detect.py:1891`）。
- 上传队列：`events/upload_queue.db`（SQLite WAL 模式，`utils/upload_queue.py:13`）。

## 9. 模型与识别
- 目标检测：`best.rknn`，推理入口在 `DetectWorker.run()`（`run_zone_detect.py:1646`）。
- 车牌识别：可选 `lprnet.rknn`，识别流程见 `run_zone_detect.py:1728`。
- 类别与阈值：`CLASS_NAMES`/`CLASS_THRESH`（`run_zone_detect.py:44`、`run_zone_detect.py:76`）；可由配置覆盖（`run_zone_detect.py:208`）。

## 10. 调试与监控
- 资源监控：启用 `monitor_interval` 后输出 CPU/内存/温度等（`run_zone_detect.py:1743`）。
- 调试覆盖：`logic.debug_overlay` 打开时，在调试帧叠加 Zone/锚点/水雾框等（`run_zone_detect.py:1884`）。
- 车型与车牌锁定：多帧投票与缩小冻结（`run_zone_detect.py:1119`、`run_zone_detect.py:1128`、`run_zone_detect.py:1146`）。

## 11. 已知限制与建议
- RKNN Lite 2 需匹配平台 wheel 手动安装（`requirements.txt` 注释）。
- 硬解 pipeline 对编码与插件版本有依赖，失败自动回退（详见 `docs/GStreamer_HW_Decode.md`）。
- Web 控制台默认允许任意来源访问，部署到公网需加 CORS 限制或网关鉴权（见 `README.md` 提示）。

## 12. 维护约定
- 变更记录格式
  - 日期：`YYYY-MM-DD`
  - 修改人：姓名或角色
  - 概述：一句话概述
  - 详情：要点列表（必要时附代码引用 `file_path:line_number`）
  - 影响范围：配置/接口/依赖/兼容性
- 在每次改动后将记录追加到本文件「变更记录」段落，并尽量附带代码引用以便快速定位。

## 13. 变更记录

- 2025-12-14 | AI | 创建项目总览与变更记录文档  
  - 新增 `docs/ARCHITECTURE_AND_CHANGELOG.md`，汇总架构、配置、事件、Web 控制台与维护约定。  
  - 后续改动将按第 12 节格式持续记录，便于审阅与复盘。
- 2025-12-14 | AI | 暴露车辆跟踪 IoU 阈值到配置与 Web 控制台  
  - 在 `config_manager.py:71` 增加 `logic.vehicle_iou_threshold` 默认 0.3。  
  - 在 `run_zone_detect.py:859` 使用该配置初始化 `VehicleTracker`，便于针对新场景调低 IoU 保持 ID 稳定。  
  - 在 `web/templates/zone_editor.html:135` 添加对应输入项，允许从 Web 调参并写回 `config.json`。
- 2025-12-14 | AI | 增强车辆跟踪与事件生命周期控制  
  - 在 `config_manager.py:81` 增加 `logic.vehicle_center_gate_ratio`、`logic.disable_plate_only_events`、`logic.single_lifecycle_events` 默认配置。  
  - 在 `run_zone_detect.py:859` 为 `VehicleTracker` 增加中心距离门控参数 `center_gate_ratio`，当 IoU 略低但中心距离足够近时仍可延续同一轨迹，缓解远距离场景下的断轨多 ID 问题。  
  - 在 `run_zone_detect.py:1014` 的 `EventManager` 中新增两个开关：`disable_plate_only_events` 禁止仅车牌轨迹触发 Type1-5 事件，`single_lifecycle_events` 保证单个 trackId 生命周期内最多触发一套完整事件（在 Type5 或超时清理后标记为 closed）。  
  - 在 `web/templates/zone_editor.html:119` 暴露上述新逻辑参数到 Web 控制台，可按场景通过 config 启用/关闭，避免影响已调好的 test01 场景。
- 2025-12-15 | AI | 为 Type5 事件增加 Zone A 停留帧逻辑门槛  
  - 在 `config_manager.py:71` 新增 `logic.min_zone_a_dwell_frames_for_type5` 默认 0（关闭），用于按场景配置 Type5 触发所需的最小 Zone A 停留帧数。  
  - 在 `run_zone_detect.py:1094` 的 `EventManager.update_track` 中为每个轨迹维护 `zone_a_enter_frame` 与 `zone_a_dwell_frames`，并在 `debug` 字段中输出 `zone_a_elapsed` 以便调试。  
  - 在正常 `exit_a` 分支与 `flush_inactive` 超时补发逻辑中，对 Type5 事件增加门槛：当 `min_zone_a_dwell_frames_for_type5 > 0` 且 `zone_a_dwell_frames` 小于该值时，不再触发 Type5，过滤掉短暂误检或“闪现”的人形/车辆带来的随机 Type5。  
  - 在 `web/templates/zone_editor.html:133` 中将 `min_zone_a_dwell_frames_for_type5` 暴露为“Type5 最小 Zone A 停留帧数 (0关闭)”输入项，可在 Web 控制台按场景单独调节，不影响已调好的 test01。
- 2025-12-15 | AI | 为 Type1 事件增加 Zone A 停留帧数逻辑门槛  
  - 在 `config_manager.py:71` 新增 `logic.min_track_frames_for_type1` 默认 0（关闭），用于按场景配置 Type1 触发所需的最小 Zone A 停留帧数。  
  - 在 `run_zone_detect.py:1227` 的 `EventManager.update_track` 中为每个轨迹维护 `zone_a_dwell_frames`，表示锚点在 Zone A 内累计停留的帧数。  
  - 在 `update_track` 中，当锚点处于 Zone A 内、且 `zone_a_dwell_frames` 大于等于阈值、并且尚未触发过 Type1 时触发 Type1；如果首次进入 A 时未满足阈值导致未触发，但后续进入 B 确认 Type2 前仍未产生 Type1，则在 Type2 前自动补发一次 Type1，保证 Type2 不会“裸奔”。  
  - 在 `web/templates/zone_editor.html:133` 中将 `min_track_frames_for_type1` 暴露为“Type1 最小 Zone A 停留帧数 (0关闭)”输入项，可在 Web 控制台按场景单独调节，不影响已调好的 test01。
- 2025-12-15 | AI | 为 Type1-5 事件增加车型锁定前置条件  
  - 在 `config_manager.py:68` 新增 `logic.require_vehicle_type_for_events` 默认 `False`，用于按场景控制是否要求轨迹先锁定 `vehicleType` 才允许触发 Type1-5 事件，避免仅车牌轨迹或未识别车型的轨迹产生事件。  
  - 在 `run_zone_detect.py:1088` 的 `EventManager.__init__` 中读取该开关为 `self.require_vehicle_type_for_events`。  
  - 在 `run_zone_detect.py:1405` 的 `EventManager.emit_event` 中，统一解析 `vehicleType`，当开启开关且结果为空字符串（包括仅车牌轨迹）时直接返回，不写入 JSON/CSV、不入上传队列，保证所有上报的 Type1-5 事件都具备非空车型字段。  
- 2025-12-21 | AI | 调整 Type4 触发逻辑、冲洗时长与车型补全策略  
  - Type4 触发逻辑：仅在 `exit_b` 时尝试触发，并新增 `logic.min_zone_b_dwell_frames_for_type4`（默认 60 帧）门槛，过滤短暂停留导致的过早结束；对应实现见 `run_zone_detect.py:1290-1334`，默认配置见 `config_manager.py:83-90,98-104`。  
  - 冲洗时长计算：移除 `wash_duration_offset_seconds` 的 20 秒偏移，改为优先使用 Type4 与 Type3 帧号差值（`last_type3_frame` 到当前帧），在缺失 Type3 的情况下回退到 Zone B 停留时长；实现见 `run_zone_detect.py:1528-1534`。  
  - 阈值整体下调：将 `logic.min_track_frames_for_type1` from 15→5、`logic.min_zone_a_dwell_frames_for_type5` from 30→25、`logic.zone_b_anchor_min_frames` from 15→10、`logic.vehicle_lock_min_votes` from 80→40，并同步到 `config.json` 与各场景配置 `config_test01.json` / `config_test02.json` / `config_003.json` / `config_004.json` / `config_8m.json` / `config_9m.json` / `config_11m.json` / `config_12m.json`，便于更早产生 Type1/2/5、更快冻结车型。  
  - 车型缺失时的事件待补全队列：当 `logic.require_vehicle_type_for_events=True` 时，Type3/4/5 在车型为空时不会直接丢弃，而是按 `track_id+event_type` 暂存于内存队列中；后续一旦 `_resolve_vehicle_type` 得到非空车型，将以统一车型回放历史 Type3/4/5 事件，而 Type1/2 始终允许在车型为空时触发，保证起止事件完整（实现见 `run_zone_detect.py:1409-1480`）。  
  - 双锚点方案：基于流向向量 `zones.flow_vector` 在车框底部中心（头锚点）沿反向偏移一段车高，得到更靠近车尾的尾锚点，并将尾锚点作为 Zone A/B 判定与停留帧数统计的主锚点，用于改善 Type4/5 触发时机的空间对齐；实现见 `run_zone_detect.py:1918-1945` 与 `zone_manager.py:34-87`。  
  - Web 控制台调整：将 `logic.disable_plate_only_events` 与 `logic.single_lifecycle_events` 从前端逻辑面板中移除，默认在配置加载时强制置为 `True`，仅保留 `require_vehicle_type_for_events`、`min_track_frames_for_type1`、`min_zone_a_dwell_frames_for_type5`、`zone_b_anchor_min_frames` 等关键参数给前端调节；对应模板变更见 `web/templates/zone_editor.html:168-202,264-282`。  
- 2025-12-25 | AI | 防止仅有 Type1 无 Type5 时 per-ID 录像无限增长  
  - 在 `run_zone_detect.py:1383-1418` 的 `EventManager.flush_inactive` 中，当轨迹已触发过 Type1（存在 `record_start_frame`）但始终未触发合法 Type5、且因超时被判定为不再活跃时，直接根据该轨迹的 `last_frame_idx` 内化一个固定的录像尾巴，将 `record_stop_frame` 设为 `last_frame_idx + fps*5`，确保 per-ID 录像在后续全局帧推进时自动在尾部 5 秒处停止，而不会因为缺失 Type5 导致视频文件无限增长。  
  - 该逻辑为内置行为，不再依赖任何新的配置项，保持事件触发门槛与现有 Type5 语义不变，仅为录像生命周期增加安全熔断，适用于 `logic.enable_per_id_video=True` 场景。  
- 2025-12-25 | AI | 调整轨迹生命周期控制以避免 closed 状态影响后续车辆  
  - 在 `run_zone_detect.py:1383-1430` 的 `EventManager.flush_inactive` 中，将超时清理逻辑拆分为两层：首先继续在必要时补发 Type4/Type5 事件，然后仅在轨迹已经拥有合法 Type5（`5 in st['events']`）且 `logic.single_lifecycle_events=True` 时才将其标记为 `closed`，避免短暂噪声轨迹或仅进入 ZoneA 未满足 Type5 门槛的轨迹被误设为 closed。  
  - 同时，无论 `single_lifecycle_events` 开关状态如何，在超时清理阶段都会对该 `track_id` 对应的内部状态条目执行 `self.tracks.pop(tid, None)`，确保后续上游检测器若复用相同数字 ID，`EventManager` 会将其视作全新的生命周期重新统计 ZoneA/B 停留与事件触发条件，从而避免 id9、17、20-25 这类“前段短噪声 + 后段真实业务”情况下，后段业务因继承 closed 状态而完全不产出 Type1-5 事件的问题。  
- 2026-01-05 | AI | 多实例调试画面与 CPU 亲和力配置  
  - 在 `web/templates/zone_editor.html` 新增 `system.cpu_mask` 输入框，并将“实时调试画面”改为带实例下拉，可选择任意 Guardian 子进程的帧缓存/调试 JPG/性能指标；与之匹配地，前端所有 `/frame_meta`、`/frame`、`/frame/reload`、`/debug_frame_meta`、`/debug_frame`、`/metrics/runtime` 请求都携带 `key=`，并在切换实例时自动刷新画面与指标。  
  - 在 `web/server.py` 的 `/metrics/runtime` 路由支持 `key` 参数，读取对应配置文件的 metrics JSON，确保多实例时性能面板不会串台。  
  - 文档新增 `system.cpu_mask` 说明与“多实例调试/监控”小节，记录 Guardian 绑定 CPU/查看不同实例调试画面的用法。  
