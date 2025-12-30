# GStreamer 硬解开关使用说明

> 当前开发环境主要以 OpenCV 软件解码为主，GStreamer+mpp 方案尚未在甲方现场全量验证，仅供提前预留的“可选加速”。

## 1. 所需环境

1. **Rockchip GStreamer 插件**：确保系统可安装 `gstreamer1.0-rockchip1`、`gstreamer1.0-plugins-bad`、`gstreamer1.0-plugins-base`、`gstreamer1.0-plugins-good` 等组件。
2. **Mpp 运行时**：通常随上面的 Rockchip 仓库一起安装（例如 `ppa:george-coolpi/multimedia`）。如果系统未提供，可从 Rockchip 官方源码仓库自行编译安装。
3. **权限**：安装软件包需要 `sudo`。若环境受限，请让运维预先装好上述包。

## 2. 配置 `cam.yaml`

在对应摄像头的 `cli_args` 中设置：

```yaml
cli_args:
  video: rtsp://{lan-ip}/xxx        # 或本地文件
  hw_decode: true                   # 开启硬解
  hw_pipeline: ''                   # 若留空则使用默认 pipeline
```

- `hw_decode: true`：走 GStreamer -> appsink 流程，减少 CPU 解码开销。
- `hw_pipeline`（可选）：自定义 pipeline，例如  
  `rtspsrc location=... latency=200 ! rtph264depay ! h264parse ! mppvideodec ! videoconvert ! video/x-raw,format=BGR`.
- 若 `hw_decode` 为 false，则自动回退为 OpenCV `VideoCapture` 软件解码，无需改代码。

## 3. 运行与排障

1. 执行 `./run_from_config.sh -- --cameras wash`，注意启动日志会打印 `Using GStreamer capture` 用于确认硬解生效。
2. 若提示 “缺少 mppvideodec”，说明插件未正确安装或 GStreamer 版本过旧，请先在命令行确认 `gst-inspect-1.0 mppvideodec` 能找到插件。
3. 如果 pipeline 拉流失败，程序会自动回退为软件解码并给出警告；为保证稳定，可在正式上线前做 1~2 小时的压力测试。

## 4. 已知限制

- 尚未在甲方现场摄像头上做全面测试，若遇到花屏/卡顿，请临时关闭 `hw_decode`。
- 目前仅支持 H.264/H.265 RTSP 以及标准容器文件；其他编码需自行扩展 pipeline。
- 与软件解码相比，硬解画面格式固定为 BGR；若需 YUV/RGBA，可在 `hw_pipeline` 中自行插入 `videoconvert`。

如需进一步调优（例如 mpp 动态频率、零拷贝等），可在该文档基础上扩展。暂不建议甲方自行修改脚本代码。
