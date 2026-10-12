"""锋芒直播录屏：抖音 / 快手 / 小红书直播开播监听 + 自动录制（pywebview 桌面窗口）。"""
import ctypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import webview

from core import guard, vault
from core.config import APP_DIR, RES_DIR, Store, WindowState
from core.douyin import DouyinClient, DouyinError
from core.engine import Engine, find_ffmpeg
from core.kuaishou import KuaishouClient, KuaishouError, looks_like_kuaishou
from core.xiaohongshu import XiaohongshuClient, XiaohongshuError, looks_like_xiaohongshu

APP_NAME = "锋芒直播录屏"
VERSION = "1.0.0"
# 只允许开一个：第二个实例看到这个互斥体存在就直接把老窗口叫出来然后退出
MUTEX_NAME = "FengmangLiveScreen.SingleInstance.v1"
# 左右两栏（主播列表 + 添加主播）比原来的单栏要宽一些
WINDOW_WIDTH = 1120
WINDOW_HEIGHT = 760
MIN_WIDTH = 1000
MIN_HEIGHT = 640

SEC_UID_HINT = re.compile(r"MS4wLjAB")


def _fatal(message):
    """启动前的硬错误：打包版没有控制台，只能弹系统对话框告诉用户。"""
    ctypes.windll.user32.MessageBoxW(None, str(message), APP_NAME, 0x10)


def _platform_of(line):
    """按链接特征挑平台：抖音域名 / 抖音号段优先，再看小红书，最后才是快手。"""
    text = (line or "").strip().lower()
    if "douyin" in text or SEC_UID_HINT.search(line or ""):
        return "douyin"
    if looks_like_xiaohongshu(line):
        return "xiaohongshu"
    if looks_like_kuaishou(line):
        return "kuaishou"
    return "douyin"


def _set_clipboard(text):
    """写系统剪贴板（不依赖 tkinter，打包后也能用）。"""
    cf_unicodetext = 13
    gmem_moveable = 0x0002
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    if not user32.OpenClipboard(None):
        return False
    handle = None
    try:
        user32.EmptyClipboard()
        data = str(text).encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(gmem_moveable, len(data))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return False
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(cf_unicodetext, handle):
            return False
        handle = None  # 所有权交给系统，不能再释放
        return True
    finally:
        if handle:
            kernel32.GlobalFree(handle)
        user32.CloseClipboard()


def _dpi_scale():
    """系统 DPI 缩放比例，150% 的屏就是 1.5。"""
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except AttributeError:  # 老系统没这个函数，当成 100%
        return 1.0


def _primary_screen():
    """主屏大小，换算成 pywebview 那一套逻辑像素。"""
    user32 = ctypes.windll.user32
    scale = _dpi_scale()
    return user32.GetSystemMetrics(0) / scale, user32.GetSystemMetrics(1) / scale


def _virtual_screen():
    """所有显示器拼起来的总范围（逻辑像素），用来判断记住的位置还在不在屏幕上。"""
    user32 = ctypes.windll.user32
    scale = _dpi_scale()
    return (
        user32.GetSystemMetrics(76) / scale,   # SM_XVIRTUALSCREEN
        user32.GetSystemMetrics(77) / scale,   # SM_YVIRTUALSCREEN
        user32.GetSystemMetrics(78) / scale,   # SM_CXVIRTUALSCREEN
        user32.GetSystemMetrics(79) / scale,   # SM_CYVIRTUALSCREEN
    )


def _fit_size(screen_w, screen_h):
    """默认窗口比屏幕还大的时候缩一缩，否则居中了也有一截露在屏幕外面。"""
    width = max(MIN_WIDTH, min(WINDOW_WIDTH, int(screen_w) - 40))
    height = max(MIN_HEIGHT, min(WINDOW_HEIGHT, int(screen_h) - 40))
    return width, height


def _centered_geometry():
    """默认位置：主屏正中央。

    pywebview 自带的 CenterScreen 在缩放屏上算不对（它把窗口尺寸乘了 DPI 缩放，
    但 WinForms 居中时用的还是没缩放的尺寸），所以干脆自己按逻辑像素算好传进去。
    """
    screen_w, screen_h = _primary_screen()
    width, height = _fit_size(screen_w, screen_h)
    return {
        "x": int((screen_w - width) / 2),
        "y": int((screen_h - height) / 2),
        "width": width,
        "height": height,
    }


