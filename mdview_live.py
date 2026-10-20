# mdview 实时预览插件（mdview 需求 165；333 增未装 mdview 时的官网安装引导）
#
# 在 Sublime Text 中编辑 Markdown 时，把【未保存】的缓冲区内容实时推送给本机
# mdview 预览窗口，实现与 mdview F3 分屏编辑一致的「边输入边无感刷新」。
#
# 工作原理：
#   每个 mdview 预览实例启动时在 live session 目录登记 <port>.json：
#       Windows: %LOCALAPPDATA%\mdview\live\
#       macOS:   ~/.cache/mdview/live/
#   内容为 {"pid", "port", "token", "md_file", "version"}。
#   本插件按当前文件路径匹配实例，GET /_live/ping 校验存活后，
#   带 x-mdview-token POST /_live/push 推送全文；mdview 端复用其
#   F3 分屏的局部刷新链路（行级 diff + 原位换块），不整页刷新、不闪。
#
# 注意：实时推送端点为 mdview Pro 功能；社区版返回 404，插件会静默待机。
#
# 兼容 Sublime Text 3（Python 3.3）与 4（Python 3.8）：勿用 f-string / walrus。

import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser

import sublime
import sublime_plugin

MD_EXTS = ('.md', '.markdown', '.mdown', '.mkdn', '.mkd', '.mdtxt', '.mdtext')

# 服务端 axum 默认请求体上限 2MB，留余量
MAX_PUSH_BYTES = 1800 * 1024

_pending_gen = {}    # view.id() -> int，防抖世代
_last_push_ts = {}   # view.id() -> float，上次实际推送时间（节流用）
_last_sent = {}      # view.id() -> str，上次已推送内容（相同则跳过）
_session = {}        # normpath -> {'port':, 'token':, 'path':} 已校验实例
_negative = {}       # normpath -> float，无实例负缓存时间戳
_notified = set()    # 已提示过的状态（'404' / 'too-large'），避免刷屏
NEGATIVE_TTL = 2.0   # 无实例时重新扫描间隔（秒）

# —— 需求 166：双向位置同步 ——
_cursor_gen = {}     # view.id() -> int，光标推送防抖世代
_cursor_scan = {}    # normpath -> float，光标路径上的低频实例发现时间戳
_last_cursor = {}    # view.id() -> int，上次已推送的光标行
_nav_applied = {}    # view.id() -> (line, ts)，live-nav 落点回声抑制
_sse = {}            # port -> {'thread':, 'stop': Event}，/_events 反向监听
_flash_gen = {}      # view.id() -> int，落点描边闪烁世代
CURSOR_DEBOUNCE_MS = 150   # 光标停驻 150ms 才推送（与内容防抖 300ms 分开）
CURSOR_SCAN_TTL = 3.0      # 无缓存会话时光标路径的实例发现间隔（秒）
SSE_GIVE_UP = 10           # SSE 连续重连失败次数上限（约 20s），实例已退出

# —— 需求 333：本机没有 mdview 时，引导前往官网自行下载安装 Pro 版 ——
# （不做自动下载/静默安装：用户拍板让安装发生在浏览器与安装器里。）
# 下载页按通道分流：中文用户 → 国内版 mdview.cn，其余 → 国际版 mdview.top；
# 两版配置/付费/更新通道完全隔离（需求 200），装错版须卸载重装，
# 故判定保守 + channel 设置可覆写。
DOWNLOAD_PAGE = {
    'cn': 'https://www.mdview.cn/download.html',
    'intl': 'https://www.mdview.top/download',
}


def _settings():
    return sublime.load_settings('mdview.sublime-settings')


