# 豆包语音输入 (Doubao Murmur) - Linux/SteamOS 版

使用豆包 ASR（自动语音识别）服务实现全局语音转文字输入。适用于 SteamOS Desktop Mode (Steam Deck) 和其他 Linux 发行版。

> **macOS 用户**: 请使用项目根目录的 macOS 版本。

## ✨ 功能

- ⌨️ **全局热键**: 右 `Alt` 可选“按键切换”或“按住说话”，`ESC` 取消（X11 桌面下任何应用均可用；Wayland 见 [下文](#-wayland--gnome-用户须知)）
- 🎮 **手柄一键语音输入**: 在 Steam Input 桌面布局中把手柄按键映射为右 Alt 即可（见下文）
- 📝 **实时转写**: 说话时文字实时显示在屏幕顶部悬浮条中（不抢焦点、不挡输入）
- 📋 **自动粘贴**: 识别结果自动粘贴到当前输入框；终端自动改用 `Ctrl+Shift+V`
- 🔐 **登录一次**: 通过内置 WebView 登录豆包，凭证持久化存储
- 🛎 **系统托盘**: 常驻托盘 🎤 图标，左键打开控制面板，右键菜单登录/退出（KDE 等支持 StatusNotifierItem 的桌面）
- ⌨️ **屏幕触摸键盘**: SteamOS 桌面模式下的可拖动、可缩放软键盘，专为掌机握持设计的**分体**与**左/右单手**布局（见下文）

## ⌨️ 屏幕触摸键盘

SteamOS 桌面模式自带的虚拟键盘不能移动、不能缩放，常挡住输入框。本项目内置了一个可拖动、可缩放的触摸软键盘，把按键注入到当前焦点窗口（不抢焦点），适合 Steam Deck 等掌机。

<p align="center">
  <img src="../docs/screenshots/keyboard_full.png" width="760" alt="屏幕键盘 - 全键模式">
</p>

- **打开方式**: 托盘菜单「⌨ 软键盘」，或全局快捷键 `Ctrl + Super + Shift`（三个修饰键同时按住）
- **布局模式**（顶部按钮循环切换）:
  - **全键**: 整块标准键盘，居中
  - **分体**: 左右两个半键盘分别贴住屏幕两侧、中间留空——掌机双手握持时两个拇指正好够到
  - **左手 / 右手**: 整块键盘压成手机宽度、吸附到屏幕最左 / 最右，单拇指即可输入
- **移动 / 缩放**: 拖动顶部条移动，右上角 `⤡` 拖拽缩放，或 `S / M / L` 一键预设；位置和模式会被记住
- **符号层**: `?123` 切换符号层；`Shift / Ctrl / Alt` 为吸附式（点一下临时生效、双击锁定），可打出 `Ctrl+C`、`Ctrl+Shift+V` 等组合键
- 纯 ASCII / 拉丁键盘——中文请用本项目的语音输入

<p align="center">
  <img src="../docs/screenshots/keyboard_split.png" width="820" alt="屏幕键盘 - 分体模式">
</p>

> 仅 X11 桌面（如 SteamOS 的 KDE Plasma）；按键注入依赖宿主机的 `xdotool`（SteamOS 自带）。

## 📋 系统要求

- **OS**: SteamOS 3 / Arch Linux / 其他支持 GTK4 的 Linux 发行版
- **音频**: PipeWire (SteamOS 默认) 或 PulseAudio
- **Python**: 3.10+
- **桌面环境**: KDE Plasma (推荐) 或 GNOME
- **显示协议**: **X11 会话开箱即用**；Wayland 会话需要额外配置且功能受限，见 [下文](#-wayland--gnome-用户须知)

## 🚀 安装

### Ubuntu 22.04: X11 源码安装

登录界面点击齿轮，选择 **Ubuntu on Xorg**。`echo "$XDG_SESSION_TYPE"` 应输出
`x11`。先安装 Ubuntu 官方更新（需要启用 `jammy-updates` / `jammy-security`）；
当前更新仓库提供 GTK4 版 WebKitGTK 6。不要用 GTK3 的 WebKit2 4.1 替代它。

```bash
sudo apt update
sudo apt install python3-venv python3-gi python3-gi-cairo \
  gir1.2-gtk-4.0 gir1.2-webkit-6.0 libportaudio2 xdotool xclip x11-utils
cd linux
/usr/bin/python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m doubao_murmur
```

使用系统 Python 是为了复用 Ubuntu 的 GI / GTK；`websockets`、`sounddevice`
和 `python-xlib` 由项目依赖安装进虚拟环境，不需要 PPA 或替换系统 Python。
如果 apt 找不到 `gir1.2-webkit-6.0`，请检查官方更新源或使用下文 Flatpak，
不要继续安装不完整的原生环境。

X11 下全局热键使用 XRecord，复制/粘贴可使用 `xclip` / `xdotool`；
无需加入 `input` 组，也不需要 `ydotool`。Ubuntu GNOME 的托盘需要启用
Ubuntu AppIndicators 扩展；即使托盘不可见，再启动一次应用也可打开控制面板。
录音仍需要可用的 PulseAudio / PipeWire 音频服务。

```bash
.venv/bin/python -m pytest tests/ -q
# 可选的隔离 X11 启动检查（不登录、不录音）：
sudo apt install xvfb
xvfb-run -a env DOUBAO_X11_SMOKE=1 .venv/bin/python -m pytest tests/test_x11_smoke.py -q
```

下面的 `.deb` 包仍面向 Ubuntu 26.04；22.04 请使用本节的虚拟环境或 Flatpak。

### Ubuntu 26.04: `.deb`

从源码构建并安装原生包：

```bash
sudo apt install dpkg-dev make
cd linux
make deb
sudo apt install ./dist/doubao-murmur_*_all.deb
```

只构建、不安装时运行 `make deb` 即可；产物在 `linux/dist/`。当前默认包名是
`doubao-murmur_1.5.1+wayland8_all.deb`，升级时会保留原有登录信息。

**GitHub 自动构建 / 下载**：打开仓库 **Actions → Ubuntu 26.04 Debian package**，
选择成功的运行，在 **Artifacts** 下载 `doubao-murmur-ubuntu-26.04-<commit>`。
解压后在该目录执行：

```bash
sha256sum -c SHA256SUMS
sudo apt install ./doubao-murmur_*_all.deb
```

工作流在 Linux 相关 PR、`master` 更新时运行；也可以在合并工作流后用
**Run workflow** 手动触发。CI 在真正的 Ubuntu 26.04 容器中安装 `.deb`、
解析 apt 依赖，并针对已安装的代码运行测试及隔离 X11 启动检查。
它不等同于真实 GNOME Wayland 下的录音/粘贴测试，也不会自动安装到你的电脑。
构建产物保留 30 天。

**长期保留到 Releases**：在已通过检查的提交上创建专用 Debian 发布 tag：

```bash
git tag deb-v1.5.1+wayland8
git push origin deb-v1.5.1+wayland8
```

`deb-v` 后的部分就是 Debian 包版本（可用 `DEB_VERSION=... make deb` 本地覆盖）。
后续发布使用新的、递增的版本，不复用已有 tag。
`deb-v*` 仅触发 Debian 发布，不触发原来的 `v*` macOS / Windows / Flatpak 发布。

安装不会删除 `~/.config/doubao-murmur/` 中已有的登录信息。Wayland 下首次安装后，
再完成一次输入权限和自动粘贴设置，然后注销并重新登录：

```bash
sudo usermod -aG input "$USER"
systemctl --user enable --now ydotool.service
```

### 方法一: Flatpak (推荐)

从 [Releases](../../../../releases) 页面下载 `doubao-murmur.flatpak`：

```bash
flatpak install --user doubao-murmur.flatpak
flatpak run com.doubao.Murmur
```

WebKitGTK 等依赖打包在 GNOME runtime 中，**不受 SteamOS 系统更新影响**。

自动粘贴依赖宿主机的 `xdotool`（SteamOS 自带）。

> **Wayland 桌面（GNOME 等）还需要把自己加进 `input` 组并重新登录**，
> 否则热键不工作。详见 [Wayland / GNOME 用户须知](#-wayland--gnome-用户须知)。

也可以从源码自行构建：

```bash
cd linux
flatpak install flathub org.flatpak.Builder org.gnome.Platform//49 org.gnome.Sdk//49
make flatpak-install
```

### 方法二: 直接运行 (开发模式)

```bash
# 1. 安装系统依赖（SteamOS 需要先 sudo steamos-readonly disable）
sudo pacman -S python python-pip python-gobject gtk4 webkitgtk-6.0 xdotool

# 2. 安装 Python 依赖
cd linux
python3 -m venv --system-site-packages .venv
.venv/bin/pip install websockets sounddevice python-xlib

# 3. 运行
PYTHONPATH=src .venv/bin/python -m doubao_murmur
```

> ⚠️ SteamOS 系统更新会清除 pacman 安装的包，届时需重装 `webkitgtk-6.0`。推荐用 Flatpak。

## 🎮 使用方法

1. 首次启动会弹出控制面板，点击 **登录豆包**，在 WebView 中完成登录
2. 登录后应用驻留在系统托盘（右下角 🎤 图标）
3. 在控制面板选择录音方式；默认是 **按键切换**，也可选 **按住说话**
4. 将光标放到任意输入框，按 **右 Alt**，屏幕顶部出现悬浮条，开始说话
5. 悬浮条实时显示识别文字（超长时自动滚动显示最新内容）
6. 按键切换模式下再按一次 **右 Alt**；按住说话模式下松开 **右 Alt**。文字会自动粘贴到输入框
7. 想中途放弃按 `ESC`，不复制也不粘贴

> 悬浮条本身只显示文字，没有按钮 —— 早期版本那个屏幕底部的 ⏹ 一键录音按钮
> 已在 v1.4.6 移除。开始和结束都只能用热键。

悬浮条可以拖动并记住位置。若更换显示器后位置不合适，可从托盘菜单或
控制面板选择 **重置悬浮框位置**。

### 网络 / 识别状态

悬浮条第二行显示实际语音识别链路的状态：获取连接参数、连接服务、
等待首个响应，以及最近一次服务响应距今的毫秒数（ms）。空识别结果也算服务响应；
“没有文字”不等于断网，状态栏不会估算网速或给出无依据的网络质量评分。

启动阶段（参数获取和连接合计）最多等待 8 秒。连接完成前停止录音时，
已录音频仍保留到原启动期限；连接成功后再等待最终结果。
连接失败、中断、服务错误、麦克风启动失败和空结果会显示具体提示，
释放资源后保留 4 秒；此时可直接开始下一次录音，不必等待提示消失。
连接中断时已有文字会暂时保留供查看，但不会作为成功结果自动粘贴。
登录失效仍会提示重新登录，普通网络错误不会清除登录凭证。

### 快捷键

| 快捷键 | 功能 |
|--------|------|
| 右 Alt 键 | 按键切换，或按住说话（可在控制面板选择） |
| ESC 键 | 取消当前录音（不粘贴） |

### 🎮 手柄一键语音输入 (Steam Deck / 掌机)

桌面模式下 Steam 接管了手柄，原生按键事件不会透传，但可以让 Steam 把手柄按键转成键盘键：

1. 打开 **Steam → 设置 → 控制器 → 桌面布局 → 编辑**
2. 把 **R3（右摇杆按下）** 或任意顺手的按键 → 添加命令 → **键盘 → 右 Alt**
3. （可选）把 **B 键** → **键盘 → Escape**，用于取消录音

之后在任何应用里按 R3 即可开始/结束语音输入。应用通过 X11 层（XRecord）监听按键，
Steam 注入的按键和物理键盘都能识别，无需额外权限。

## ⚠️ Wayland / GNOME 用户须知

本项目最初是为 SteamOS 的 KDE Plasma（X11）写的，**在 Wayland 会话下功能受限**。
Fedora Silverblue / Bluefin、Ubuntu 22.10+、Fedora Workstation 等默认 GNOME Wayland
的系统都属于这一类。

**为什么右 Alt 没反应**

- 默认的按键监听走 X11 的 XRecord 扩展。Wayland 会话下应用跑在 XWayland 里，
  XRecord 只能看到送进 XWayland 的按键 —— 焦点在原生 Wayland 窗口时，按键
  压根不经过 XWayland，所以监听不到。
- 后备的 evdev 监听读 `/dev/input/event*`，需要额外授权（见下）。

**让热键能用**

```bash
# 1. 加入 input 组（读 /dev/input 需要），之后必须注销重新登录
sudo usermod -aG input "$USER"

# 2. Flatpak 用户：v1.5.0 之后的包已内置 --device=input，无需操作。
#    v1.5.0 及更早的版本手动放开：
flatpak override --user --device=input com.doubao.Murmur
```

重新登录后启动应用，日志里出现 `evdev listener active` 就说明通了。

**自动粘贴仍然不行**

粘贴依赖 `xdotool`（X11 专用）或 `ydotool`（需要 `ydotoold` 常驻）。
Wayland 下 xdotool 粘不进原生窗口。Ubuntu 26.04 安装的是用户级
`ydotool.service`，可这样启用：

```bash
sudo apt install ydotool
systemctl --user enable --now ydotool.service
```

其他发行版的服务名称和运行方式可能不同，请以发行版的软件包说明为准。

**没装 ydotool 也能用** —— 剪贴板复制成功但无法自动粘贴时，应用会发送桌面通知，
可手动 `Ctrl+V`（终端里是 `Ctrl+Shift+V`）。如果所有复制方式都失败，则不会发送
粘贴按键，以免误粘贴剪贴板中原有的内容。通知能否显示取决于桌面通知服务。

文字交付在后台按顺序执行，不阻塞录音界面。复制和粘贴之间的后端工具仍可能
超时；退出会取消排队的交付和后续步骤，但已经开始的系统操作无法撤回。
自动粘贴使用发送按键时的焦点窗口；工具成功仅表示按键请求已发出，不能保证
目标应用实际接受了文字。

> Fedora Silverblue / Bluefin 这类原子系统装 ydotool 要 `rpm-ostree install` 加重启，
> 代价较大，建议直接用手动粘贴。

**托盘图标看不到**

原版 GNOME 不支持 StatusNotifierItem，需要装
[AppIndicator 扩展](https://extensions.gnome.org/extension/615/appindicator-support/)。
Bluefin 等定制版默认带这个扩展。没有托盘时，再执行一次
`flatpak run com.doubao.Murmur` 会唤出控制面板（单实例应用）。

**彻底的 Wayland 支持**（全局热键走 XDG GlobalShortcuts portal 或 libei、
粘贴走 wtype/libei）尚未实现，欢迎 PR。

## 🏗 项目结构

### 桌面适配开发边界

不要按 Ubuntu 版本分叉业务逻辑；22.04 / 26.04 均可能运行 X11 或 Wayland。
`desktop.is_x11_session()` 是宿主会话策略入口，优先使用 `XDG_SESSION_TYPE`，
缺失时才检查显示环境变量。`GDK_BACKEND=x11` 只表示本应用使用 XWayland，
不代表整个桌面是 X11。

- `HotkeyManager.start()`：X11 上优先仅运行 XRecord；失败时保留 evdev 后备。
  Wayland 上仍同时运行可用的 XRecord / evdev，覆盖原生 Wayland 窗口。
- `TextDelivery`：调用者只提交文本，由一个后台工作线程维护完整交付顺序，
  返回粘贴请求已发送 / 仅复制成功 / 复制失败。GTK 剪贴板后备操作和结果回调在
  GTK 主线程执行；录音状态不等待桌面工具完成，退出时关闭交付队列。
- `PasteHelper`：负责具体剪贴板和按键后端，不管理工作线程或录音状态。
  X11 优先 `xdotool`，失败后保留其他可用后端；Wayland 保留
  uinput / ydotool 等现有顺序。新增后端只需调整对应的选择流程和测试，
  不需要修改录音状态机。
- `ui/windowing.py`：继续负责 GTK 窗口提示和实际 surface 能力判断；
  不把窗口后端误当作宿主会话。
- 录音、ASR、凭证和配置文件不依赖发行版或桌面会话；WebKit 登录统一使用 GTK4 版 6.0。
- `session_status.py`：不依赖 GTK 的不可变诊断快照，不复制录音状态。
  `ASRClient` 只报告传输事实；`TranscriptionManager` 统一管理诊断、启动期限、
  资源清理和提示保留；`AppState` 发布变化，`Overlay` 订阅并渲染。
  停止发送音频不等于断开 WebSocket，不能用 `ASRClient.is_connected` 推导界面状态。
  新增平台界面应消费这套状态契约，不新增网络状态管理器或按发行版分叉规则。

更改会话判定时测试 `test_desktop.py`；更改后端优先级时测试 hotkey / paste
用例。真实 X11 启动检查单独在 Xvfb 中运行，避免单元测试读取键盘或注入按键。

```
linux/
├── src/doubao_murmur/
│   ├── __main__.py          # 入口点
│   ├── app.py               # 主 GtkApplication
│   ├── app_state.py         # 应用状态管理
│   ├── config.py            # 配置常量
│   ├── asr_client.py        # WebSocket ASR 客户端
│   ├── audio_capture.py     # 麦克风音频采集
│   ├── transcription.py     # 录音状态机
│   ├── params_store.py      # 凭证持久化
│   ├── hotkey/              # 输入管理
│   │   ├── manager.py       # 热键管理器（统一封送到 GTK 主线程）
│   │   ├── x11_listener.py  # X11/XRecord 全局键监听（主用）
│   │   └── evdev_listener.py # /dev/input 监听（非 X11 后备）
│   ├── ui/                  # 用户界面
│   │   ├── overlay.py       # 录音悬浮窗
│   │   ├── windowing.py     # X11 置顶/定位/无焦点处理
│   │   ├── tray_icon.py     # 系统托盘 + 控制面板
│   │   ├── sni_tray.py      # StatusNotifierItem/dbusmenu 纯 DBus 实现
│   │   └── login_window.py  # WebView 登录
│   ├── paste/               # 剪贴板/粘贴
│   │   └── paste_helper.py
│   └── resources/           # JS 注入脚本
├── flatpak/                 # Flatpak 打包
├── tests/                   # 单元测试
└── run.sh                   # 开发启动脚本
```

## 🔧 配置

登录凭证存储在 `~/.config/doubao-murmur/asr_params.json`，录音方式存储在
`~/.config/doubao-murmur/settings.json`。

删除此文件可以强制重新登录：

```bash
rm ~/.config/doubao-murmur/asr_params.json
```

## ❓ 常见问题

### 启动后什么都没出现
- 已登录时应用驻留系统托盘，查看右下角是否有 🎤 图标，按右 Alt 即可录音
- 点击托盘图标或再启动一次应用（单实例）可打开控制面板
- 桌面不支持托盘（如原版 GNOME）时没有图标，功能不受影响
- 界面上**没有**麦克风按钮，使用右 Alt 开始录音

### 按右 Alt 没反应
- Wayland 会话（GNOME 默认）需要额外配置，见 [Wayland / GNOME 用户须知](#-wayland--gnome-用户须知)
- X11 会话下从终端启动看日志：`flatpak run com.doubao.Murmur`，
  应该出现 `X11 key listener active` 或 `evdev listener active`，两条都没有就是监听没起来
- 键盘布局没有右 Alt（只有 AltGr）时也能触发，但如果 AltGr 被输入法占用则可能失效

### 没有声音/录音失败
- 检查麦克风权限: `arecord -l` 查看可用设备
- 检查 PipeWire: `pactl info` 确认音频系统正常
- 测试 Python 音频: `python3 -c "import sounddevice; print(sounddevice.query_devices())"`

### 自动粘贴不工作
- 确认宿主机有 `xdotool`（SteamOS 自带；其他发行版 `sudo pacman -S xdotool`）
- 文字始终会复制到剪贴板，粘贴失败时可手动 `Ctrl+V`（终端 `Ctrl+Shift+V`）

### 手柄按键不触发
- 确认是在 **桌面布局**（Desktop Layout）里设置的映射，不是某个游戏的布局
- 确认映射的目标是键盘的 **右 Alt**（Right Alt），不是左 Alt

### WebView 无法加载
- 安装 WebKitGTK: `sudo pacman -S webkitgtk-6.0`
- 确认网络连接正常

## 📝 开发

```bash
# 安装开发依赖
pip3 install --user -e ".[dev]"

# 运行测试
make test

# 运行应用
make run
```

## 📄 许可证

MIT License - 详见项目根目录的 LICENSE 文件。

## 🔗 相关链接

- [macOS 版本](../README.md)
- [豆包官网](https://www.doubao.com)
- [SteamOS](https://www.steamdeck.com)
