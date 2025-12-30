# 车辆冲洗事件上报 payload 说明

本文件说明本地事件 JSON 通过 `EventManager._build_api_payload` 转换为对外上报的请求体结构, 对齐甲方《车辆冲洗事件上报接口文档》, 并标注各字段的必填/可空规则。

上报接口与甲方约定为:

- 接口名称: 车辆冲洗事件上报接口
- 接口描述: 用于上报车辆在冲洗/绕行区域全流程中的关键事件, 每个事件对应一次过车记录, 通过 `id` 和 `type` 唯一标识不同阶段。
- 请求 URL: `/api/vehicle/wash-event` (由配置 `system.api.url` 指定)
- 请求方式: `POST`
- 数据格式: `application/json`

## 1. 公用请求参数

所有类型的事件请求体中都包含以下两个公用参数:

| 参数名 | 类型 | 必填 | 描述 |
| ------ | ---- | ---- | ---- |
| `id`   | String | 是 | 本次过车流程的唯一标识符。由 `device_id_trackId` 组成, 同一辆车从事件 1 到事件 5 必须使用相同的 `id`。 |
| `type` | Integer | 是 | 事件类型。可选值:<br/>1 - 初始化(发现车辆)<br/>2 - 车辆即将进入区域<br/>3 - 车辆正在冲水<br/>4 - 车辆离开冲洗/绕行区域<br/>5 - 事件终结(车辆离开) |

此外, 所有事件都会携带一组通用业务字段(见下面各事件说明中的表格), 其中部分为必填, 部分为可空。

## 2. 事件 1 (type=1): 初始化(发现车辆)

车辆首次被识别并锁定轨迹时上报, 包含车辆的基础信息。

### 2.1 请求体示例

```json
{
  "id": "RK3588-DEV_5",
  "type": 1,
  "captureTime": "2025-12-22 14:15:20",
  "captureImage": "base64_string_of_jpg_bytes",
  "lane": "冲洗",
  "plateNumber": "苏AW1379",
  "plateConfidence": 0.97,
  "plateColor": "黄色",
  "plateColorConfidence": 0.90,
  "vehicleType": "渣土车",
  "vehicleTypeConfidence": 0.95,
  "plateIsGuess": false
}
```

### 2.2 字段说明

| 参数名 | 类型 | 必填 | 描述 |
| ------ | ---- | ---- | ---- |
| `captureTime` | String | 是 | 事件抓拍时间, 格式 `YYYY-MM-DD HH:MM:SS`, 由帧时间戳转换。 |
| `captureImage` | String | 是 | 抓拍图片。当前配置为 Base64 模式: 读取对应 JPG 文件的二进制内容, 做标准 Base64 编码, 再用 UTF-8 解码为**纯文本字符串**, 不包含 `data:image/jpeg;base64,` 前缀; 对端在展示时可自行加上前缀。 |
| `lane` | String | 是 | 车辆所在车道, 由配置 `lane_name` 决定, 例如 `"冲洗"` 或 `"绕行"`。 |
| `plateNumber` | String | 否 | 识别出的车牌号; 未识别成功时为空串。 |
| `plateConfidence` | Float | 否 | 车牌号识别置信度, 0–1 之间的小数, 由整条轨迹多帧平均得到。 |
| `plateColor` | String | 否 | 识别出的车牌颜色, 例如 `"蓝色"`、`"黄色"` 等; 未能推断时使用默认颜色或空串。 |
| `plateColorConfidence` | Float | 否 | 车牌颜色识别置信度, 0–1 之间的小数; 推断命中时通常为 0.9, 否则回退为默认值。 |
| `vehicleType` | String | 否 | 识别出的车型, 输出为**中文标签**, 例如: `"小汽车"`、`"蓝色卡车"`、`"黄色卡车"`、`"渣土车"`、`"五小工程车"`。 |
| `vehicleTypeConfidence` | Float | 否 | 车型识别置信度, 0–1 之间的小数, 由整条轨迹多帧平均得到。 |
| `plateIsGuess` | Boolean | 否 | 是否来自影子车牌池的推断结果; 仅在车牌由候选池推断时为 `true`。 |

