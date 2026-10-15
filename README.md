# iPad Bridge

用 Windows 电脑**自带的蓝牙**，把电脑模拟成一套蓝牙键盘鼠标连到 iPad 上：
鼠标推过屏幕边缘就"穿越"到 iPad，直接操作 iPad 自己的 App；往回推或按快捷键就回到电脑。
不需要额外硬件，iPad 上也不用装 App。

## 要求

- Windows 10 2004（build 19041）及以上，蓝牙适配器支持 LE 外设（Peripheral）模式
  （启动时会自动检测，不支持会直接提示）
- Python 3.10+（开发时用的是 3.14）
- iPadOS 13.4 及以上（系统原生支持鼠标指针）

## 安装

```bat
setup.bat                      :: 用 py / python 创建 .venv 并安装依赖
setup.bat C:\Python314\python.exe   :: 或者指定某个 python
```

## 使用

- `start.bat`：后台运行，只在托盘显示图标（平时用这个）
- `run.bat`：带控制台窗口运行，能实时看到日志（调试用，Ctrl+C 退出）

托盘图标颜色：灰色 = 等待 iPad 连接，蓝色 = iPad 已连接、键鼠在电脑上，绿色 = 正在控制 iPad。

### 第一次配对

1. 运行程序，然后在电脑上打开 **设置 → 蓝牙和其他设备**（托盘菜单"配对新 iPad"一键打开），**配对完成前别关这个页面**。
   Windows 只有在这个页面开着时才接受别的设备主动来配对，否则会直接拒绝（系统日志 BTHUSB 事件 37）。
2. iPad：设置 → 蓝牙 → "其他设备"里点电脑的名字（比如 `DESKTOP-XXXX`）。
   如果出现**两个同名**的，一个是我们模拟的键鼠，一个是电脑本身，选能连上并且不再断开的那个。
3. 电脑右下角弹出配对通知时点"允许"。两边出现数字就确认一致。

配对过一次以后，程序启动 iPad 会自动重连，不用再开蓝牙设置页。

### 切换

| 操作 | 效果 |
|---|---|
| 鼠标推过最外侧屏幕的边缘（默认右边） | 切到 iPad，iPad 指针从靠电脑那一侧的边缘出现 |
| 在 iPad 上往回推过靠电脑那一侧的边缘 | 回到电脑 |
| `Ctrl+Alt+Q`（可改） | 任何时候在电脑 / iPad 之间切换 |
| iPad 断开蓝牙 | 自动回到电脑 |

控制 iPad 时：键盘、鼠标移动、左/右/中键、滚轮都转给 iPad，电脑本身不响应。
默认 `Win` = iPad 的 Command，`Alt` = Option；把配置里的 `ctrl_as_cmd` 改成 `true` 可以用 Ctrl+C / Ctrl+V 复制粘贴。
中文输入用 iPad 自己的输入法（Ctrl+空格 或 Caps Lock 切换）。

## 配置

第一次运行会在项目目录生成 `ipad-bridge.ini`，每一项都有中文注释。常用的几项：

- `side`：iPad 在电脑屏幕的哪一边，`right` / `left`
- `hotkey`：切换快捷键，如 `ctrl+alt+q`、`ctrl+shift+f12`、`scrolllock`
- `speed`：iPad 指针速度倍率
- `ipad_width`：判断"回到电脑"用的 iPad 虚拟宽度；回来得太早就调大，要推很久才回来就调小

改完重启程序生效。

## 原理

- `ipad_bridge/ble.py`：用 WinRT 的 `GattServiceProvider` 发布标准的 HID over GATT 服务（键盘 + 鼠标组合设备），
  iPad 把电脑当成一个蓝牙键鼠。
- `ipad_bridge/router.py`：全局低级键鼠钩子（`WH_MOUSE_LL` / `WH_KEYBOARD_LL`）。在 iPad 模式下吞掉电脑的输入，
  把鼠标相对位移、按键换算成 HID 报告发给 iPad。电脑指针被固定在屏幕中间，只用来计算位移。
- iPadOS 的鼠标加速没法关，程序无法精确知道 iPad 指针在哪。所以切过去时先把 iPad 指针"推"到靠电脑那一侧的边缘对齐，
  之后用虚拟位置估算什么时候该回到电脑（`ipad_width` / `return_push` 可调）。

## 已知限制

- 电脑指针在 iPad 模式下停在屏幕中间，看得见但不动。
- 侧键、横向滚轮暂不转发。
- 用的是 Windows 自带蓝牙协议栈，iPad 只连这一台电脑的"键鼠"，和其他蓝牙设备可以共存。