def _initial_geometry(state):
    """上次关窗口时的位置；没记录、或者位置已经跑到屏幕外，就用主屏居中。"""
    saved = state.load()
    if not saved:
        return _centered_geometry()
    vx, vy, vw, vh = _virtual_screen()
    width = max(MIN_WIDTH, min(saved["width"], int(vw)))
    height = max(MIN_HEIGHT, min(saved["height"], int(vh)))
    x, y = saved["x"], saved["y"]
    # 标题栏得留在屏幕里，横向至少露出 120px，不然用户拖都拖不回来
    if not vx - 8 <= y <= vy + vh - 32:
        return _centered_geometry()
    if not (x + width > vx + 120 and x < vx + vw - 120):
        return _centered_geometry()
    return {"x": x, "y": y, "width": width, "height": height}


def _remember_geometry(window, state):
    """记住窗口位置和大小：拖完/缩放完停 0.8 秒落盘一次，关窗口时再落一次。"""
    lock = threading.Lock()
    pending = {"timer": None, "minimized": False}

    def _write():
        with lock:
            pending["timer"] = None
            if pending["minimized"]:
                return
        try:
            state.save(window.x, window.y, window.width, window.height)
        except Exception:  # noqa: BLE001  窗口已经销毁就随它去，别卡住退出流程
            pass

    def _schedule():
        with lock:
            if pending["timer"] is not None:
                pending["timer"].cancel()
            timer = threading.Timer(0.8, _write)
            timer.daemon = True
            pending["timer"] = timer
            timer.start()

    def _on_moved(*_args):
        _schedule()

    def _on_resized(*_args):
        _schedule()

    def _on_minimized():
        # 最小化时窗口坐标会变成 -32000 这种占位值，别记进去
        with lock:
            pending["minimized"] = True
            if pending["timer"] is not None:
                pending["timer"].cancel()
                pending["timer"] = None

    def _on_restored():
        with lock:
            pending["minimized"] = False
        _schedule()

    def _on_closing():
        with lock:
            if pending["timer"] is not None:
                pending["timer"].cancel()
                pending["timer"] = None
        _write()

    window.events.moved += _on_moved
    window.events.resized += _on_resized
    window.events.minimized += _on_minimized
    window.events.restored += _on_restored
    window.events.closing += _on_closing