def _dbg(msg):
    """调试日志（settings 里 \"debug_log\": true 才写）：<live 目录>/plugin-debug.log。"""
    try:
        if not _settings().get('debug_log', False):
            return
        now = time.time()
        line = '%s.%03d %s\n' % (
            time.strftime('%H:%M:%S', time.localtime(now)),
            int((now % 1) * 1000), msg)
        with open(os.path.join(_live_dir(), 'plugin-debug.log'), 'a',
                  encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass


def _enabled():
    return _settings().get('enabled', True)


def _sync_cursor():
    return _settings().get('sync_cursor', True)


def _debounce_ms():
    try:
        return max(50, int(_settings().get('debounce_ms', 300)))
    except Exception:
        return 300


def _live_dir():
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or ''
        return os.path.join(base, 'mdview', 'live')
    return os.path.join(os.path.expanduser('~'), '.cache', 'mdview', 'live')


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def _is_markdown(view):
    name = view.file_name()
    return bool(name) and name.lower().endswith(MD_EXTS)


def _http(method, url, token=None, payload=None, timeout=2.0):
    data = None
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['x-mdview-token'] = token
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def _ping(entry, file_path):
    """校验 session 存活且仍预览同一文件；失败返回 False（调用方清理）。"""
    url = 'http://127.0.0.1:{0}/_live/ping'.format(entry['port'])
    try:
        resp = _http('GET', url, timeout=1.5)
        data = json.loads(resp.read().decode('utf-8'))
        return data.get('ok') and _norm(data.get('path', '')) == _norm(file_path)
    except Exception:
        return False


def _version_key(s):
    """版本字符串 → 可比较元组（解析失败给空元组，视为最旧）。"""
    try:
        return tuple(int(p) for p in str(s).split('.') if p.isdigit())
    except Exception:
        return ()


def _scan_sessions(file_path):
    """扫描 live 目录，找到预览 file_path 的存活实例；顺带清理死实例文件。
    同一文件被多个实例同时预览时采信版本最高的（新旧 exe 混开同文档时，
    避免栓在不支持新端点的旧实例上）。"""
    live = _live_dir()
    try:
        names = os.listdir(live)
    except OSError:
        return None
    target = _norm(file_path)
    best = None
    for name in names:
        if not name.endswith('.json'):
            continue
        fpath = os.path.join(live, name)
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                entry = json.load(f)
        except (OSError, ValueError):
            continue
        if _norm(entry.get('md_file', '')) != target:
            continue
        if _ping(entry, file_path):
            if best is None or _version_key(entry.get('version')) \
                    > _version_key(best.get('version')):
                best = entry
            continue
        # 死实例（进程崩溃残留）→ 删除，下次不再尝试
        try:
            os.remove(fpath)
        except OSError:
            pass
    return best


def _find_session(file_path):
    key = _norm(file_path)
    cached = _session.get(key)
    if cached:
        return cached
    neg = _negative.get(key, 0.0)
    if time.time() - neg < NEGATIVE_TTL:
        return None
    entry = _scan_sessions(file_path)
    if entry:
        entry['path'] = file_path
        _session[key] = entry
        _ensure_sse(entry)  # 需求 166：会话建立即起反向监听（预览点击 → 光标回跳）
        return entry
    _negative[key] = time.time()
    return None


def _drop_session(file_path):
    key = _norm(file_path)
    _session.pop(key, None)
    _negative[key] = 0.0  # 立即允许重扫


# —— 需求 166：SSE 反向监听（预览点击 → 插件跳光标） ——

def _ensure_sse(entry):
    """每个实例端口一条 /_events 监听线程（daemon，断线退避重连）。"""
    port = entry['port']
    st = _sse.get(port)
    if st and st['thread'].is_alive():
        return
    st = {'stop': threading.Event(), 'resp': None}
    t = threading.Thread(
        target=_sse_loop,
        args=(port, entry['token'], entry.get('path') or '', st))
    t.daemon = True
    st['thread'] = t
    _sse[port] = st
    t.start()


def _kill_sse(st):
    """停 SSE 线程：置标志 + 关响应句柄，把阻塞中的 readline 立即唤醒。
    （插件热重载时旧线程滞留在 readline 最长 30s，期间与新线程双份处理事件）"""
    st['stop'].set()
    resp = st.get('resp')
    if resp is not None:
        try:
            resp.close()
        except Exception:
            pass


def _sse_loop(port, token, file_path, st):
    stop = st['stop']
    url = 'http://127.0.0.1:{0}/_events'.format(port)
    failures = 0
    while not stop.is_set():
        try:
            _dbg('sse connect port=%d' % port)
            req = urllib.request.Request(
                url, headers={'x-mdview-token': token})
            # 服务端 keep-alive 每 15s 一行注释，30s 读超时不会误杀健康连接
            resp = urllib.request.urlopen(req, timeout=30)
            st['resp'] = resp
            failures = 0
            _dbg('sse connected port=%d' % port)
            while not stop.is_set():
                raw = resp.readline()
                if not raw:
                    _dbg('sse closed-by-peer port=%d' % port)
                    break  # 连接关闭，外层重连
                line = raw.decode('utf-8', 'replace').strip()
                if line.startswith('data: live-select:'):
                    # 预览选中文字 → 选中对应源码（a=起始行, b=结束行(0=至文末), t=选中文本)
                    try:
                        payload = json.loads(line[len('data: live-select:'):])
                        sa = int(payload.get('a') or 0)
                        sb = int(payload.get('b') or 0)
                        st_text = payload.get('t') or ''
                    except (ValueError, TypeError):
                        _dbg('sse live-select parse-error line=%r' % line[:120])
                        continue
                    _dbg('sse live-select a=%d b=%d t=%d chars' % (sa, sb, len(st_text)))
                    # 注意默认参数绑定：闭包直接引用循环变量会拿到最后一次的值
                    sublime.set_timeout(
                        lambda a=sa, b=sb, t=st_text, fp=file_path:
                            _apply_select(fp, a, b, t), 0)
                    continue
                if not line.startswith('data: live-nav:'):
                    continue
                try:
                    n = int(line[len('data: live-nav:'):])
                except ValueError:
                    continue
                _dbg('sse live-nav line=%d' % n)
                # 注意默认参数绑定：闭包直接引用循环变量会拿到最后一次的值
                sublime.set_timeout(
                    lambda n=n, fp=file_path: _apply_nav(fp, n), 0)
        except Exception as e:
            failures += 1
            _dbg('sse error port=%d failures=%d: %r' % (port, failures, e))
            if failures >= SSE_GIVE_UP:
                _dbg('sse give-up port=%d' % port)
                break  # 实例大概率已退出，放弃（会话重建立时会再起线程）
        st['resp'] = None
        if stop.wait(2.0):
            break
    _sse.pop(port, None)
    _dbg('sse thread exit port=%d' % port)


def _find_view(file_path):
    """在所有窗口中找 file_path 对应的视图，返回 (view, window) 或 (None, None)。"""
    target = _norm(file_path)
    for w in sublime.windows():
        for v in w.views():
            fn = v.file_name()
            if fn and _norm(fn) == target:
                return v, w
    return None, None


def _apply_nav(file_path, line):
    """主线程执行：把 file_path 对应视图的选区跳到 line 行（不抢 OS 窗口焦点）。"""
    try:
        view, win = _find_view(file_path)
        if not view or not view.is_valid():
            _dbg('apply-nav line=%d: view not found' % line)
            return
        _dbg('apply-nav line=%d view=%s' % (line, view.id()))
    except Exception:
        _dbg('apply-nav exception:\n' + traceback.format_exc())
        return
    try:
    # 回声抑制置位必须先于选区修改（on_selection_modified 随即触发）
        _nav_applied[view.id()] = (line, time.time())
        _last_cursor[view.id()] = line
        pt = view.text_point(max(0, line - 1), 0)
        view.sel().clear()
        view.sel().add(sublime.Region(pt))
        view.show(pt)
        win.focus_view(view)  # 切到该标签（不抬升窗口）
    except Exception:
        _dbg('apply-nav exception:\n' + traceback.format_exc())


# —— 预览选中文字 → 选中对应源码（定位算法与 mdview 147 F3 选区联动同款） ——

def _sel_norm_map(s):
    """空白归一 + 索引回映：连续空白折叠为单个空格（首尾不留）。
    返回 (归一文本, map)，map[i] = 归一文本第 i 字符在原串中的偏移。"""
    out = []
    imap = []
    last_ws = True  # 起始视为空白：前导空白不产生空格
    for i, c in enumerate(s):
        if c.isspace():
            if not last_ws:
                out.append(' ')
                imap.append(i)
            last_ws = True
        else:
            out.append(c)
            imap.append(i)
            last_ws = False
    if out and out[-1] == ' ':
        out.pop()
        imap.pop()
    return ''.join(out), imap


def _locate_in_region(region, text):
    """在源码区间 region 中定位渲染选中文本 text，返回 (start, end) 或 None。
    精确 → 去首尾空白 → 空白归一 → 头尾锚点 逐级定位（同 mdview 147 selLocate）。"""
    i = region.find(text)
    if i >= 0:
        return (i, i + len(text))
    trimmed = text.strip()
    if trimmed and trimmed != text:
        i = region.find(trimmed)
        if i >= 0:
            return (i, i + len(trimmed))
    # 空白归一：段落跨源码行/跨块选区的换行差异（渲染软换行不入序列化、
    # 块边界换行数不一致）在此拉齐
    nr_text, nr_map = _sel_norm_map(region)
    nt, _ = _sel_norm_map(text)
    if not nt:
        return None
    j = nr_text.find(nt)
    if j >= 0:
        return (nr_map[j], nr_map[j + len(nt) - 1] + 1)
    # 头尾锚点：选区文字被行内格式符号（**、` 等）截断时，按归一首尾各 4 字符夹出区间
    #（命中段会带上中间的格式符号，可接受的近似）；过短文本锚点歧义大，不用
    if len(nt) > 4:
        head, tail = nt[:4], nt[-4:]
        hs = nr_text.find(head)
        te = nr_text.rfind(tail)
        if hs >= 0 and te >= hs:
            s = nr_map[hs]
            e = nr_map[te + len(tail) - 1] + 1
            if e > s:
                return (s, e)
    return None


def _apply_select(file_path, line_a, line_b, text):
    """主线程执行：在 file_path 对应视图中选中预览选区对应的源码（不抢 OS 窗口焦点）。"""
    try:
        if not _sync_cursor():
            _dbg('apply-select gated: sync_cursor off')
            return
        view, win = _find_view(file_path)
        if not view or not view.is_valid():
            _dbg('apply-select a=%d b=%d: view not found (fp=%r)' % (
                line_a, line_b, file_path))
            return
        total = view.rowcol(view.size())[0] + 1
        line_a = max(1, min(line_a, total))
        if line_b <= 0 or line_b > total:
            line_b = total  # 0 = 末块至文末（前端不知源码总行数）
        line_b = max(line_a, line_b)
        start_pt = view.text_point(line_a - 1, 0)
        end_pt = view.line(view.text_point(line_b - 1, 0)).end()
        s, e = start_pt, end_pt
        hit = None
        if text:
            region_text = view.substr(sublime.Region(start_pt, end_pt))
            hit = _locate_in_region(region_text, text)
            if hit:
                s, e = start_pt + hit[0], start_pt + hit[1]
            # 文本对不上（公式/复杂 html 块等）→ 退化为选中覆盖到的整段源码行
        # 回声抑制置位必须先于选区修改（on_selection_modified 随即触发）；
        # push_cursor 取 sel[0].begin() 所在行，此处 begin 即区间起点
        begin_line = view.rowcol(s)[0] + 1
        _dbg('apply-select a=%d b=%d view=%s region=(%d,%d) locate=%s -> sel=(%d,%d) line=%d' % (
            line_a, line_b, view.id(), start_pt, end_pt,
            'hit' if hit else 'fallback', s, e, begin_line))
        _nav_applied[view.id()] = (begin_line, time.time())
        _last_cursor[view.id()] = begin_line
        view.sel().clear()
        view.sel().add(sublime.Region(s, e))
        # show() 只在目标不可见时滚动——目标本就在视口内时零视觉反馈，
        # 短选区 + 失焦暗色高亮极易被看成「没有反应」；居中滚动给出明确跳动
        view.show_at_center(s)
        win.focus_view(view)  # 切到该标签（不抬升窗口）
        vid = view.id()
        # 落点高亮闪烁 1.5s：失焦窗口的选区走 inactive_selection 配色，实测
        # #4E5A65 对 #343D46 几乎不可见（且用户点进 Sublime 查看时点击即
        # 塌缩选区，永远看不到）。闪烁层绘制在选区底色之下，故放大到整行
        # （选区外的行段露出色带）；region.orangish 为 ST4 内建主题自适应亮色
        try:
            g = _flash_gen.get(vid, 0) + 1
            _flash_gen[vid] = g
            flash_region = view.line(sublime.Region(s, e))
            view.add_regions('mdview_sync_sel', [flash_region],
                             'region.orangish', '', 0)
            _dbg('apply-select flash set %r' % (
                (flash_region.begin(), flash_region.end()),))

            def clear_flash(v=view, gen=g):
                if v.is_valid() and _flash_gen.get(v.id()) == gen:
                    v.erase_regions('mdview_sync_sel')
            sublime.set_timeout(clear_flash, 1500)
        except Exception:
            pass
        expect = (s, e)
        _dbg('apply-select done sel_now=%r' % (
            [(r.begin(), r.end()) for r in view.sel()],))
        # 延迟回读：排查落位后被其他事件/插件悄悄塌缩
        def readback():
            v, _w = _find_view(file_path)
            if v and v.id() == vid:
                _dbg('apply-select readback+800ms sel=%r expect=%r' % (
                    [(r.begin(), r.end()) for r in v.sel()], expect))
        sublime.set_timeout(readback, 800)
    except Exception:
        _dbg('apply-select exception:\n' + traceback.format_exc())


def _stop_sse_for(file_path):
    """文件最后一个视图关闭时停掉对应实例的监听线程。"""
    entry = _session.get(_norm(file_path))
    if not entry:
        return
    port = entry['port']
    for w in sublime.windows():
        for v in w.views():
            fn = v.file_name()
            if fn and _norm(fn) == _norm(file_path):
                return  # 仍有视图使用该文件
    st = _sse.pop(port, None)
    if st:
        _kill_sse(st)


def plugin_unloaded():
    for st in _sse.values():
        _kill_sse(st)


def _status_once(key, msg):
    if key not in _notified:
        _notified.add(key)
        sublime.status_message(msg)


def _post(entry, endpoint, payload):
    url = 'http://127.0.0.1:{0}{1}'.format(entry['port'], endpoint)
    return _http('POST', url, token=entry['token'], payload=payload)


def push_view(view):
    """读取视图全文并推送给 mdview（在 async 线程中调用）。"""
    if not _enabled() or not _is_markdown(view):
        return
    file_path = view.file_name()
    content = view.substr(sublime.Region(0, view.size()))
    if _last_sent.get(view.id()) == content:
        return
    if len(content.encode('utf-8')) > MAX_PUSH_BYTES:
        _status_once('too-large', 'mdview: 文件过大，实时预览推送已跳过')
        return
    entry = _find_session(file_path)
    if not entry:
        return
    t0 = time.time()
    try:
        resp = _post(entry, '/_live/push', {'path': file_path, 'content': content})
        resp.read()
        _last_sent[view.id()] = content
        _last_push_ts[view.id()] = time.time()
        _dbg('push %d bytes ok in %dms' % (
            len(content.encode('utf-8')),
            int((_last_push_ts[view.id()] - t0) * 1000)))
    except urllib.error.HTTPError as e:
        _dbg('push http-error %d' % e.code)
        if e.code == 404:
            # 社区版无此端点：长时间负缓存，本次会话不再打扰
            _status_once('404', 'mdview: 实时预览推送需要 Pro 版本')
            _negative[_norm(file_path)] = time.time() + 3600
            _session.pop(_norm(file_path), None)
        elif e.code == 409:
            # 窗口已切到别的文件：丢弃缓存实例，下次按键重扫
            _drop_session(file_path)
        elif e.code == 403:
            _drop_session(file_path)  # token 失效（实例重启），重扫
    except Exception as e:
        _dbg('push conn-error: %r' % e)
        _drop_session(file_path)  # 连接失败：实例可能已退出
        # mdview 刚重启时缓存会话指向死端口：立即重扫补推一次，避免丢掉首个按键
        entry = _find_session(file_path)
        if not entry:
            return
        try:
            resp = _post(entry, '/_live/push', {'path': file_path, 'content': content})
            resp.read()
            _last_sent[view.id()] = content
            _last_push_ts[view.id()] = time.time()
            _dbg('push retry ok')
        except Exception as e2:
            _dbg('push retry failed: %r' % e2)
            _drop_session(file_path)


def push_cursor(view):
    """需求 166：把当前光标行推给 mdview（预览跟随滚动）。在 async 线程中调用。"""
    if not _enabled() or not _sync_cursor() or not _is_markdown(view):
        return
    file_path = view.file_name()
    sel = view.sel()
    if not sel:
        return
    line = view.rowcol(sel[0].begin())[0] + 1
    vid = view.id()
    if _last_cursor.get(vid) == line:
        return
    # 回声抑制：该行刚由 live-nav 应用（预览点回来的），不回推
    na = _nav_applied.get(vid)
    if na and na[0] == line and time.time() - na[1] < 1.5:
        _last_cursor[vid] = line
        _dbg('cursor line=%d suppressed (echo)' % line)
        return
    entry = _session.get(_norm(file_path))
    if not entry:
        # 光标事件很频繁，无缓存会话时只能低频尝试发现（内容推送链路自带发现）
        key = _norm(file_path)
        now = time.time()
        if now - _cursor_scan.get(key, 0.0) < CURSOR_SCAN_TTL:
            return
        _cursor_scan[key] = now
        entry = _find_session(file_path)
        if not entry:
            return
    _last_cursor[vid] = line
    _dbg('cursor line=%d post' % line)
    try:
        _post(entry, '/_live/cursor', {'path': file_path, 'line': line}).read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _negative[_norm(file_path)] = time.time() + 3600
            _session.pop(_norm(file_path), None)
        elif e.code in (403, 409):
            _drop_session(file_path)
    except Exception:
        _drop_session(file_path)


class MdviewLiveListener(sublime_plugin.EventListener):

    def on_modified_async(self, view):
        # 领先+尾随节流（对齐 F3 分屏的 300ms throttle）：距上次实际推送已满
        # debounce 立即推（连续输入时每 300ms 出一次），否则按剩余时间尾随一发；
        # 世代号保证同一时刻只有最新一发在途。原为纯防抖——连续输入期间一直不推，
        # 体感远不如 F3 实时。
        if not _enabled() or not _is_markdown(view) or view.is_scratch():
            return
        vid = view.id()
        gen = _pending_gen.get(vid, 0) + 1
        _pending_gen[vid] = gen
        debounce = _debounce_ms()
        elapsed_ms = (time.time() - _last_push_ts.get(vid, 0.0)) * 1000
        wait = max(0, int(debounce - elapsed_ms))

        def fire():
            if _pending_gen.get(vid) != gen:
                return  # 期间又有修改，由更新的世代接管
            push_view(view)

        sublime.set_timeout_async(fire, wait)

    def on_activated_async(self, view):
        # 需求 166 修订：激活视图时也补建会话/SSE——先开 Sublime 后开 mdview
        # （或 mdview 重启、SSE 连败放弃后）且尚未编辑/移动光标时，
        # 选区/点击同步依赖的 SSE 借激活事件恢复，不必等首次输入。
        if not _enabled() or not _is_markdown(view) or view.is_scratch():
            return
        file_path = view.file_name()
        entry = _session.get(_norm(file_path))
        if entry:
            _ensure_sse(entry)  # 线程已退出则补起（内部判活，幂等）
            return
        key = _norm(file_path)
        now = time.time()
        if now - _cursor_scan.get(key, 0.0) < CURSOR_SCAN_TTL:
            return
        _cursor_scan[key] = now
        _find_session(file_path)

    def on_selection_modified_async(self, view):
        # 需求 166：光标行跟随——停驻 150ms 后推送光标行（世代防抖同内容推送）
        if not _enabled() or not _sync_cursor() or not _is_markdown(view) \
                or view.is_scratch():
            return
        vid = view.id()
        gen = _cursor_gen.get(vid, 0) + 1
        _cursor_gen[vid] = gen

        def fire():
            if _cursor_gen.get(vid) != gen:
                return
            push_cursor(view)

        sublime.set_timeout_async(fire, CURSOR_DEBOUNCE_MS)

    def on_post_save_async(self, view):
        # 保存后立即推一次终态（多数情况与防抖在途内容相同，插件侧幂等跳过）
        push_view(view)

    def on_close(self, view):
        vid = view.id()
        _pending_gen.pop(vid, None)
        _last_push_ts.pop(vid, None)
        _last_sent.pop(vid, None)
        _cursor_gen.pop(vid, None)
        _last_cursor.pop(vid, None)
        _nav_applied.pop(vid, None)
        file_path = view.file_name()
        if not file_path:
            return
        _stop_sse_for(file_path)
        entry = _session.pop(_norm(file_path), None)
        if not entry:
            return

        def detach():
            try:
                _post(entry, '/_live/detach', {'path': file_path}).read()
            except Exception:
                pass

        sublime.set_timeout_async(detach, 0)


class MdviewPreviewCommand(sublime_plugin.WindowCommand):
    """在 mdview 中打开当前文件（未运行则启动 mdview）。"""

    def is_enabled(self):
        view = self.window.active_view()
        return bool(view) and _is_markdown(view)

    def is_visible(self):
        """右键菜单只对 Markdown 视图出现。

        Package Control 收录要求 context menu 条目必须带条件（右键菜单位有限）：
        is_enabled 只把条目变灰、仍占一席，故这里用 is_visible 让非 Markdown
        视图彻底看不到「Preview in mdview」。
        """
        view = self.window.active_view()
        return bool(view) and _is_markdown(view)

    def run(self):
        view = self.window.active_view()
        file_path = view.file_name()
        exe = _find_exe()
        if not exe:
            # 需求 333：未检测到 mdview → 引导官网下载安装 Pro（装好后再次执行即可联动）
            _offer_install()
            return
        try:
            subprocess.Popen([exe, file_path])
        except OSError as e:
            sublime.error_message('启动 mdview 失败：{0}'.format(e))


# —— 需求 333：未装 mdview 时的官网安装引导（Sublime 无 locale API，通道按 OS 界面语言） ——

def _lang_zh():
    """OS 界面语言是否中文。Windows 用 GetUserDefaultUILanguage（Sublime 自身
    选 UI 语言同源，比 locale.getdefaultlocale 的「区域格式」准）；mac 读
    AppleLocale；Linux 看 LANG；最后 locale 兜底。"""
    try:
        if os.name == 'nt':
            import ctypes
            return (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x04
        if sys.platform == 'darwin':
            out = subprocess.check_output(
                ['defaults', 'read', '-g', 'AppleLocale'],
                stderr=subprocess.STDOUT)
            return out.strip().lower().startswith(b'zh')
        lang = os.environ.get('LANG', '') or os.environ.get('LC_ALL', '')
        if lang:
            return lang.lower().startswith('zh')
    except Exception:
        pass
    try:
        import locale as _locale
        return (_locale.getdefaultlocale()[0] or '').lower().startswith('zh')
    except Exception:
        return False


def _channel():
    """版本通道：channel 设置显式指定优先（cn/intl，自动判定误伤的逃生口）；
    auto = OS 界面语言中文 → 国内版，否则国际版（与产品 GEO 分流一致）。"""
    pref = str(_settings().get('channel', 'auto') or 'auto').lower()
    if pref in ('cn', 'intl'):
        return pref
    return 'cn' if _lang_zh() else 'intl'


def _offer_install():
    """提示「依赖 mdview，是否前往官网下载」；确认后打开对应版本的下载页。
    装好 mdview 后再次执行预览命令即可联动。"""
    ch = _channel()
    tag = '国内版' if ch == 'cn' else '国际版'
    if sublime.yes_no_dialog(
            '本插件依赖 mdview 进行实时预览，需要从 mdview 官网下载安装 mdview'
            '（%s），是否继续？' % tag):
        webbrowser.open(DOWNLOAD_PAGE[ch])


def _installed_exe(ch):
    """注册表定位已安装的 mdview：InstallDir（国内版 mdview / 国际版 mdview-en，
    WOW64 双视图都查——32 位 NSIS 写的键在 WOW6432Node）→ 默认目录。
    只管通道定位、找不到返回 None——不回环调 _find_exe（_find_exe 已把它排在
    assoc 之后、通用候选之前，回环会在未装 mdview 的机器上无限递归）。"""
    product_id = 'mdview-en' if ch == 'intl' else 'mdview'
    if os.name == 'nt':
        try:
            import winreg
            for view in (0, winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                try:
                    key = winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE,
                        r'Software\%s' % product_id, 0,
                        winreg.KEY_READ | view)
                except OSError:
                    continue
                try:
                    install_dir = winreg.QueryValueEx(key, 'InstallDir')[0]
                finally:
                    key.Close()
                if install_dir:
                    exe = os.path.join(install_dir, 'mdview.exe')
                    if os.path.exists(exe):
                        return exe
        except Exception:
            pass
    for base in (os.environ.get('ProgramFiles'), os.environ.get('ProgramW6432')):
        if base:
            exe = os.path.join(base, product_id, 'mdview.exe')
            if os.path.exists(exe):
                return exe
    return None


def _find_exe_assoc():
    """Windows：按 .md 文件关联（用户实际的「打开方式」）定位 mdview.exe。

    走 AssocQueryStringW 而非手读注册表——UserChoice/FileExts 残留键的优先级
    规则复杂（本仓库 issue-md-file-association.md 踩过），系统 API 一步到位。
    仅当关联程序确实叫 mdview.exe 时才采信（关联到 VS Code 等则跳过走兜底）。
    """
    if os.name != 'nt':
        return None
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        # ASSOCF_NONE=0, ASSOCSTR_EXECUTABLE=2；S_OK=0
        hr = ctypes.windll.shlwapi.AssocQueryStringW(
            0, 2, '.md', 'open', buf, ctypes.byref(size))
        if hr != 0:
            return None
        exe = buf.value.strip()
        if exe and os.path.basename(exe).lower() == 'mdview.exe' \
                and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return None


def _find_exe():
    configured = _settings().get('mdview_path') or ''
    if configured and os.path.exists(configured):
        return configured
    assoc = _find_exe_assoc()
    if assoc:
        return assoc
    # 需求 333：注册表 InstallDir（国内版 mdview / 国际版 mdview-en，双视图）
    for ch in ('cn', 'intl'):
        exe = _installed_exe(ch)
        if exe:
            return exe
    if os.name == 'nt':
        candidates = []
        for env_var in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            base = os.environ.get(env_var)
            if base:
                for product_id in ('mdview', 'mdview-en'):
                    candidates.append(
                        os.path.join(base, product_id, 'mdview.exe'))
                    candidates.append(
                        os.path.join(base, 'Programs', product_id, 'mdview.exe'))
    else:
        candidates = [
            '/Applications/mdview.app/Contents/MacOS/mdview',
            os.path.expanduser('~/Applications/mdview.app/Contents/MacOS/mdview'),
        ]
    for c in candidates:
        if os.path.exists(c):
            return c
    # PATH 兜底
    name = 'mdview.exe' if os.name == 'nt' else 'mdview'
    for d in os.environ.get('PATH', '').split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None
