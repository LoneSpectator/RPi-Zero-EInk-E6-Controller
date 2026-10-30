# RPi-Zero-EInk-E6-Controller

一个 Python 墨水屏控制程序：FastAPI + Pillow + SQLite，原生 HTML/CSS/JavaScript 控制台，无前端构建步骤。面向 **Raspberry Pi Zero 2 W + Waveshare 7.3 英寸 E6 六色电子纸（800×480）**，通过 ARM64 容器部署到目标树莓派。

- **概览**：设备系统时间、设备时区、电池电压与估算电量、刷新状态、最后成功下发画面、最近记录、即将执行的排程。
- **图片**：上传 JPG/PNG（12 MiB、2000 万像素以内），完整等比显示（白色留边）或居中裁剪铺满；自动处理 EXIF 方向、透明背景和六色抖动。
- **文本**：段落与混合样式、12–120 px 字号、粗体、斜体、左/中/右对齐、六种文字/背景颜色、五种限定字体；服务端排版预览，内容溢出明确报错。
- **内容库**：保存内容、搜索、按类型筛选、重复显示、删除、添加排程。预览不入库，保存或“确认并刷新”才入库，每次成功显示更新显示时间。
- **排程**：增删改查、启用/停用，指定日期的某一分钟或五字段 cron。SQLite 持久化，串行刷新，执行结果可查。

## 容器镜像与自动构建

