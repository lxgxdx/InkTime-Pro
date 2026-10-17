# InkTime 服务器（Docker 版）

一种墨水屏电子相框的服务端软件。基于 [dai-hongtao/InkTime](https://github.com/dai-hongtao/InkTime) 改造，
一个容器 = 照片打分 + 每日选片渲染 + HTTP 下载 + 网页控制台，跑在 NAS（Unraid）上。

## 它能做什么

| 功能 | 说明 |
|---|---|
| 照片打分 | 用大模型（MiniMax/智谱/DeepSeek）给相册照片打"回忆度/美观度"分、写一句旁白文案 |
| 每日出图 | 每天挑一张"历史上的今天"，渲染成墨水屏能显示的 `.bin` 图片，供相框下载 |
| 多屏支持 | 同时给多个不同尺寸/分辨率的墨水屏各出一份图片 |
| AI 智能裁切 | 让大模型指出照片主体，自动裁出好看的构图，不再是死板的居中 |
| 无意义照片过滤 | 截图、收据、杂物等会被标注，不生成成品、不浪费额度 |
| 照片去重 | 找出内容相同 / 连拍 / 视觉相似的照片并标记，扫描时自动跳过（省额度、不重复上屏）；设置页可手动重建索引并实时看进度 |
| 刷新拍摄地点 | 换过城市库之后，按库里已有的 GPS 坐标重算地名 —— 不必重扫相册、不消耗额度（重扫会把每张重新送大模型，为改个地名不值当） |
| 额度自动利用 | 深夜空闲时自动多扫几张，把 MiniMax 快要清零的套餐额度用掉，不浪费 |
| 网页控制台 | 浏览器里看打分结果、模拟渲染效果、改配置，全程中文界面 |

## 三件套

| 脚本 | 作用 | 触发 |
|---|---|---|
| `analyze_photos.py` | 扫相册 → 调大模型打分+写文案+AI裁切 → 存数据库 | 手动 / 定时 |
| ↳ `analyze_photos.py --dedupe-only` | 只建/更新去重索引（不调大模型、不烧额度）。首次全量要读遍相册每个文件，建议单独定时、别和扫描挤在一起 | 手动 / 定时 |
| ↳ `analyze_photos.py --refresh-cities` | 只按库里已有的 GPS 坐标重算拍摄地点（换过城市库之后刷这个即可）。不读文件、不调大模型，几秒钟 | 手动 |
| `render_daily_photo.py` | 每天选「历史上的今天」→ 渲染 `.bin` | 手动 / 定时 |
| `server.py` | Flask：给相框下载 `.bin` + 网页控制台(8765) + 后台自动任务 | 常驻 |
| `geocode.py` | 逆地理编码（GPS → 中文地名）。优先高德 API，本地城市库兜底，结果按 1km 网格缓存 | 被 analyze 调用 |

## 屏幕多分辨率（config 驱动一次出多块屏）

`config.py` 里 `SCREENS` 是**列表**，每个元素一块屏（含 `enabled` 开关、调色板、打包格式）。
勾选的屏会各自生成对应尺寸的图片；第一个勾选的是主屏。

| 屏 | 宽×高 | 打包格式 |
|---|---|---|
| GDEP073E01（当前，竖用） | 480×800 | `1byte_per_px` |
| 7.09" E6（以后） | 1200×1600 | `1byte_per_px` |
| 12.43" 133C（以后） | 1208×1600 | `2px_per_byte` |

六色调色板（索引=像素值）：`0黑 1白 2红 3黄 4蓝 5绿`，在 `SCREEN["palette"]` 里配置。

## 版式：一套比例适配所有分辨率（2026-10-17 重做）

旧版把字号写死成 22/20 px，480 宽时占 4.6% 画宽，1200 宽的 7.09" 上只剩 1.8% ——
字像蚂蚁；横用时日期还被裁出画布。现在整套版式以 **480 短边 = 1 倍**为基准等比缩放：

| 元素 | scale=1（480×800） | 7.09"（1200×1600） |
|---|---|---|
| 文案字号 | 22 px | 55 px |
| 日期/地点字号 | 18 px | 45 px |
| 文字带高度 | 108 px | 270 px |
| 电量图标 | 高 18 px（= 日期字号） | 高 45 px |

- 实现：`converter.layout_scale()/layout_band_height()`（预裁切和渲染共用同一份），
  `render_daily_photo._layout_metrics()` 算全部尺寸。
- **`text_area_height` 已废弃**：配置里还留着只为兼容旧文件，实际不参与计算。
- **电量图标**（2026-10-18 调整）：**字号 ≠ 字形高** —— 45px 的字体，数字实际只有 35px。
  外壳高和百分比高都取**数字字形高**（和「2022年10月17日」一样高，7.09" 上是 35px），
  整组右边缘贴齐页面右边距。固件按 `battery_slot.h` / `glyph_h` 推导整组尺寸；
  老服务器给的窄槽会自动缩到放得下。
  > 曾经把外壳按中文字高（44px）做过一版，用户实测"过于大了"，所以以数字字高为准。
  几何公式在 `_battery_group_width()` 和 `overlay.cpp` 各有一份，**必须同步改**。

> 折行也顺手修了：行首不放句读标点（避头点），两行时尽量均分 ——
> 之前会出现第二行只剩一个「。」的情况。
>
> 版式固定为**经典留白**：照片满宽 + 底部白条 + 文案左对齐；
> **最后一行整体右对齐**，格式为
> `2022年10月17日 深圳市南山区 [电量图标]`（中文日期、无前导零）。
> 不做主题切换。

## 拍摄地点：高德逆地理编码（2026-10-17 新增）

本地 `world_cities_zh.csv` 有两个硬伤：

1. **找的是"最近的地名点"，不是"照片在哪"** —— 最近的点常常是几十公里外的村镇；
2. 全球只有 **20%** 的点有中文名（中国境内 94%，但那 6% 的村镇/街区点**恰恰经常是
   "最近的点"**），命中就退化成拼音 —— 屏上出现 "Huanghua 27km" 这种看着像 bug 的东西。

`geocode.py` 改成**优先高德 Web 服务 API**：

1. 设置页「📍 拍摄地点」填 **Web 服务 Key**（或 `config.py` 的 `AMAP_KEY` / 环境变量）
2. Key 只存 `web_settings.json` / 环境，不进镜像
3. 查到的结果按 **约 1km 网格**缓存到 `geo_cache.json`（跟 `photos.db` 同目录），
   几千张照片通常只消耗几百次额度
4. 只有失败/没配 Key 时才退回本地城市库；本地库**只出中文名，不出拼音**

| | |
|---|---|
| 免费额度 | 个人认证开发者 **5000 次/日**（自然日 00:00 重置），QPS≈3 |
| 申请 | <https://lbs.amap.com> → 应用管理 → 新建应用 → 添加 Key → 服务平台选「Web 服务」 |
| 显示格式 | `市 + 区县` → 「深圳市南山区」；直辖市用省份顶（「北京市海淀区」） |
| 国外 | 高德返回非中国 → 自动退回本地库出中文名 |
| 查不到 | **整块地点不显示**（不再显示经纬度） |

改完 Key：设置页「保存设置」→「📍 按新 Key 重算全部地名」。

## 本机快速跑通（不装 Docker）

```bash
python -m venv venv
venv/Scripts/pip install -r requirements.txt   # Windows；Linux 用 venv/bin/pip
cp config-example.py config.py                  # 然后编辑 config.py 填 MiniMax key

# 1. 分析照片（首次批量；照片放 IMAGE_DIR）
export MINIMAX_API_KEY=你的key
python analyze_photos.py

# 2. 渲染一张当天的 bin
python render_daily_photo.py

# 3. 启动下载服务器 + 网页控制台
python server.py
# 浏览器开 http://127.0.0.1:8765/review 看效果
```

## 部署到 NAS（Unraid / 任意 Compose 主机）

### 方式 A：从 GitHub 拉镜像（推荐，升级最省事）

1. 把 `docker-compose.yml` 拷到 NAS 的 Compose 目录。
2. 编辑它：
   - `image:` 改成你的镜像（如 `ghcr.io/你的用户名/inktime:latest`）
   - `MINIMAX_API_KEY` 填你的 key
   - `volumes` 里的 `/mnt/user/photos` 改成你的相册真实路径
   - `/mnt/user/inktime_converted` 改成存成品的路径
3. 启动：`docker compose up -d`
4. 首次进容器跑一遍打分：
   ```bash
   docker exec -it inktime python analyze_photos.py
   ```
5. 浏览器验证：`http://NAS_IP:8765/review`

### 方式 B：本地构建

```bash
git clone <你的仓库地址> inktime && cd inktime
# 编辑 docker-compose.yml 同上
docker compose up -d --build
docker exec -it inktime python analyze_photos.py
```

## 增量升级（不丢照片和数据）

照片、数据库、设置、成品都放在**挂载卷**里，不在镜像里。所以升级只是换个镜像，数据全保留。

```bash
# 方式 A：拉预构建镜像
docker compose pull && docker compose up -d

# 方式 B：本地构建
git pull && docker compose up -d --build
```

升级后如果网页控制台还是旧版，`docker compose restart inktime` 即可。

## 与 ESP32 固件衔接

固件源码在 [`firmware/`](firmware/)（PlatformIO 工程，编译/烧录/配网步骤见那里的 README）。

### 固件要的下载地址

```
GET /static/inktime/<DOWNLOAD_KEY>/<屏名>/<方向>/manifest.json
GET /static/inktime/<DOWNLOAD_KEY>/<屏名>/<方向>/photo_<n>.bin
```

- `<屏名>` = `SCREENS[].name`，如 `GDEP073E01`
- `<方向>` 只有 `portrait`（480×800）和 `landscape`（800×480）两种。
  正方形照片归入 `portrait`，**不存在第三种值**（曾经有个 `square` 值，
  下载路由的方向白名单不认它，设备永远 404）
- `manifest.json` 字段：

  | 字段 | 含义 |
  |---|---|
  | `screen` / `orientation` | 屏名 / 方向 |
  | `canvas.{width,height}` | 该方向的画布尺寸（横图会对调） |
  | `bin_format` / `palette_size` | `1byte_per_px`，6 |
  | `count` | **该方向实际的张数**（不保证等于 `DAILY_PHOTO_QUANTITY`） |
  | `latest` | 恒为 `photo_0.bin` |
  | `generated_at` | 本次渲染时间戳。相框「换一张」后靠它判断新图是否真的出来了 |
  | `battery_slot.{x,y,w,h}` | 右下角留给相框画电量图标的**白底区坐标** |
  | `photos[]` | 每张的 `rel` / `path` / `date` / `side` / `memory` |

- **`count` 不保证是 5**，序号 `0..count-1` 连续无空洞；某个方向的目录
  **可能整个不存在**（那天该方向一张都没选中）。所以固件不能硬编码 `photo_0..4`。
- 相框的路径前缀只用到 `<屏名>/<方向>/` 这两段；`DOWNLOAD_KEY` 在 `config.py`，
  改后要同步改固件配网页里的「下载密钥」。
- 端口 8765 已在 compose 里映射；ESP32 能访问 NAS 该端口即可。

### 电量图标（`battery_slot`）

相框要在屏幕上画一个电量图标（充满绿、低于 20% 变红），位置在右下角。
但底部第二行是「日期左对齐 + 地点右对齐」，**右下角本来被地点文字占着**，
所以服务器渲染时会：

1. 把地点文字右端往左让出 `BATTERY_SLOT_W + GAP`；
2. 把这块白底区的坐标写进 `manifest.json` 的 `battery_slot`。

固件照着这个坐标画，**不硬编码位置** —— 以后改版式只要动服务器，固件不用重编。
槽的尺寸固定 72×30、不随方向缩放，因为固件那边用的是固定尺寸的点阵字模。

> 相关的两个函数在 `render_daily_photo.py`：`_text_band_metrics()`（文字两行的 y，
> 渲染和定位都调它）和 `_battery_slot()`。

### 设备状态上报

```
POST /api/device/report     {"soc":82,"voltage":4.01,"rssi":-58,"charging":true,
                             "full":false,"orientation":"portrait","fw":"v0.1.0"}
```

设置页「相框设备」那张卡片会显示。**不报也一切照常**，只是那几行是空的。
另有 `POST /api/next`（换一张，异步重渲染）和 `GET /api/device`（读状态）。

### 顶层兼容路径（旧固件用，新固件别用）

```
GET /static/inktime/<DOWNLOAD_KEY>/latest.bin
GET /static/inktime/<DOWNLOAD_KEY>/photo_<n>.bin
```

`latest.bin` 是「默认屏 + portrait 优先」的副本，仍然新鲜；但**顶层
`photo_<n>.bin` 已经没有任何代码会写了**，磁盘上若还有那是历史遗留文件，
拉到的会是陈旧内容。

## 说明（重要）

- `config.py` 含密钥，**不要提交**（已在 `.gitignore` 排除）。用 `.env` + 环境变量注入。
- 照片会上传大模型云端打分——涉及隐私，介意的话换本地模型或自建 VLM。
- 分析脚本可断点续跑（已处理的照片不重复），`-j 4` 并发加速。
- `web_settings.json`（网页控制台保存的 key/屏幕/阈值设置）也已在 `.gitignore` 排除。

## 支持的图片格式

| 格式 | 说明 |
|---|---|
| JPG / JPEG / PNG / BMP / WEBP | 常规照片 ✅ |
| HEIC / HEIF | iPhone 手机照（`pillow-heif`）✅ |
| RAW（.dng/.cr2/.nef/.arw/.orf/.raf/.rw2/.pef） | 相机原片（`rawpy`/LibRaw 解码）✅ |

RAW 解码依赖 `rawpy`（Docker 里已含 `libraw`），某张解码失败会跳过不影响其它。

## MiniMax 适配（重要）

MiniMax-M3 是推理模型，默认输出带 `<think>...</think>` 思考块 + ````json ```` 围栏。
`analyze_photos.py` 已做兼容（自动剥离再解析）。换其它模型时这些仍是尽力兼容。

### 额度查询

MiniMax 套餐额度是"5 小时一清、用不完浪费"。网页控制台会显示实时剩余额度（进度条+百分比），
并在深夜空闲时段自动多扫描照片把额度用掉。接口 `GET https://api.minimaxi.com/v1/token_plan/remains`（注意是 `.com` 不是 `.io`）。
