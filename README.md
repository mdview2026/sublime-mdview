# mdview live preview for Sublime Text

Edit Markdown in Sublime Text and see it render in the **mdview** preview window
as you type — no saving required. Uses the same line-level diff + in-place block
patching as mdview's built-in F3 split editor, so the preview never flickers or
reloads. Cursor, click and selection are synced both ways.

> Live-push endpoints are a mdview **Pro** feature (the free/community build
> returns 404 and the plugin stays idle). Get mdview from
> [mdview.top](https://www.mdview.top/download) (international) or
> [mdview.cn](https://www.mdview.cn/download.html) (Chinese).

**Requires** Sublime Text 4 (build 4107+) and a running mdview desktop app.
No third-party Python dependencies.

## Requirements

- **Sublime Text 4** (build 4107+).
- **mdview desktop app (Pro)** — this plugin renders nothing locally; it only
  pushes the buffer to mdview's preview window. If mdview is not detected, the
  plugin points you to the official download page.
- No third-party Python dependencies; only the `urllib` / `threading` standard
  libraries are used.

## Installation

**Option 1 (recommended)**: Package Control command palette →
`Package Control: Install Package` → search for `mdview`.

**Option 2 (manual)**: copy (or symlink) this whole directory into Sublime's
Packages directory, named `mdview`:

- Windows: `%APPDATA%\Sublime Text\Packages\mdview\`
- macOS: `~/Library/Application Support/Sublime Text/Packages/mdview/`

Restart Sublime (or run `sublime_plugin.reload_plugin`) to activate.

## Usage

1. Right-click the editing area → **Preview in mdview** (or Command Palette
   `mdview: Preview Current File`) to open/focus the preview window — the
   context-menu entry **only appears in Markdown views** and stays out of the
   way for other file types;
2. Just type — edits are pushed as you go (one frame every 300 ms during
   continuous typing) and the preview updates automatically;
3. Saving (Ctrl+S) does not trigger a full reload: mdview recognizes the
   pushed-content echo and handles it silently.

### mdview not installed?

If mdview is not detected when you run the preview command, the plugin asks
whether to open the official download page. Install the **Pro** edition (live
push is a Pro feature), then run the preview command again.

The download page is routed by channel: Chinese OS UI language → CN build
(mdview.cn), otherwise → international build (mdview.top). The two builds have
fully independent config/payment/update channels; if the routing guesses wrong,
override it with the `channel` setting.

### Position sync (two-way)

- Move the Sublime cursor (after a 150 ms dwell) → the preview scrolls to the
  matching block;
- Click a block in the preview → the Sublime cursor jumps to the matching
  source line;
- Drag-select text in the preview → Sublime selects the matching source
  (character-precise, falling back to whole lines when no exact match). On
  landing, the Sublime viewport centers with a 1.5 s orange line flash — in an
  unfocused window the selection highlight itself is very dim, so the jump and
  flash are the main "synced" feedback; no need to click into Sublime to
  confirm (clicking would collapse the selection).

Disable with `sync_cursor` in the settings.

## How it works

- Each mdview preview instance registers `{pid, port, token, md_file}` at
  `%LOCALAPPDATA%\mdview\live\<port>.json`
  (macOS: `~/.cache/mdview/live/`) on startup;
- The plugin matches an instance by the current file path, verifies it with
  `GET /_live/ping`, then pushes the full buffer via `POST /_live/push` with
  the `x-mdview-token` header; mdview renders and broadcasts an SSE
  `live-push` event, and its front end reuses the F3 partial-refresh pipeline
  for flicker-free updates;
- When the view is closed, the plugin calls `/_live/detach` to unlink.

## Settings

`Preferences → Package Settings → mdview → Settings`, or Command Palette
`Preferences: mdview Settings` (split view: left = read-only defaults, right =
your overrides):

| Key | Default | Description |
|---|---|---|
| `mdview_path` | `""` | Path to the mdview executable; empty = auto-detect (on Windows: .md file association first, then registry InstallDir, common install dirs and PATH) |
| `debounce_ms` | `300` | Push throttle interval in ms (frame interval during continuous typing) |
| `enabled` | `true` | Master switch for live push |
| `sync_cursor` | `true` | Master switch for cursor/click/selection position sync |
| `channel` | `"auto"` | mdview download channel: `auto` = Chinese OS UI language → CN build, otherwise → international build; force with `cn` / `intl` |
| `debug_log` | `false` | Troubleshooting: write detailed push/cursor/selection sync logs to `<live dir>/plugin-debug.log` |

## Known limits

- Single-file push is capped at ~1.8 MB (server request-body limit);
- Only Markdown views backed by an on-disk file (`view.file_name()` non-empty)
  are pushed;
- After the mdview window switches to a different file, pushing stops
  automatically (server returns 409) and resumes when it switches back;
- When the same file is previewed by multiple mdview instances, the plugin
  trusts the one with the highest version (avoids latching onto an old exe
  without the new endpoints when old/new builds are mixed).

---

# mdview Sublime Text 插件（中文说明）

在 Sublime Text 中编辑 Markdown，mdview 预览窗口**边输入边无感刷新**——
无需保存文件，效果与 mdview 内置 F3 分屏编辑一致（行级 diff + 原位换块，不整页刷新）。

> 实时推送端点为 mdview **Pro** 功能（社区版返回 404，插件静默待机）。

## 依赖

- **Sublime Text 4**（build 4107+）。
- **mdview 桌面版（Pro）**——本插件不自己渲染 Markdown，只把缓冲区推给
  mdview 的预览窗口。未检测到 mdview 时会引导到官网下载页。
- 无第三方 Python 依赖，只用 `urllib` / `threading` 标准库。

## 安装

**方式一（推荐）**：Package Control 命令面板 → `Package Control: Install Package`
→ 搜索 `mdview`。

**方式二（手动）**：把整个目录复制（或建符号链接）到 Sublime 的
Packages 目录并命名为 `mdview`：

- Windows: `%APPDATA%\Sublime Text\Packages\mdview\`
- macOS: `~/Library/Application Support/Sublime Text/Packages/mdview/`

```bat
rem Windows 示例（管理员或开发者模式可用符号链接）
mklink /D "%APPDATA%\Sublime Text\Packages\mdview" "E:\Study\markdown\mdview\sublime"
```

重启 Sublime（或 `sublime_plugin.reload_plugin`）后生效。

## 使用

1. 右键编辑区 → **Preview in mdview**（或命令面板 `mdview: Preview Current File`）
   打开/唤起预览窗口——右键项**只在 Markdown 视图出现**，其它文件类型不占位置；
2. 正常编辑即可——输入即推（连续输入时每 300ms 出一帧），预览自动更新；
3. 保存（Ctrl+S）不会触发整页刷新：mdview 识别「推送内容落盘」回声，静默处理。

### 本机没装 mdview？

执行预览命令时若未检测到 mdview，插件会提示「本插件依赖 mdview 进行实时预览，
需要从 mdview 官网下载安装 mdview，是否继续？」——确认后打开官网下载页，
自行下载安装 **Pro 版**（实时推送是 Pro 功能）；装好后再次执行预览命令即可联动。

下载页自动分流：OS 界面语言为中文 → 国内版（mdview.cn），否则 → 国际版
（mdview.top）。两版配置/付费/更新通道完全独立，打开的版本不对可在设置里用
`channel` 手动指定。

### 位置同步（双向）

- 移动 Sublime 光标（停驻 150ms）→ 预览跟随滚动到对应块；
- 点击预览正文块 → Sublime 光标跳到对应源码行；
- 在预览里拖选文字 → Sublime 自动选中对应源码（精确到字符，对不上时退化为整行）；
  落位时 Sublime 视口会居中跳动并伴随 1.5 秒橙色行高亮——失焦窗口里选区
  高亮本身很暗，跳动和闪烁是「同步到了」的主要视觉反馈，无需点进 Sublime 确认
  （点击会塌缩选区）。

可在设置中用 `sync_cursor` 关闭。

## 工作原理

- mdview 每个预览实例启动时在 `%LOCALAPPDATA%\mdview\live\<port>.json`
  （macOS `~/.cache/mdview/live/`）登记 `{pid, port, token, md_file}`；
- 插件按当前文件路径匹配实例，`GET /_live/ping` 校验后带 `x-mdview-token`
  `POST /_live/push` 推送全文；mdview 渲染后广播 SSE `live-push`，
  前端复用 F3 的局部刷新链路完成无感更新；
- 关闭视图时插件调用 `/_live/detach` 解除关联。

## 设置

`Preferences → Package Settings → mdview → Settings`，或命令面板
`Preferences: mdview Settings`（左右分屏：左＝默认值只读，右＝用户覆盖）：

| 键 | 默认 | 说明 |
|---|---|---|
| `mdview_path` | `""` | mdview 可执行文件路径，留空自动探测（Windows 优先取 .md 文件关联的打开方式，再走注册表 InstallDir、常见安装目录与 PATH） |
| `debounce_ms` | `300` | 推送节流间隔（连续输入期间的出帧间隔） |
| `enabled` | `true` | 实时推送总开关 |
| `sync_cursor` | `true` | 光标/点击/选区位置同步总开关 |
| `channel` | `"auto"` | mdview 下载通道：`auto` = OS 界面语言中文装国内版、其余装国际版；可强制 `cn` / `intl` |
| `debug_log` | `false` | 排查用：往 `<live 目录>/plugin-debug.log` 写推送/光标/选区同步详细日志 |

## 已知边界

- 单文件推送上限约 1.8MB（服务端请求体限制）；
- 仅推送已落盘过的文件（`view.file_name()` 非空）的 Markdown 视图；
- mdview 窗口内切换到别的文件后，推送自动停止（服务端 409），切回即恢复；
- 同一文件被多个 mdview 实例同时预览时，插件采信版本最高的实例
  （避免新旧 exe 混开时栓在不支持新端点的旧实例上）。