> 说明: 实现上, 以上字段在 JSON 中都会携带, 但对于甲方接口语义, 标为“否”的字段可视为可选, 值可能为空或缺省。

## 3. 事件 2/3/4: 车辆进入/冲洗中/离开洗区

- 事件 2 (type=2): 车辆即将进入区域
- 事件 3 (type=3): 车辆正在冲水
- 事件 4 (type=4): 车辆离开冲洗/绕行区域

这三个事件在本接口中的结构相同, 主要用于标记流程节点。

### 3.1 请求体示例 (以事件 2 为例)

```json
{
  "id": "RK3588-DEV_5",
  "type": 2,
  "captureTime": "2025-12-22 14:15:25",
  "captureImage": "base64_string_of_jpg_bytes",
  "lane": "冲洗",
  "plateNumber": "苏AW1379",
  "plateConfidence": 0.97,
  "plateColor": "黄色",
  "plateColorConfidence": 0.90,
  "vehicleType": "渣土车",
  "vehicleTypeConfidence": 0.95,
  "plateIsGuess": false,
  "washStartTime": "2025-12-22 14:15:27"
}
```

### 3.2 字段说明 (type = 2/3/4 通用)

| 参数名 | 类型 | 必填 | 描述 |
| ------ | ---- | ---- | ---- |
| `captureTime` | String | 是 | 事件抓拍时间, 格式 `YYYY-MM-DD HH:MM:SS`。 |
| `captureImage` | String | 是 | 抓拍图片, Base64 编码 JPG, 规则同事件 1。 |
| `lane` | String | 是 | 车辆所在车道, 例如 `"冲洗"` 或 `"绕行"`。 |
| `plateNumber` | String | 否 | 识别出的车牌号; 未识别成功时为空串。 |
| `plateConfidence` | Float | 否 | 车牌号识别置信度, 0–1 小数, 为整条轨迹的平均值。 |
| `plateColor` | String | 否 | 识别出的车牌颜色。 |
| `plateColorConfidence` | Float | 否 | 车牌颜色置信度, 0–1 小数。 |
| `vehicleType` | String | 否 | 识别出的车型, 为中文标签(如 `"渣土车"`), 算法同事件 1。 |
| `vehicleTypeConfidence` | Float | 否 | 车型识别置信度, 0–1 小数, 为整条轨迹的平均值。 |
| `plateIsGuess` | Boolean | 否 | 是否来自影子车牌池推断。 |
| `washStartTime` | String | 否 | 冲洗开始时间。当系统已经判定车辆进入冲洗状态时携带, 格式 `YYYY-MM-DD HH:MM:SS`。 |

> 说明: 相比甲方原始文档中事件 2/3/4 仅要求 `captureTime` 和 `captureImage`, 本实现会在条件允许时附带车牌/车型等更多信息, 但这些字段可视为可选字段。

## 4. 事件 5 (type=5): 事件终结(车辆离开)

车辆完全离开区域时上报, 包含本次冲洗的最终统计信息。

### 4.1 请求体示例

```json
{
  "id": "RK3588-DEV_5",
  "type": 5,
  "captureTime": "2025-12-22 14:17:19",
  "captureImage": "base64_string_of_jpg_bytes",
  "lane": "冲洗",
  "washEndTime": "2025-12-22 14:17:10",
  "videoEndTime": "2025-12-22 14:17:19",
  "totalWashDuration": 112.14,
  "cleanliness": 0,
  "plateNumber": "苏AW1379",
  "plateConfidence": 0.97,
  "plateColor": "黄色",
  "plateColorConfidence": 0.90,
  "vehicleType": "渣土车",
  "vehicleTypeConfidence": 0.95,
  "plateIsGuess": false,
  "washStartTime": "2025-12-22 14:15:27",
  "direction": 5,
  "directionLabel": "正向前出"
}
```

### 4.2 字段说明

