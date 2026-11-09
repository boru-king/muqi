# 目憩（Mù Qī）

> 目为眼，憩为歇息 —— 「目憩片刻，心自悠然」。

一款 Windows 桌面端的智能用眼提醒工具。通过 **键鼠活动检测 + 摄像头人脸检测** 的混合策略判断是否真正在用眼，避免传统定时器在「看视频、看论文、看文档」等几乎不动键鼠的场景下误判。

## 获取方式

| 你的目的 | 从哪里拿 |
| --- | --- |
| **直接使用** | 项目 **Release** 页面下载 `目憩.exe`，无需安装 Python，双击即可运行 |
| **阅读 / 修改源码、自行打包** | 克隆本仓库 |

> 分工：**仓库放源码与配置，Release 放可执行程序**。`dist/目憩.exe` 不入库，只在发布时作为 Release 附件上传。

---

## 功能特性

| 能力 | 说明 |
| --- | --- |
| 智能计时 | `GetLastInputInfo` 每秒检测键鼠空闲；空闲达阈值后按设定间隔启动摄像头抽检，检测到人才继续累计用眼时间 |
| 强制休息 | 达到提醒周期后全屏锁定桌面并自动开始休息倒计时；可「取消休息」提前恢复，或等倒计时结束自动恢复。休息期间**完全不计时** |
| 摄像头隐私 | 每次检测「打开 → 捕帧 → 检测 → `release()`」，画面只存在于内存，**不落盘、不上传、不缓存**，只返回「是否有人」的布尔结果；默认开启，可随时关闭 |
| 系统托盘 | 后台常驻、不弹主窗口；右键查看状态/今日累计，暂停/恢复、手动休息、设置、退出 |
| 设置界面 | 全部参数可视化编辑，JSON 持久化，保存后**热更新**无需重启；含隐私说明与数据清理入口 |
| 数据统计 | SQLite 记录每日累计用眼时长与休息次数；周/月图表（tkinter Canvas 自绘，零额外依赖） |
| 数据清理 | 手动 30/90 天前、自定义起止日期、清除全部；自动保留最近 N 天；**清理前二次确认**，清理后 `VACUUM` 回收空间 |
| 锁屏/休眠 | `OpenInputDesktop` 轮询 + 墙钟跳变兜底，锁屏/注销/UAC/休眠时停止一切检测与计时 |
| 提醒方式 | 弹窗（强制）、托盘气泡通知、系统提示音，可多选 |
| 单实例 | Windows 命名 Mutex 防多开 |

---

## 运行环境

- Windows 10 / 11
- Python 3.8+，**必须是带完整 Tcl/Tk 的 CPython**（本项目 `.venv` 使用 uv 的 CPython 3.12.14，Tk 8.6 开箱可用）
- 可选：一个可正常打开的摄像头；没有摄像头时程序会回退为纯键鼠模式并提示

> 坑提示：某些精简版 Python 能 `import tkinter` 但创建 `Tk()` 时报 `Can't find a usable init.tcl`；若遇到，请换用官方安装包或 uv 的 python-build-standalone。

---

## 快速开始（开发环境）

```bat
:: 1. 创建独立虚拟环境（不要使用全局环境；选带完整 Tcl/Tk 的解释器）
py -3.12 -m venv .venv
:: 或用 uv：uv venv --python 3.12 .venv

:: 2. 安装依赖
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 3. 运行
.venv\Scripts\python.exe main.py
```

启动后**不会弹出主窗口**，只在系统托盘出现图标。退出请从托盘右键「退出」，以便把最后不足 30 秒的统计落盘。

首次运行会自动生成 `config/settings.json`；也可先复制模板：

```bat
copy config\settings.example.json config\settings.json
```

---

## 打包为独立 exe

```bat
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean muqi.spec
```

产物：`dist\目憩.exe`（onefile、windowed、自带图标，约 69 MB）。

`muqi.spec` 做了两件关键事：

1. 用 `SPECPATH` + `cv2.data.haarcascades` **动态定位**资源，把 `assets/icon.ico` 与 Haar 级联 XML 收进包内 `assets/`（对应 `config/default.py` 中 frozen 状态下的 `RESOURCE_DIR`），不写死本机路径；`collect_data_files("cv2")` 保留 OpenCV 自带数据。
2. 声明 `pystray._win32`、`PIL.ImageTk`、`cv2.data`、`winsound` 等 hidden imports。

打包后目录约定（均写在 **exe 所在目录**，可写）：

```
目憩.exe
config/settings.json    用户配置（首次运行生成）
data/stats.db           SQLite 统计
data/muqi.log           运行日志（滚动 2 MB × 3）
assets/reminders/       用户自定义提醒图片
```

不要把这些目录放进只读位置（如 `C:\Program Files`），否则配置与统计无法写入。

### 发布到 Release