- **GitHub 容器页面**：[rpi-zero-eink-e6-controller](https://github.com/LoneSpectator/RPi-Zero-EInk-E6-Controller/pkgs/container/rpi-zero-eink-e6-controller)
- **镜像地址**：`ghcr.io/lonespectator/rpi-zero-eink-e6-controller`
- **构建状态与日志**：[GitHub Actions · Container](https://github.com/LoneSpectator/RPi-Zero-EInk-E6-Controller/actions/workflows/container.yml)

推送到 `main` 或推送 `v*` 版本标签时，GitHub Actions 在 ARM64 runner（`ubuntu-24.04-arm`）上直接构建并发布仅供目标设备使用的 `linux/arm64` 镜像。不发布 amd64 或 armv7 镜像；宿主系统必须为 64 位 ARM。Pull Request 仅检查 ARM64 镜像能否构建，不运行模拟测试、不发布镜像；也可在 Actions 页面手动运行默认分支的工作流。

| 镜像标签 | 用途 |
| --- | --- |
| `latest` | `main` 最近一次成功发布的镜像 |
| `sha-<完整提交 SHA>` | 固定到某次构建的源码提交 |
| `1.2.3`、`1.2` | 推送 `v1.2.3` 等版本标签后生成 |

镜像构建成功只表示容器可以完成构建，不代表实体屏幕或电池功能已通过验收。工作流不运行模拟测试作为发布门槛，硬件验收按下文在目标树莓派上完成。

工作流使用 GitHub 自动提供的 `GITHUB_TOKEN` 发布，无需另设个人访问令牌。**首次发布后，若包仍为私有，仓库所有者需在上述容器页面的 Package settings → Change visibility 中设为 Public，才能匿名拉取。** 若仓库或组织限制 Actions 发布包，还需允许该工作流写入 Packages。构建成功及包公开前，镜像可能尚不可拉取；请先检查构建日志。继续保持私有时，使用有 `read:packages` 权限的凭据登录 GHCR 后拉取，勿把凭据写入 `.env` 或提交到仓库。

## 树莓派真机部署

目标环境为 **Pi Zero 2 W + 64 位 Raspberry Pi OS（arm64）**。直接拉取 GitHub Actions 发布的 `linux/arm64` 镜像，无需在设备上构建或编译依赖。首次拉取会下载驱动依赖和全部字体；amd64 和 32 位 armv7 不在发布范围内。

1. 按[微雪 Wiki](https://www.waveshare.net/wiki/RPi_Zero_PhotoPainter)连接设备，确认是 E6 六色版本。使用 `sudo raspi-config` 开启 **SPI** 和 **I²C**，按提示重启。
2. 确认宿主机有 `/dev/spidev0.0`、`/dev/gpiochip0` 和 `/dev/i2c-1`。先停止微雪测试程序、原有 cron/相框服务，确保没有其他程序占用这些引脚。
3. 在树莓派上安装 Docker Engine、Compose 插件和 Git，然后获取部署配置：

```bash
git clone https://github.com/LoneSpectator/RPi-Zero-EInk-E6-Controller.git
cd RPi-Zero-EInk-E6-Controller
cp .env.example .env
```

编辑 `.env`，设置非空 `WEB_PASSWORD`；需要从局域网电脑访问时，设置 `BIND_ADDRESS=0.0.0.0`。默认镜像为 `ghcr.io/lonespectator/rpi-zero-eink-e6-controller:latest`，可修改 `IMAGE` 固定到已发布的版本标签或 `sha-<完整提交 SHA>`。

拉取镜像并启动真机服务：

```bash
docker compose -f compose.yaml -f compose.hardware.yaml pull
docker compose -f compose.yaml -f compose.hardware.yaml up -d
docker compose -f compose.yaml -f compose.hardware.yaml logs -f
```

浏览器访问树莓派的 **8000 端口**，使用 `.env` 中的用户和密码登录（默认用户 `admin`）。默认只绑定 `127.0.0.1`；设置 `BIND_ADDRESS=0.0.0.0` 后才允许局域网访问。跨不可信网络访问时，在前面部署 HTTPS 反向代理。

硬件配置映射三个设备，不需要 `privileged: true`。容器默认 root，用于打开已映射的设备和写入数据目录。若改为普通用户运行，需自行配置设备组权限和数据目录权限。GPIO 采用 `lgpio`，无需在容器中伪造树莓派型号探测信息。

| 接口 | 配置 |
| --- | --- |
| 屏幕 | SPI0 CE0：`/dev/spidev0.0`，4 MHz，mode 0，内核控制 CS |
| 复位 / DC / BUSY | BCM 17 / 25 / 24 |
| 屏幕供电 | **BCM 27**，与该产品官方配置一致 |
| 电池监测 | I²C-1，INA219 默认 **0x43**，可用 `INA219_ADDRESS` 覆盖 |
| RTC | 由宿主系统负责校时，程序读取宿主系统时钟；不修改 DS3231 |

驱动来自[微雪官方产品仓库](https://github.com/waveshareteam/RPi_Zero_PhotoPainter)，固定来源提交见 [NOTICE](app/vendor/NOTICE.md)，保留上游许可声明；GPIO 容器适配独立实现。BUSY 有超时保护，异常会释放硬件资源。

首次真机验收：在图片页面上传测试图片，生成预览并确认刷新；等待概览显示刷新完成，然后核对物理屏幕的方向与颜色，再核对电池电压。**当前云端只完成模拟与容器测试，未进行实体屏幕、INA219 和 arm64 真机验收。** 电子纸刷新较慢，过程中闪烁属驱动刷新行为，不支持局部或视频刷新，请避免频繁刷新。

### “屏幕实时状态”的含义

屏幕正在刷新或已有任务等待刷新时，图片、文本和内容库的手动刷新按钮均禁用，完成后自动恢复；预览和仅保存内容仍可使用。提交前会重新检查状态，后端以同一事务检查并入队，忙碌时返回 HTTP 409，避免重复点击、多个页面或排程同时触发导致额外手动刷新。连接异常、无法确认状态时也暂停手动刷新。排程继续按原有规则串行执行。

控制台空闲时每四秒、刷新期间每秒读取服务状态，显示“待确认 / 刷新中 / 待机 / 刷新失败”、刷新队列，以及**最后成功下发的像素图**。该硬件没有屏幕像素回读或摄像头，无法确认断电、拔线或外部程序改屏后的实际可见画面。刷新失败或中断会明确提示实际画面可能未知；不会将排队或失败任务当作刷新成功。

电量沿用官方 UPS 示例的 `3.0–4.2 V` 线性估算并限制在 0–100%，不是精密 SOC；测不到时显示“不可用”，不会编造数字。估算不包含充电状态识别。

## 文本与字体

屏幕允许的颜色只有 `#000000`、`#ffffff`、`#ff0000`、`#ffff00`、`#0000ff`、`#00ff00`。服务端校验颜色、字体、字号、文档长度；粘贴按纯文本处理，通过工具栏添加样式。富文本传递为结构化段落及文字片段，不执行 HTML。六色量化后同一份 PNG 同时用于预览、保存和设备显示，物理色彩受光线及面板影响。

| 控制台字体 | 容器提供的字体 | 自定义字体路径变量 |
| --- | --- | --- |
| 黑体 | 文泉驿正黑（开源替代） | `FONT_HEITI` |
| 宋体 | AR PL UMing（开源替代） | `FONT_SONGTI` |
| 楷体 | AR PL UKai（开源替代） | `FONT_KAITI` |
| 思源黑体 | Noto Sans CJK SC（对应项目版本） | `FONT_SOURCE_SANS` |
| 思源宋体 | Noto Serif CJK SC（对应项目版本） | `FONT_SOURCE_SERIF` |

字形粗体和斜体由 Pillow 合成。编辑区使用浏览器字体，**生成后的像素预览是最终排版依据**。需要特定厂商的黑体/宋体/楷体时，可将合法授权 TTF/OTF 文件只读挂载到容器，用上述变量指定绝对路径。不内置或下载未经授权的商业字体。缺少字体会报错，不会悄悄替换为无法显示中文的西文字体。

## 排程规则

默认时区 **Asia/Shanghai**，由 `TZ` 决定，控制台按设备时区输入及显示日期，不依赖浏览器时区；数据库保存 UTC 时间戳。更改设备时区后应编辑并保存已有 cron 排程，使下次执行时间重新计算。请在宿主机启用 NTP 或配置 RTC 校时，容器不具备修改系统时间的权限。

| 表达式 | 行为 |
| --- | --- |
| `*/30 * * * *` | 每小时 00、30 分 |
| `0 8 * * *` | 每天 08:00 |
| `15 9 * * 1-5` | 周一至周五 09:15 |
| `0 12 1 * *` | 每月 1 日 12:00 |

采用标准五字段 cron（分、时、日、月、周；日与周同时指定时采用 cron 的 OR 语义），不支持秒字段或 `@hourly` 等别名。单次时间必须位于未来且秒为零。后台每秒检查到期任务，将任务与后续执行时间在同一数据库事务中保存，防止重复入队。

- 同一时间多个排程按队列依次显示，物理完成时间受刷新耗时影响，不保证整分钟瞬间完成。
- 容器重启：保留未开始的手动/定时刷新；刷新途中中断的任务标记失败，不自动重复刷，屏幕状态待确认。
- 停机/休眠期间的排程最多容忍两分钟延迟；超时记录为失败并跳过，循环排程计算下一个未来时间，不集中补播历史画面。
- 最多 20 个等待/进行中的任务；手动刷新只在队列空闲时接收；队列满时定时任务记录失败并跳过本次。
- 删除或停用排程只影响后续触发，已入队的任务继续执行。
- 被排程引用的内容（包含停用/已完成排程）或正在等待/刷新的内容不能删除，返回明确提示。删除当前内容不会清除物理屏幕，最后成功下发图单独保留。
- 必须单实例、单 Uvicorn worker；数据目录带进程锁。多副本部署不受支持。

## 数据与运维

`./data` 挂载为 `/data`，包含 SQLite 数据库、内容 PNG、最后成功下发图与实例锁。拉取和更新镜像不会清除这些数据。预览不落盘，图片保存的是屏幕分辨率的六色图，不保留原始上传文件，文本保留结构化文档。单份内容是不可变快照；需要修改时新建一份。

真机更新须继续使用同一组配置文件，保留设备映射：

```bash
git pull --ff-only
docker compose -f compose.yaml -f compose.hardware.yaml pull
docker compose -f compose.yaml -f compose.hardware.yaml up -d
```

保留已有 `.env` 和 `data/`，不要再次复制示例覆盖配置。修改 `IMAGE` 可切换到已发布的指定版本。

备份时先停止服务，复制整个 `data/`（包括可能存在的 SQLite WAL 文件），再启动。不要只备份运行中数据库的单个文件。

```bash
docker compose stop
# 备份 data/ 到你选择的位置后：
docker compose start
```

常用配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `IMAGE` | `ghcr.io/lonespectator/rpi-zero-eink-e6-controller:latest` | Compose 使用的镜像，可固定版本 |
| `DISPLAY_DRIVER` | `mock` | `mock` 或 `waveshare`；真机模式必须设置密码 |
| `DATA_DIR` | `/data` | 数据目录 |
| `TZ` | `Asia/Shanghai` | IANA 时区 |
| `WEB_USERNAME` / `WEB_PASSWORD` | `admin` / 空 | Basic 登录凭据，不写入源码 |
| `MOCK_REFRESH_SECONDS` | `2` | 模拟刷新时长 |
| `GPIO_CHIP` | `0` | GPIO 芯片编号；更改时同时更改 devices 映射 |
| `I2C_BUS` / `INA219_ADDRESS` | `1` / `0x43` | 电池传感器配置 |
| `EPD_BUSY_TIMEOUT` | `180` | 单阶段 BUSY 超时秒数，总硬件操作另有 240 秒上限 |

## 开发和测试

开发使用 Python 3.12；本地开发需安装上述五种中文字体（Debian/Ubuntu：`fonts-wqy-zenhei fonts-arphic-uming fonts-arphic-ukai fonts-noto-cjk`）。Docker 镜像已包含它们。

模拟模式仅用于调试排程、内容管理和接口逻辑，不验证 GPIO/SPI 接线、实际刷屏或 INA219 读数；生产部署使用上面的真机配置。以下本地检查是可选的软件回归检查，不作为 Actions 发布前置。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
DATA_DIR=./data DISPLAY_DRIVER=mock .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
# 另一终端：
.venv/bin/python -m pytest -q
```

测试覆盖上传校验、缩放/裁剪、透明与方向、六色输出、富文本、刷新成功与失败、单次与 cron 排程、错过任务策略、队列容量、并发手动刷新拦截、忙碌状态与完成/失败解锁、重启恢复、删除保护、登录、跨站请求和请求大小上限、硬件超时与电池读取协议。字体测试会遍历当前安装字体，完整五种字体需在容器中验证。

可选浏览器端到端检查需额外安装 `playwright` 和 Chromium，针对**全新数据目录的模拟实例**运行（会新增测试内容）：

```bash
.venv/bin/pip install playwright==1.64.1
APP_URL=http://127.0.0.1:8000 CHROMIUM_PATH=/usr/bin/chromium .venv/bin/python scripts/browser_smoke.py
```

刷新互锁的独立浏览器回归检查会自行启动临时模拟实例，不修改现有内容或排程：

```bash
CHROMIUM_PATH=/usr/bin/chromium .venv/bin/python -m scripts.browser_refresh_smoke
```

检查覆盖图片、文本、内容库的禁用状态，刷新时预览和保存、第二个标签页、断线重连以及刷新失败后的恢复。

主要目录：`app/main.py` API，`render.py` 排版与量化，`engine.py` 队列/排程，`hardware.py` 真机/模拟驱动与电池，`store.py` 数据库，`static/` 控制台。健康检查为 `/api/health`，状态为 `/api/status`；配置密码后 API 同样要求 Basic 登录。

需要从本地源码构建并运行容器时，显式叠加 `compose.build.yaml`：

```bash
docker compose -f compose.yaml -f compose.build.yaml up -d --build
# 真机本地构建则继续叠加硬件配置：
# docker compose -f compose.yaml -f compose.build.yaml -f compose.hardware.yaml up -d --build
```

本地构建使用 `photopainter:local`，不依赖 GHCR 镜像是否已发布。真机拉取部署使用 `compose.yaml` 与 `compose.hardware.yaml`；单独使用 `compose.yaml` 是开发用的模拟模式。

在生产镜像的依赖和字体环境中运行同一套测试（测试工具只安装在测试镜像中）：

```bash
docker build -t photopainter:local .
docker build -f Dockerfile.test -t photopainter:test .
docker run --rm photopainter:test
```

受限网络构建时需允许 Docker Hub、Debian 软件源和 PyPI；若环境提供 HTTPS 代理，按 Docker 标准方式传入 `HTTP_PROXY` / `HTTPS_PROXY` 构建参数。镜像支持可选 BuildKit secret `proxy_ca` 传入受信任的构建 CA 文件；保留 TLS、APT 签名和包校验，不关闭证书验证。

## AI 编码声明

本项目使用 **OpenAI Codex** 进行 AI 辅助开发。初始版本的应用代码、Web 控制台、容器配置、测试和文档主要由 AI 根据项目维护者提出的需求生成并迭代。此声明说明代码的开发来源，不表示已完成人工逐行审查。

初始实现已在云端通过 36 项自动化测试，以及浏览器交互、跨时区排程、实际分钟触发和容器重启恢复验证；**实体墨水屏刷新、INA219 电池读取和 arm64 真机运行尚未验收**。AI 生成的实现仍可能存在缺陷，部署前应结合硬件文档进行人工审查和真机验证。

微雪电子纸驱动属于引用的第三方代码，其来源、固定版本和许可说明见 [驱动声明](app/vendor/NOTICE.md)。本声明不改变项目的 [许可证](LICENSE)，也不改变第三方代码、依赖和字体各自的许可与署名要求。