| 参数名 | 类型 | 必填 | 描述 |
| ------ | ---- | ---- | ---- |
| `captureTime` | String | 是 | 最终抓拍/离开时间, 格式 `YYYY-MM-DD HH:MM:SS`。 |
| `captureImage` | String | 是 | 最终抓拍图片, Base64 编码 JPG, 规则同事件 1。 |
| `lane` | String | 是 | 车辆所在车道, 例如 `"冲洗"` 或 `"绕行"`。 |
| `washEndTime` | String | 是 | 冲水结束时间, 格式 `YYYY-MM-DD HH:MM:SS`。内部实现优先取冲水结束帧对应时间, 如为空则回退为当前事件 `captureTime`。 |
| `videoEndTime` | String | 是 | 视频结束时间, 即车辆完全从监控区域消失的时间, 格式 `YYYY-MM-DD HH:MM:SS`。 |
| `totalWashDuration` | Float | 是 | 实际冲水总时长(秒)。仅统计有效冲水区间, 中途停顿会被扣除, 不等于简单的 `washEndTime - washStartTime`。 |
| `cleanliness` | Integer | 是 | 车身清洁度评估, 百分制(0–100)。当前实现使用配置默认值, 后续可替换为真实评分。 |
| `plateNumber` | String | 否 | 车牌号, 通常为整个流程中置信度最高或平均后的结果; 未识别成功时为空串。 |
| `plateConfidence` | Float | 否 | 车牌号平均置信度, 0–1 小数。 |
| `plateColor` | String | 否 | 车牌颜色。 |
| `plateColorConfidence` | Float | 否 | 车牌颜色平均置信度, 0–1 小数。 |
| `vehicleType` | String | 否 | 车型, 中文标签, 例如 `"小汽车"`、`"渣土车"` 等。 |
| `vehicleTypeConfidence` | Float | 否 | 车型平均置信度, 0–1 小数。 |
| `plateIsGuess` | Boolean | 否 | 是否来自影子车牌池推断。 |
| `washStartTime` | String | 否 | 冲水开始时间, 格式 `YYYY-MM-DD HH:MM:SS`。如果在事件 3 阶段已判定开始冲洗, 终结事件会回带此字段。 |
| `direction` | Integer | 否 | 行驶方向编码, 例如 5 表示正向前出, 7 表示反向前出; 未判定时为 0。 |
| `directionLabel` | String | 否 | 行驶方向中文描述, 例如 `"正向前出"`。 |

## 5. 返回参数

接口返回结构遵循甲方定义:

| 参数名 | 类型 | 描述 |
| ------ | ---- | ---- |
| `code` | Integer | 状态码。`200` 表示成功, 非 `200` 表示失败。 |
| `message` | String | 对状态码的详细描述信息。 |
| `data` | Object | 返回的数据, 可能为空。 |

成功响应示例:

```json
{
  "code": 200,
  "message": "事件上报成功",
  "data": null
}
```

失败响应示例:

```json
{
  "code": 400,
  "message": "请求参数校验失败：id不能为空",
  "data": null
}
```

## 6. 注意事项

1. `id` 的唯一性与连续性: 必须保证同一辆车从 `type=1` 到 `type=5` 的整个流程使用相同的 `id`, 以便系统关联所有事件。
2. 字段可选性: 事件 1 和事件 5 中, 车辆识别信息(车牌、车型等)在协议上为可选字段, 但如果识别成功, 当前实现会尽量全部上报。
3. 时间格式: 所有时间字段均使用 `YYYY-MM-DD HH:MM:SS` 格式, 基于帧时间戳转换。
4. 图片格式: `captureImage` 字段在当前配置下为 **JPG 二进制的标准 Base64 编码字符串**, 不含 `data:image/jpeg;base64,` 前缀。对端若用于前端展示, 需自行拼接前缀。
5. 置信度计算: 事件 5 中的车牌/车型置信度为整个流程中识别结果的平均值(按帧加权), 与甲方文档中“平均值或最优值”的要求兼容。