1. **同步版本号**：`config/default.py` 的 `APP_VERSION`、`pyproject.toml` 的 `version`，与 Release tag 保持一致（建议 `v1.0.0`）。
2. **干净构建**：删除旧的 `build/`、`dist/` 目录后执行上面的 PyInstaller 命令。
3. **本机验证**：双击 `dist\目憩.exe` → 托盘出现 → 右键「设置」改一项并保存 → 右键「退出」；确认 `dist\config\settings.json` 与 `dist\data\stats.db` 正常生成、`dist\data\muqi.log` 无 `CRITICAL`。
4. **生成校验和**（Windows 自带）：

   ```bat
   certutil -hashfile "dist\目憩.exe" SHA256
   ```

   把结果写成 `SHA256SUMS.txt`，与 exe 一起作为 Release 附件，方便下载者校验完整性。
5. **打 tag 并上传**：

   ```bat
   git tag v1.0.0
   git push origin v1.0.0
   ```

   在仓库 Release 页面新建 Release，上传 `目憩.exe` 与 `SHA256SUMS.txt`。Release 说明建议包含：版本号、变更摘要、支持的 Windows 版本、SHA256、首次运行会生成哪些目录。

> CI 自动构建上传 Release 目前**未配置**，发布是手动流程；如需自动化请按所用平台补充 workflow。

---

## 仓库内容约定

| 位置 | 内容 | 是否入库 |
| --- | --- | --- |
| `main.py`，`config/` `core/` `ui/` `data/` 下的 `.py` | 源码 | ✅ |
| `requirements.txt`、`muqi.spec`、`pyproject.toml` | 依赖 / 构建 / 工具配置 | ✅ |
| `.gitignore`、`.gitattributes`、`.editorconfig` | 仓库规范 | ✅ |
| `README.md` | 项目文档 | ✅ |
| `项目需求文档：目憩（Mù Qī）.md` | 需求规格（仅本地留存） | ❌ |
| `config/settings.example.json` | 配置模板 | ✅ |
| `assets/icon.ico`、`assets/reminders/.gitkeep` | 图标与目录占位 | ✅ |
| `dist/目憩.exe`、`build/` | 打包产物与中间文件 | ❌（exe 上传 Release） |
| `.venv/` | 虚拟环境 | ❌ |
| `config/settings.json` | 用户实际配置（可能含本地图片路径等隐私） | ❌ 首次运行自动生成 |
| `data/*.db`、`data/*.log*` | 统计数据与日志 | ❌ |
| `assets/reminders/*`、`assets/*.png` 等 | 用户自定义提醒图片 | ❌ |

`.gitattributes` 统一仓库内文本行尾为 LF、Windows 脚本为 CRLF，并把 `*.ico` / `*.exe` / `*.db` 等标记为二进制，避免 Git 误做文本 diff 或换行转换。

---

## 配置项

`config/settings.json`（**不入库**），也可在设置界面修改；字段说明见 `config/default.py` 与 `config/settings.example.json`：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `reminder_interval_minutes` | 60 | 用眼提醒间隔；提醒在累计 `N、2N、3N…` 分钟处触发 |
| `rest_duration_minutes` | 15 | 强制休息时长 |
| `idle_threshold_seconds` | 60 | 键鼠空闲多久后启动摄像头抽检 |
| `camera_check_interval_seconds` | 60 | 摄像头抽检间隔 |
| `camera_enabled` | `true` | 是否启用摄像头检测 |
| `reminder_text` | 该休息了… | 自定义提醒文字 |
| `reminder_image` | 空 | 自定义图片路径；配置后**裁剪铺满全屏作为背景**并叠暗化遮罩，文字与倒计时浮在图上；留空则纯深色背景 |
| `animation_enabled` / `animation_type` | `true` / `fade` | 弹窗动画开关与类型（`fade` / `zoom` / `blink`） |
| `reminder_methods` | `["popup"]` | 提醒方式，可多选 `popup` / `notification` / `sound` |
| `auto_cleanup_enabled` | `false` | 自动清理开关 |
| `auto_cleanup_keep_days` | 90 | 自动清理保留天数 |

修改提醒间隔后，下一次提醒阈值会按 `interval × n` 重新对齐（不会立刻弹窗，也不会把提醒无限推迟）。

---

## 数据清理与隐私

- 手动清理：设置 →「数据与隐私」→ 30 天前 / 90 天前 / 自定义起止 / 全部。每次都会弹出**二次确认**，显示范围与将删除行数；确认后才删除并执行 `VACUUM`。
- 自动清理：开启后于**程序启动时 + 每日首次写库时**检查，同一天只执行一次；不弹确认，仅写日志与托盘提示。
- 隐私：摄像头只使用 Haar Cascade 判断「屏幕前是否有人」，**不进行人脸识别**；不保存、不上传任何画面；无网络请求。

---

