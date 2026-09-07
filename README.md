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
| 额度自动利用 | 深夜空闲时自动多扫几张，把 MiniMax 快要清零的套餐额度用掉，不浪费 |
| 网页控制台 | 浏览器里看打分结果、模拟渲染效果、改配置，全程中文界面 |

## 三件套

| 脚本 | 作用 | 触发 |
|---|---|---|
| `analyze_photos.py` | 扫相册 → 调大模型打分+写文案+AI裁切 → 存数据库 | 手动 / 定时 |
| `render_daily_photo.py` | 每天选「历史上的今天」→ 渲染 `.bin` | 手动 / 定时 |
| `server.py` | Flask：给相框下载 `.bin` + 网页控制台(8765) + 后台自动任务 | 常驻 |

## 屏幕多分辨率（config 驱动一次出多块屏）

`config.py` 里 `SCREENS` 是**列表**，每个元素一块屏（含 `enabled` 开关、调色板、打包格式）。
勾选的屏会各自生成对应尺寸的图片；第一个勾选的是主屏。

| 屏 | 宽×高 | 打包格式 |
|---|---|---|
| GDEP073E01（当前，竖用） | 480×800 | `1byte_per_px` |
| 7.09" E6（以后） | 1200×1600 | `1byte_per_px` |
| 12.43" 133C（以后） | 1208×1600 | `2px_per_byte` |

六色调色板（索引=像素值）：`0黑 1白 2红 3黄 4蓝 5绿`，在 `SCREEN["palette"]` 里配置。

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

- 固件下载地址 = `http://<NAS_IP>:8765/static/inktime/<DOWNLOAD_KEY>/latest.bin`
  （`DOWNLOAD_KEY` 在 `config.py`，改后需同步改固件的 `DAILY_PHOTO_PATH_PREFIX`）
- 端口 8765 已在 compose 里映射；ESP32 能访问 NAS 该端口即可。

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
