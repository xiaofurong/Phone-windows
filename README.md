# PC Control — 局域网远程控制终端

把一台 Windows 电脑变成可被手机 / Kindle / 任意浏览器远程控制的"终端"：
鼠标、键盘、音量、显示器输入源切换、亮度、缩放、应用快捷启动，全部通过网页搞定。
原生 Windows 输入注入（非模拟键），跨屏、跨权限窗口（含高完整性托盘菜单）均可控。

## 架构

```
┌────────────┐   HTTP/JSON    ┌──────────────────┐   win32 SendInput   ┌──────────┐
│ 手机/Kindle │ ─────────────▶ │  agent.py (Win)  │ ──────────────────▶ │  本机系统 │
│  /网页      │                │  :8765 (token)   │                     │  鼠标/键 │
└────────────┘                └──────────────────┘                     └──────────┘
                                     ▲
┌────────────┐   relay             │
│ fnOS Docker │ ───────────────────┘  (hub.py 转发到 PC_AGENT_URL)
│ hub :8080   │
└────────────┘
```

三部分：

| 部分 | 位置 | 作用 |
|------|------|------|
| **agent** | 仓库根 `agent.py` + `run.bat` | Windows 上的输入代理，常驻后台，监听 `0.0.0.0:8765` |
| **hub**   | `hub/` | fnOS / Docker 上的中继 + 控制面板前端（PWA），把请求转发给 agent |
| **kindle**| Kindle 设备 `/f/extensions/pc-control/` | KUAL 扩展，打开控制页（本地无副本，见下方"Kindle 部署"） |

## 目录结构

```
pc-control/
├── agent.py              # 核心：Windows 输入注入（鼠标/键盘/滚轮/音量/显示器）
├── run.bat               # 自提权看门狗：以管理员(ELEVATED)常驻，3s 保活
├── install_task.bat      # 注册计划任务 PCControlAgent（开机免 UAC 自启）
├── start_agent_hidden.vbs# 隐藏窗口启动 run.bat
├── check_integrity.py    # 诊断：检查进程 UIPI 完整性级别（排查右键失灵）
├── ccd_toggle.py         # CCD 显示器开关工具
├── config.example.json   # 配置模板（复制为 config.json 后改本机值）
├── hub/                  # fnOS 控制面板
│   ├── hub.py            # 中继服务，转发到 PC_AGENT_URL
│   ├── index.html        # 控制面板主页面（手机端）
│   ├── app.py            # 占位
│   ├── Dockerfile        # fnOS / Docker 部署
│   ├── make_icons.py     # PWA 图标生成
│   └── public/
│       ├── kindle.html   # KOReader 风格 Kindle 控制页（含触控板）
│       ├── manifest.webmanifest
│       ├── sw.js
│       └── icons/
└── .gitignore
```

> 注：`config.json`、`*.log`、`probe*.py`、`*.apk/*.exe/*.zip` 等已加入 `.gitignore`，不会上传。

## 一、Windows agent 部署（核心）

1. 安装依赖（Python 3.11+）：
   ```bash
   pip install pywin32          # 实际依赖 ctypes + 系统 dll，通常无需额外包
   ```
2. 准备配置：把 `config.example.json` 复制为 `config.json`，改 `token` 和 `apps` 路径为本机值。
3. 启动（**必须管理员**，否则高完整性软件托盘菜单右键会失灵——见下方"已知坑"）：
   - 双击 `run.bat` → 弹 UAC 点"是"；或
   - 双击 `install_task.bat` 注册开机自启（同样点一次 UAC，之后重启自动以管理员运行）。
4. 验证：`http://127.0.0.1:8765/status`（需带 `X-Token` 头）应返回 `ok`。

| 端点 | 说明 |
|------|------|
| `POST /vinput/mouse` | 鼠标移动/点击/滚轮（`action`: move/click/down/up，`button`: left/right/middle，`delta`: 滚轮细粒度） |
| `POST /vinput/key`   | 键盘输入 |
| `POST /vinput/wheellines` | 设置系统滚轮行数 |
| `POST /system/volume` | 音量 |
| `POST /display/input` | 显示器输入源切换（DDC/CI） |
| `GET  /status` | 状态 |

所有写操作需带请求头 `X-Token: <你的 token>`。

## 二、控制面板（fnOS / Docker）

```bash
cd hub
docker build -t pc-control-hub .
docker run -d -p 8080:8080 \
  -e PC_AGENT_URL=http://<PC局域网IP>:8765 \
  -e HUB_TOKEN=<你的token> \
  pc-control-hub
```
浏览器打开 `http://<fnOS_IP>:8080` 即控制面板。手机可添加为 PWA（加到主屏）。

## 三、Kindle 部署（KUAL 扩展）

Kindle（越狱 + KUAL，固件 5.19.2 实测）上需建扩展目录 `/f/extensions/pc-control/`：

- `config.xml` — KUAL 注册
- `menu.json` — 菜单项
- `bin/open.sh` — 启动浏览器并注入控制页 URL

`open.sh` 关键逻辑（5.19.2 无 `mesquite`，改用 `lipc` + `appmgrd`）：

```sh
# 先启动浏览器，等 lipc 就绪，再注入 URL
appmgrd start app://com.lab126.browser
sleep 2
lipc-set-prop com.lab126.browser url "http://<fnOS或PC_IP>:8080/kindle.html"
```

控制页用 `hub/public/kindle.html`（已做老 WebKit 兼容：ES5 + XHR + setTimeout，无 fetch/rAF/Grid）。
本仓库**未包含** Kindle 上的 KUAL 文件副本，部署时从设备拷贝或按上面对照重建即可。

## 四、换电脑 / 迁移

1. `git clone <本仓库>`。
2. 进入 `pc-control`，按"一、Windows agent 部署"装依赖、配 `config.json`（从 `config.example.json` 改本机路径）。
3. 部署 `hub/` 到新 fnOS / Docker（改 `PC_AGENT_URL`）。
4. Kindle 重新装 KUAL 扩展（见上）。
5. 所有设备 `token` 保持一致即可互通。

## 已知坑

- **右键高完整性软件（如爱思助手/i4Tools）触控板失灵**：根因是 UIPI 完整性隔离——
  agent 若以普通用户运行，其 `SendInput` 注入会被系统静默拦截，无法送达管理员进程的托盘菜单，
  只有物理鼠标不受限。解决：**agent 必须以管理员(高完整性)运行**（用 `run.bat`/`install_task.bat` 提权）。
  `check_integrity.py` 可复验：提权后应能正常打开 i4Tools 进程（不再是 ACCESS_DENIED）。
- **视频全屏下虚拟光标消失**：agent 用 `SendInput` 硬件绝对路径（非 `SetCursorPos` 软设），保证光标在视频画面之上可见。
- **DDC/CI 半死态**：若显示器不响应输入源切换，先关掉 Twinkle Tray 等会抢 I2C 总线的软件（移除其对 0x60/0xD6 的监控）。

## 安全提示

`config.json` 里的 `token` 是控制平面的访问密钥，**请勿提交到公开仓库**。上传前已用 `config.example.json` 脱敏。