def _claim_single_instance():
    """占一个全局互斥体，返回值第二个是「已经有程序在跑了」。

    多开会让两个进程同时轮询开播状态、同时往同一个目录里写录像，白占 CPU 和带宽。
    返回的句柄要一直留着，进程退出时系统才会把互斥体收掉。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return None, False          # 拿不到就照常启动，别把人挡在门外
    return handle, ctypes.get_last_error() == 183   # ERROR_ALREADY_EXISTS


def _focus_running_instance(title, wait=3.0):
    """把已经在跑的那个窗口叫到前台（第二个实例干的最后一件好事）。"""
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    hwnd = 0
    deadline = time.time() + wait
    while time.time() < deadline:     # 老实例可能刚启动、窗口还没建出来
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            break
        time.sleep(0.25)
    if not hwnd:
        return False
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow(hwnd, 5)      # SW_SHOW
    user32.ShowWindow(hwnd, 9)      # SW_RESTORE
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    nosize_nomove = 0x0001 | 0x0002
    user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, nosize_nomove | 0x0040)
    user32.SetWindowPos(hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, nosize_nomove)
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow(hwnd)
    return True


def _force_show(window, title):
    """把窗口从托盘里叫回前台。

    只调 pywebview 的 show() 不够稳：它内部是 Show() + Activate()，而 Activate()
    在窗口不属于前台进程时会被系统忽略，表现就是「点托盘只闪一下任务栏、窗口不上来」，
    偶尔还会停在最小化状态，任务栏有按钮但屏幕上看不到窗口。所以这里再补一次
    ShowWindow + SetForegroundWindow。
    """
    for attempt in range(2):
        try:
            window.restore()
            window.show()
        except Exception:  # noqa: BLE001
            pass
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow(hwnd, 5)   # SW_SHOW
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = ctypes.c_bool
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        nosize_nomove = 0x0001 | 0x0002
        topmost, notopmost = ctypes.c_void_p(-1), ctypes.c_void_p(-2)
        # 先顶到最前再撤掉，比直接 SetForegroundWindow 更容易成功
        user32.SetWindowPos(hwnd, topmost, 0, 0, 0, 0,
                            nosize_nomove | 0x0040)   # + SHOWWINDOW
        user32.SetWindowPos(hwnd, notopmost, 0, 0, 0, 0, nosize_nomove)
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow(hwnd)
        if not attempt and user32.IsIconic(hwnd):
            time.sleep(0.35)     # 还缩在任务栏里就再来一遍
            continue
        return


def _setup_tray(window, api, icon_path):
    """托盘图标：菜单里能把窗口调回来，也能退出程序。

    最小化按钮走 Windows 自己的最小化（缩到任务栏）；右上角 × 的行为看设置里那个
    「关闭到托盘」：打开就收进托盘、程序继续在后台跑，没打开就正常退出。

    返回托盘图标对象（没起来就返回 None，这时 × 照常退出，免得窗口藏起来以后回不去）。
    """
    try:
        import pystray
        from PIL import Image
    except ImportError as exc:  # noqa: BLE001
        api._engine.log(f"没有托盘支持（{exc}），关闭窗口就是直接退出程序")
        return None

    def _close_to_tray():
        try:
            return bool(api._store.config.get("closeToTray"))
        except Exception:  # noqa: BLE001
            return False

    # quitting：托盘菜单点「退出」时置上。退出走的也是 window.destroy()，一样会触发
    #   closing 事件，不打个标记就会被下面那个 _on_closing 当成点 × 拦下来。
    # minimized：从任务栏最小化状态关窗口时，窗口坐标是 -32000 的占位值。
    flags = {"quitting": False, "minimized": False}

    def _show_window(_icon=None, _item=None):
        try:
            _force_show(window, f"{APP_NAME} {VERSION}")
        except Exception:  # noqa: BLE001
            pass

    def _quit(icon=None, _item=None):
        flags["quitting"] = True
        try:
            api._engine.stop()
        finally:
            if icon is not None:
                icon.stop()
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass

    try:
        tray = pystray.Icon(
            APP_NAME,
            Image.open(str(icon_path)),
            APP_NAME,
            pystray.Menu(
                pystray.MenuItem("显示主界面", _show_window, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", _quit),
            ),
        )
        tray.run_detached()
        for _ in range(80):      # 等图标真的挂到托盘上，最多 4 秒
            if tray.visible:
                break
            time.sleep(0.05)
        if not tray.visible:
            api._engine.log("托盘图标没能显示，关闭窗口就是直接退出程序")
            return None
    except Exception as exc:  # noqa: BLE001
        api._engine.log(f"托盘初始化失败：{exc!r}")
        return None

    def _on_minimized():
        flags["minimized"] = True

    def _on_restored():
        flags["minimized"] = False

    def _on_closing():
        """拦住关闭就是收进托盘；返回 True 才是让窗口真关掉。"""
        if flags["quitting"] or not _close_to_tray():
            return True
        if ctypes.windll.user32.GetSystemMetrics(0x2000):   # SM_SHUTTINGDOWN
            return True       # 系统正在关机，这时候不能拦
        if flags["minimized"]:
            # 最小化状态下坐标是 -32000 占位值，直接 Hide() 再 Show() 会变成
            # 「任务栏有按钮、屏幕上看不到窗口」，先还原到最小化前的位置再藏。
            try:
                window.restore()
            except Exception:  # noqa: BLE001
                pass
        window.hide()
        return False

    window.events.minimized += _on_minimized
    window.events.restored += _on_restored
    window.events.closing += _on_closing
    return tray


class Api:
    # 注意：js_api 对象上所有公开属性都会被 pywebview 递归遍历一遍
    # （它要枚举出能暴露给 JS 的方法）。一旦它顺着 self.window 摸到
    # window.native 里的 WebView2 COM 对象，就会在非 UI 线程上疯狂抛异常，
    # 每次都拖住整个界面。所以这些成员一律用下划线前缀，让它跳过。
    def __init__(self):
        self._store = Store()
        self._engine = Engine(self._store)
        self._client = DouyinClient()
        self._ks = KuaishouClient()
        self._xhs = XiaohongshuClient()
        self._window = None
        self._tray = None
        self._ks_hint_shown = False
        self._xhs_hint_shown = False
        self._dialog_lock = threading.Lock()

    # ---------- 状态 ----------

    def get_state(self):
        state = self._engine.snapshot()
        state["version"] = VERSION
        return state

    def get_logs(self):
        return list(self._engine.logs)[-200:]

    # ---------- 设置 ----------

    def apply_settings(self, incoming):
        config = self._store.apply_config(incoming or {})
        self._engine.log("设置已应用")
        return config

    def pick_folder(self, current=""):
        """选目录：用 pywebview 自己的原生对话框，避免在子线程里开 tkinter 窗口。"""
        with self._dialog_lock:
            try:
                chosen = self._window.create_file_dialog(
                    webview.FOLDER_DIALOG,
                    directory=current or self._store.config["save"],
                )
            except Exception:  # noqa: BLE001
                return ""
        if not chosen:
            return ""
        if isinstance(chosen, (list, tuple)):
            return str(chosen[0]) if chosen else ""
        return str(chosen)

    # ---------- 主播列表 ----------

    def add_users(self, text):
        added, updated, failed = [], [], []
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        for line in lines:
            try:
                record = self._resolve_line(line)
                if record is None:
                    failed.append({"line": line, "reason": "不认识的链接格式"})
                    continue
                if self._store.add_user(record):
                    added.append(record["nickname"])
                    self._engine.log(f"已添加主播：{record['nickname']}")
                else:
                    same = next((u for u in self._store.users
                                 if Store.same_user(u, record)), None)
                    if self._fill_share_link(same, record):
                        updated.append(record["nickname"])
                        continue
                    name = (same or {}).get("nickname") or record.get("nickname") or line
                    failed.append({"line": line, "reason": f"已经在列表里了：{name}"})
            except (DouyinError, KuaishouError, XiaohongshuError, ValueError, OSError) as exc:
                failed.append({"line": line, "reason": str(exc)})
        return {"added": added, "updated": updated, "failed": failed,
                "count": len(self._store.users)}

    def _fill_share_link(self, existing, record):
        """同一位主播再粘一条 APP 分享链接时，把之前缺的分享链接补上。

        快手号 / PC 链接加进来的记录没有分享链接，只能走快手接口（按 IP 限流）；
        补上分享链接之后检测改走分享页 SSR，就不吃限流了。所以这不是重复添加，
        是把这条记录修好。
        """
        share_url = str((record or {}).get("share_url") or "")
        if not existing or not share_url or existing.get("share_url"):
            return False
        if str(existing.get("type") or "") != str(record.get("type") or ""):
            return False
        fields = {"share_url": share_url}
        # 拿快手号加的那条，昵称就是快手号本身；这回解析出真名了就顺手换掉
        if record.get("nickname") and existing.get("nickname") in ("", existing.get("unique_id")):
            fields["nickname"] = record["nickname"]
        if record.get("avatar") and not existing.get("avatar"):
            fields["avatar"] = record["avatar"]
        self._store.update_user(existing["userid"], **fields)
        self._engine.log(f"已给主播补上分享链接："
                         f"{fields.get('nickname') or existing.get('nickname')}（{share_url}）")
        return True

    def _resolve_line(self, line):
        """把一行链接解析成主播记录；抖音 / 快手 / 小红书按链接特征分派。"""
        platform = _platform_of(line)
        if platform == "xiaohongshu":
            parsed = self._xhs.parse_input(line)
            if not parsed:
                return None
            return self._xiaohongshu_record(parsed)
        if platform == "kuaishou":
            parsed = self._ks.parse_input(line) or self._client.parse_input(line)
            if not parsed:
                return None
            if "principal_id" in parsed:
                return self._kuaishou_record(parsed)
            return self._douyin_record(parsed)
        parsed = self._client.parse_input(line) or self._ks.parse_input(line)
        if not parsed:
            return None
        if "principal_id" in parsed:
            return self._kuaishou_record(parsed)
        return self._douyin_record(parsed)

    def _douyin_record(self, parsed):
        client = self._client
        client.bootstrap()
        info = client.resolve(parsed)
        record = {
            "type": "douyin",
            "nickname": info.get("nickname") or "未命名主播",
            "userid": str(info.get("userid") or info.get("sec_uid") or parsed.get("web_rid", "")),
            "sec_uid": info.get("sec_uid", "") or parsed.get("sec_uid", ""),
            "web_rid": str(info.get("web_rid", "")),
            "unique_id": info.get("unique_id", ""),
            "avatar": info.get("avatar", ""),
            "disable": False,
            "mode": "video",
        }
        if not record["userid"]:
            raise ValueError("没解析到主播 ID")
        return record

    def _kuaishou_record(self, parsed):
        pid = str(parsed.get("principal_id") or "")
        info = self._ks.resolve(parsed)
        kwai = str(info.get("unique_id") or pid or "")
        if not kwai:
            raise ValueError("没解析出快手号，换个链接再试试")
        # 快手没开播时拿不到昵称，先用快手号顶上，开播后检测会自动补上
        record = {
            "type": "kuaishou",
            "nickname": info.get("nickname") or kwai,
            "userid": str(info.get("userid") or kwai),
            "sec_uid": "",
            "web_rid": "",
            "unique_id": kwai,
            "avatar": info.get("avatar", ""),
            "disable": False,
            "mode": "video",
            # 分享短链要存下来：每轮检测都重新打开它拿开播状态和清晰度地址
            "share_url": parsed.get("share_url", ""),
        }
        if not record["share_url"]:
            self._hint_share_link()
        return record

    def _hint_share_link(self):
        """PC 链接在部分网络上会被快手限流，第一次遇到时提醒一下用分享链接。"""
        if self._ks_hint_shown:
            return
        self._ks_hint_shown = True
        self._engine.log("提示：快手优先粘贴 APP 里的分享链接（v.kuaishou.com/...），"
                         "分享页能直接读出开播状态和清晰度；PC 链接在本机会被快手限流，检测可能不准。")

    def _xiaohongshu_record(self, parsed):
        info = self._xhs.resolve(parsed)
        room_id = str(info.get("web_rid") or "")
        if not room_id:
            raise XiaohongshuError("没解析出小红书直播间 ID，请粘贴直播间链接或 APP 分享链接")
        record = {
            "type": "xiaohongshu",
            "nickname": info.get("nickname") or f"小红书{room_id[-6:]}",
            "userid": room_id,
            "sec_uid": "",
            "web_rid": room_id,
            "unique_id": room_id,
            "avatar": info.get("avatar", ""),
            "disable": False,
            "mode": "video",
            # 分享链接要存下来：房间号失效后还能靠它重新找回落地的直播页
            "share_url": parsed.get("share_url", ""),
        }
        self._hint_xhs_link()
        return record

    def _hint_xhs_link(self):
        """小红书只能按直播间链接跟，第一次添加时说清楚，免得拿主页链接反复试。"""
        if self._xhs_hint_shown:
            return
        self._xhs_hint_shown = True
        self._engine.log("提示：小红书按「直播间」跟。APP 里进直播间「分享 → 复制链接」，"
                         "或者直接粘直播间地址（xiaohongshu.com/livestream/...）都行。"
                         "小红书主页链接读不到开播状态（官方要登录），所以主页链接加不进来；"
                         "另外房间号只管这一场，主播下播后原房间就失效了，他下次开播要重新粘一条新链接。")

    def remove_user(self, userid):
        self._engine.stop_user(userid)
        return self._store.remove_user(userid)

    def set_auto(self, userid, enabled):
        user = self._store.update_user(userid, disable=not enabled)
        return bool(user)

    def set_mode(self, userid, mode):
        if mode not in ("video", "audio", "vr"):
            return False
        self._store.update_user(userid, mode=mode)
        self._engine.log(f"录制方式：{ {'video': '默认录制视频', 'audio': '仅录制音频', 'vr': '录制VR直播'}[mode] }")
        return True

    def check_now(self, userid):
        return self._engine.check_now(userid)

    def stop_recording(self, userid):
        return self._engine.stop_user_recording(userid)

    def resume_recording(self, userid):
        return self._engine.resume_user_recording(userid)

    # ---------- 录制控制 ----------

    def toggle_run(self):
        if self._engine.running:
            self._engine.stop()
        else:
            if not self._engine.ffmpeg:
                return {"running": False, "error": "没有找到 ffmpeg.exe"}
            self._engine.start()
        return {"running": self._engine.running, "ffmpeg": self._engine.ffmpeg}

    def open_home(self, userid):
        user = self._store.find(userid) or {}
        if user.get("type") == "kuaishou":
            url = self._ks.home_url(user)
            if url:
                webbrowser.open(url)
            return url
        if user.get("type") == "xiaohongshu":
            url = self._xhs.home_url(user)
            if url:
                webbrowser.open(url)
            return url
        if user.get("web_rid"):
            url = f"https://live.douyin.com/{user['web_rid']}"
        elif user.get("sec_uid"):
            url = f"https://www.douyin.com/user/{user['sec_uid']}"
        else:
            return ""
        webbrowser.open(url)
        return url

    def copy_stream(self, userid):
        try:
            url = self._engine.stream_url_of(userid)
        except (DouyinError, KuaishouError, XiaohongshuError, ValueError, OSError) as exc:
            return {"url": "", "error": str(exc)}
        if url:
            self._set_clipboard(url)
        return {"url": url, "error": "" if url else "当前没有直播源（未开播？）"}

    def open_dir(self, userid):
        return self._engine.open_dir(userid)

    def open_save_dir(self):
        folder = Path(self._store.config["save"])
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)  # noqa: S606
        return str(folder)

    def open_recording(self, userid):
        folder = Path(self._engine.open_dir(userid))
        return str(folder)

    def _set_clipboard(self, text):
        return _set_clipboard(text)

    # ---------- 视频转码 ----------

    def list_recordings(self, userid=None):
        root = Path(self._store.config["save"])
        files = []
        if root.exists():
            for path in sorted(root.rglob("*"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
                if path.is_file() and path.suffix.lower() in (".ts", ".flv"):
                    files.append({"path": str(path), "name": path.name,
                                  "folder": path.parent.name,
                                  "size": path.stat().st_size,
                                  "mtime": int(path.stat().st_mtime)})
                if len(files) >= 200:
                    break
        return files

    def transcode(self, source):
        source_path = Path(source)
        if not source_path.exists():
            return {"ok": False, "error": "文件不存在"}
        if not self._engine.ffmpeg:
            return {"ok": False, "error": "没有找到 ffmpeg.exe"}
        target = source_path.with_suffix(".mp4")
        if target.exists():
            target = source_path.with_name(source_path.stem + "_转码.mp4")
        cmd = [self._engine.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
               "-i", str(source_path), "-c", "copy", "-bsf:a", "aac_adtstoasc", str(target)]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0 or not target.exists():
            plain = [self._engine.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
                     "-i", str(source_path), "-c", "copy", str(target)]
            proc = subprocess.run(plain, capture_output=True, text=True,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if target.exists():
            self._engine.log(f"转码完成：{target.name}")
            return {"ok": True, "target": str(target)}
        return {"ok": False, "error": (proc.stderr or "转码失败")[-400:]}

    def open_readme(self):
        readme = APP_DIR / "README.md"
        if readme.exists():
            os.startfile(readme)  # noqa: S606
            return str(readme)
        return ""

    def reveal(self, path):
        target = Path(path)
        if target.exists():
            subprocess.Popen(["explorer", "/select,", str(target)])  # noqa: S607
        return True


def main():
    if getattr(sys, "frozen", False):
        # 打包版：先对一遍完整性。动过任何一个文件就别想开起来。
        ok, reason = guard.verify(RES_DIR, Path(sys.executable))
        if not ok:
            _fatal("程序文件被修改过，无法启动。\n\n" + reason
                   + "\n\n请从原始压缩包重新解压一份。微信DK17179")
            return 3
    mutex, already_running = _claim_single_instance()
    if already_running:
        # 已经开着一个了：把它的窗口叫到前台就行，自己别再开第二个
        _focus_running_instance(f"{APP_NAME} {VERSION}")
        return 0
    try:
        ui_dir = vault.unpack_ui(RES_DIR / "ui.bin")
    except Exception as exc:  # noqa: BLE001
        _fatal("界面资源损坏或被修改，无法启动。\n\n" + str(exc)
               + "\n\n请从原始压缩包重新解压一份。微信DK17179")
        return 3
    api = Api()
    state = WindowState()
    ui = ui_dir / "index.html"
    icon = ui_dir / "app.ico"
    # 有上次的位置就还原，没有就交给系统居中（pywebview 默认 CenterScreen）
    geometry = {"width": WINDOW_WIDTH, "height": WINDOW_HEIGHT}
    geometry.update(_initial_geometry(state))
    window = webview.create_window(
        f"{APP_NAME} {VERSION}",
        url=str(ui),
        js_api=api,
        min_size=(MIN_WIDTH, MIN_HEIGHT),
        **geometry,
    )
    api._window = window
    _remember_geometry(window, state)
    api._tray = _setup_tray(window, api, ui_dir / "app.png")
    if not find_ffmpeg():
        api._engine.log("没有找到 ffmpeg.exe（请把它放到程序目录）")
    webview.start(icon=str(icon) if icon.exists() else None)
    if api._tray is not None:
        try:
            api._tray.stop()
            time.sleep(0.3)   # 等托盘线程把图标从通知区摘掉，免得留个点不动的残影
        except Exception:  # noqa: BLE001
            pass
    api._engine.stop()
    del mutex   # 进程要退了，互斥体跟着这个句柄一起还给系统
    shutil.rmtree(ui_dir, ignore_errors=True)   # 临时解出来的界面资源收掉


if __name__ == "__main__":
    sys.exit(main())
