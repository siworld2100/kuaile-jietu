# 快乐截图 (Screenshot Pinner)

截图 → 无边框置顶贴图，标注按需编辑（类似 Snipaste）。

## 使用流程

```
Ctrl+Alt+A 拖拽框选 → 松开鼠标 → 贴图立即悬浮
想标注: 右键贴图 →「编辑此贴图」→ 标注 →「完成」/ Enter → 贴图原地更新
```

### 框选
- 拖拽画出选区（带实时尺寸标签），**松开鼠标立即截图悬浮**——无需任何确认
- `Esc` / 右键取消；框太小（<10px）自动忽略

### 编辑器（右键贴图 →「编辑此贴图」）
- 工具：**画笔**（涂鸦）/ **矩形** / **椭圆**（画圈）/ **箭头** / **文字**（点击位置直接输入）
- 5 色色板：红 / 橙 / 蓝 / 绿 / 黑；标注粗细 3px，文字 16px
- `Ctrl+Z` /「撤销」回退一步，「清空」移除全部标注
- `Enter` /「完成」应用，`Esc` 取消
- 工具条最小宽度 680px，窄截图下画布居中显示、工具条始终完整

### 贴图
- 左键拖动 / 滚轮缩放（10%~800%）
- 右键菜单：编辑此贴图 / 保存 PNG / 复制到剪贴板 / 关闭
- 双击或 `Esc` 关闭；支持多张贴图并存

## 快速开始

双击 `dist\快乐截图.exe`（无需 Python）。托盘蓝色相机图标：左键截图，右键菜单（截图/设置/开机自启/退出）。首次运行自动注册开机自启。

## 设置窗口

托盘右键 →「设置...」：

- **截图快捷键 / 退出快捷键**：点击「修改」后**直接按下想要的组合键**即可录入（Esc 取消），自动检测与另一个快捷键的冲突
- **开机自启**：勾选框开关
- 配置保存在 `%APPDATA%\ScreenshotFloat\config.json`，每次启动自动与注册表同步

## 从源码运行 / 开发

```
py -3.12 screenshot_float.py        # 需要 pillow keyboard pystray
```

> 系统默认 `python`(3.13) 缺 tkinter，必须用 `py -3.12`。

## 重新打包 exe

```
py -3.12 -m PyInstaller --noconsole --onefile --icon icon.ico --name ScreenshotFloat screenshot_float.py
```

产物：`dist\ScreenshotFloat.exe`（单文件、无控制台、带图标）。

## 技术实现

- **Python 3.12 + tkinter + Pillow + pystray + keyboard**，单文件约 570 行
- 悬浮窗 `overrideredirect(True) + topmost`，彻底无窗口装饰
- `SetProcessDpiAwareness(2)` 保证高 DPI 屏幕截取 1:1 像素
- 全局热键使用 **Windows 原生 RegisterHotKey API**（独立线程消息循环），注册失败即提示快捷键被占用，不受其他软件的键盘钩子干扰
- 单实例：命名互斥体（`use_last_error=True` 修正 ctypes 错误码读取）
- 开机自启：`winreg` 写 `HKCU\...\CurrentVersion\Run`，托盘/设置/启动三处状态一致
- 崩溃日志：`%APPDATA%\ScreenshotFloat\error.log`

## 已知限制

- 框选遮罩只覆盖主显示器（多显示器支持待做）
- 「复制到剪贴板」依赖 pywin32（exe 内已包含；源码运行需 `py -3.12 -m pip install pywin32`）
- 快捷键被其他软件占用时无法自动检测冲突（Windows API 不提供查询）
- 框选中途按 `Esc` 或右键可取消截图（v2.1 修复：取消后不再卡死后续截图）