## 项目结构

```
muqi/
├── main.py                       程序入口（日志 / 单实例 / 启动）
├── muqi.spec                     PyInstaller 打包配置
├── requirements.txt              运行依赖
├── pyproject.toml                项目元数据 + Ruff 配置
├── .gitignore / .gitattributes / .editorconfig
├── README.md
├── config/
│   ├── default.py                默认配置、类型契约、路径常量（含 frozen 资源分离）
│   ├── manager.py                JSON 读写、校验、原子写、变更回调
│   ├── settings.example.json     配置模板（入库）
│   └── settings.json             用户配置（运行时生成，不入库）
├── core/
│   ├── state.py                  状态枚举、事件、状态快照
│   ├── detector.py               键鼠空闲检测（32 位 tick 回绕处理）
│   ├── camera.py                 摄像头人脸检测（每次检测完立即释放、不落盘）
│   ├── lockscreen.py             锁屏 / 休眠检测（OpenInputDesktop + 墙钟跳变）
│   ├── timer.py                  用眼 / 休息状态机（纯逻辑）
│   ├── sound.py                  winsound 声音提醒
│   └── app.py                    应用编排（1 秒主循环、事件分发、落盘、接线）
├── ui/
│   ├── tray.py                   系统托盘
│   ├── settings.py               设置界面 + 清理二次确认
│   ├── reminder.py               强制休息全屏窗口（Canvas 浮层）
│   ├── rest_countdown.py         倒计时模型与 Canvas 渲染器
│   └── charts.py                 周 / 月统计图表
├── data/
│   ├── stats.py                  SQLite 访问层
│   ├── cleanup.py                清理业务编排
│   └── stats.db                  运行时生成（不入库）
└── assets/
    ├── icon.ico                  应用图标
    └── reminders/.gitkeep        目录占位；用户图片不入库
```

---

## 开发者工具（可选）

```bat
.venv\Scripts\python.exe -m pip install ruff
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
```

`pyproject.toml` 固定了 `target-version = "py38"`，避免工具建议使用 3.9+ 语法而破坏 Python 3.8 兼容性。

> 目前仓库**尚未建立自动化测试目录**（`tests/`），这是已知的工程化缺口，见下方「已知限制」。

---

## 已知限制

- 多显示器环境：全屏提醒窗口只覆盖**主屏**。
- 锁屏检测采用 `OpenInputDesktop` 轮询，无法区分锁屏 / 注销 / 切换用户 / UAC 提权框；UAC 弹窗会短暂被判定为「离开」。
- Win / Alt+Tab / Ctrl+Alt+Del 由系统处理，普通程序无法拦截（刻意不安装全局键盘钩子）。
- 代码按 Python 3.8 语法编写，但**实际验证只在 Python 3.12.14 + Windows 10 19045** 上进行；exe 尚未在其他 Windows 电脑上实测，VC++ 运行库依赖也未验证。
- 尚未建立可回归的自动化测试套件；设置界面「弹窗」复选框当前不起开关作用（强制休息窗口始终显示）。

---

## 常见问题

**Q：启动后没看到窗口？**
正常，程序只在系统托盘常驻。右键托盘图标可打开设置或退出。

**Q：克隆仓库后没有 `config/settings.json`？**
正常，它不入库（避免把本地配置与个人图片路径传上去）。程序首次运行会自动生成默认配置；也可复制 `config/settings.example.json` 并改名为 `settings.json`。打包 exe 同理，配置生成在 exe 所在目录。

**Q：摄像头灯每隔一会儿亮一下？**
这是抽检。每次只开 1~2 秒，检测完立即 `cap.release()`；不保存任何画面。可在设置里关闭。

**Q：休息弹窗没铺满屏幕 / 多显示器？**
窗口用 Win32 `SetWindowPos` 按主屏物理尺寸强制铺满；多显示器环境目前只覆盖主屏。

**Q：自定义图片只显示中间一块 / 被裁掉了一部分？**
已改为全屏背景模式：`ImageOps.fit` 居中裁剪铺满整屏（保持比例、不拉伸变形），叠深色半透明遮罩保证文字可读；标题、提醒文字、倒计时与进度条都画在 Canvas 上（背景透明），只有「取消休息」按钮是实体控件。图片比例与屏幕差得越大，裁掉的边缘越多，属预期行为。

**Q：程序提示「已在运行中」？**
单实例保护生效，请从托盘退出旧实例后再启动。

**Q：日志在哪？**
`data/muqi.log`。打包成无控制台 exe 后，这是唯一的诊断入口。

**Q：exe 占用内存到 150 MB 以上？**
首次摄像头人脸检测时 OpenCV 会一次性初始化约 134 MB 的工作区并缓存复用，之后稳定不再增长，属正常现象，不是内存泄漏。摄像头设备与图像帧每次检测完都会释放、不落盘。
