"""验证「关闭到托盘」这套行为。

用法：python tools/close_tray_test.py [--exe <exe路径>]

两部分：
1) 逻辑：拿假窗口跑一遍 _setup_tray 里注册的那几个回调，看开关开着/关着、点 ×、
   最小化状态下点 ×、以及托盘菜单「退出」分别会走到哪一步（不用真开界面）。
2) 界面：真开打包版，验证最小化是缩到任务栏、点 × 收进托盘、点托盘图标能回到屏幕上。
"""
import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
DEFAULT_EXE = APP / "dist" / "锋芒直播录屏" / "锋芒直播录屏.exe"
TITLE = "锋芒直播录屏 1.0.0"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
ctypes.windll.shcore.SetProcessDpiAwareness(2)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

WM_CLOSE = 0x0010
WM_SYSCOMMAND = 0x0112
SC_MINIMIZE = 0xF020
WM_NOTIFY_PYSTRAY = 0x400 + 11      # pystray 的 WM_NOTIFY
WM_LBUTTONUP = 0x0202
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x102
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def wait_for_window(seconds=40):
    deadline = time.time() + seconds
    while time.time() < deadline:
        hwnd = user32.FindWindowW(None, TITLE)
        if hwnd:
            return hwnd
        time.sleep(0.5)
    return 0


def pid_of(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def still_alive(pid):
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def rect_of(hwnd):
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (rect.left, rect.top, rect.right, rect.bottom)


def on_screen_area(box):
    vx = user32.GetSystemMetrics(76)     # SM_XVIRTUALSCREEN
    vy = user32.GetSystemMetrics(77)
    vw = user32.GetSystemMetrics(78)
    vh = user32.GetSystemMetrics(79)
    left, top, right, bottom = box
    w = max(0, min(right, vx + vw) - max(left, vx))
    h = max(0, min(bottom, vy + vh) - max(top, vy))
    return w * h


def tray_windows(pid):
    found = []

    def cb(hwnd, _):
        if pid_of(hwnd) != pid:
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if "SystemTrayIcon" in buf.value:
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def click_tray(pid):
    icons = tray_windows(pid)
    for icon in icons:
        user32.PostMessageW(icon, WM_NOTIFY_PYSTRAY, 1, WM_LBUTTONUP)
    return len(icons)


def kill_exe(exe):
    subprocess.run(["taskkill", "/IM", Path(exe).name, "/F"], capture_output=True)
    subprocess.run(["taskkill", "/IM", "ffmpeg.exe", "/F"], capture_output=True)
    time.sleep(1.2)


# ---------------- 第一部分：回调逻辑 ----------------

class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        return [handler() for handler in self.handlers]


class FakeWindow:
    """假窗口：只记下 hide / restore / destroy 被调用的顺序。"""

    def __init__(self):
        self.events = type("Events", (), {})()
        self.events.minimized = FakeEvent()
        self.events.restored = FakeEvent()
        self.events.closing = FakeEvent()
        self.calls = []
        self.destroy_blocked = None

    def hide(self):
        self.calls.append("hide")

    def restore(self):
        self.calls.append("restore")

    def show(self):
        self.calls.append("show")

    def destroy(self):
        # 打包版里 window.destroy() 会走 WinForms 的 FormClosing，也就是触发 closing 事件
        self.calls.append("destroy")
        self.destroy_blocked = any(value is False for value in self.events.closing.fire())


class FakeApi:
    def __init__(self):
        self._store = type("Store", (), {})()
        self._store.config = {"closeToTray": False}
        self._engine = type("Engine", (), {})()
        self._engine.log = lambda message: None
        self._engine.stop = lambda: None


def logic_checks():
    sys.path.insert(0, str(APP))
    import app as app_module

    bad = []
    window = FakeWindow()
    api = FakeApi()
    tray = app_module._setup_tray(window, api, APP / "ui" / "app.png")
    if tray is None:
        print("  托盘没建起来（可能有别的实例占着），这部分跳过")
        return bad

    def blocked_by_closing():
        return any(value is False for value in window.events.closing.fire())

    def check(name, want_block, want_calls):
        before = len(window.calls)
        blocked = blocked_by_closing()
        calls = window.calls[before:]
        ok = blocked == want_block and calls == want_calls
        print(f"  {'OK ' if ok else 'BAD'} {name}：拦下={blocked} 动作={calls}")
        if not ok:
            bad.append(name)

    try:
        api._store.config["closeToTray"] = False
        check("开关关着点 × -> 直接退出", False, [])

        api._store.config["closeToTray"] = True
        check("开关打开点 × -> 收进托盘", True, ["hide"])

        window.events.minimized.fire()
        check("最小化状态下点 × -> 先还原再收进托盘", True, ["restore", "hide"])
        window.events.restored.fire()

        quit_item = [item for item in tray.menu.items if str(item) == "退出"][0]
        window.calls.clear()
        quit_item(tray)
        ok = "destroy" in window.calls and window.destroy_blocked is False
        print(f"  {'OK ' if ok else 'BAD'} 托盘菜单「退出」：动作={window.calls} "
              f"关闭被拦={window.destroy_blocked}")
        if not ok:
            bad.append("托盘菜单退出")
    finally:
        try:
            tray.stop()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return bad


# ---------------- 第二部分：真开界面 ----------------

def seed_config(app_dir, close_to_tray):
    path = app_dir / "config.json"
    data = {"config": {}, "user": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    data.setdefault("config", {})["closeToTray"] = close_to_tray
    path.write_text(json.dumps(data, ensure_ascii=False, indent="\t"), encoding="utf-8")


def gui_checks(exe):
    bad = []
    app_dir = exe.parent
    window_json = app_dir / "window.json"
    if window_json.exists():
        window_json.unlink()

    # --- 开关关着：点 × 应该直接退出 ---
    seed_config(app_dir, False)
    kill_exe(exe)
    subprocess.Popen([str(exe)], cwd=str(app_dir))
    hwnd = wait_for_window()
    if not hwnd:
        print("  BAD 窗口没起来")
        return ["窗口没起来"]
    pid = pid_of(hwnd)
    time.sleep(3)
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    deadline = time.time() + 20
    while time.time() < deadline and still_alive(pid):
        time.sleep(0.5)
    ok = not still_alive(pid)
    print(f"  {'OK ' if ok else 'BAD'} 开关关着点 × -> 程序退出：进程还在={not ok}")
    if not ok:
        bad.append("关着没退出")
    kill_exe(exe)

    # --- 开关打开：最小化去任务栏、点 × 收进托盘、点托盘图标能回来 ---
    seed_config(app_dir, True)
    if window_json.exists():
        window_json.unlink()
    subprocess.Popen([str(exe)], cwd=str(app_dir))
    hwnd = wait_for_window()
    if not hwnd:
        print("  BAD 窗口没起来")
        return bad + ["窗口没起来"]
    pid = pid_of(hwnd)
    time.sleep(3)

    def report(name, ok, detail):
        print(f"  {'OK ' if ok else 'BAD'} {name}：{detail}")
        if not ok:
            bad.append(name)

    # 最小化按钮：应该缩到任务栏，窗口还在
    user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
    time.sleep(1.0)
    visible, iconic = bool(user32.IsWindowVisible(hwnd)), bool(user32.IsIconic(hwnd))
    report("点最小化缩到任务栏", visible and iconic,
           f"visible={visible} iconic={iconic}（要 True/True）")
    user32.ShowWindow(hwnd, 9)      # SW_RESTORE
    time.sleep(0.8)

    # 点 ×：应该收进托盘，程序还活着
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    time.sleep(2.0)
    hidden_ok = still_alive(pid) and not user32.IsWindowVisible(hwnd)
    report("点 × 收进托盘", hidden_ok,
           f"进程还在={still_alive(pid)} visible={bool(user32.IsWindowVisible(hwnd))}")

    # 点托盘图标：应该回到屏幕上
    icons = click_tray(pid)
    time.sleep(3.0)
    box = rect_of(hwnd)
    area = on_screen_area(box)
    back_ok = icons > 0 and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) and area > 0
    report("点托盘图标窗口回来", back_ok,
           f"托盘窗口={icons} visible={bool(user32.IsWindowVisible(hwnd))} "
           f"iconic={bool(user32.IsIconic(hwnd))} 屏幕内像素={area}")

    # 先最小化、再点 ×：老毛病就是在这一步藏到 -32000 去
    user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
    time.sleep(1.0)
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    time.sleep(2.0)
    box = rect_of(hwnd)
    hidden_ok = still_alive(pid) and not user32.IsWindowVisible(hwnd) and on_screen_area(box) > 0
    report("最小化状态下点 × 收进托盘", hidden_ok,
           f"进程还在={still_alive(pid)} visible={bool(user32.IsWindowVisible(hwnd))} rect={box}")

    click_tray(pid)
    time.sleep(3.0)
    box = rect_of(hwnd)
    area = on_screen_area(box)
    back_ok = user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) and area > 0
    report("再点托盘图标回到屏幕上", back_ok,
           f"visible={bool(user32.IsWindowVisible(hwnd))} "
           f"iconic={bool(user32.IsIconic(hwnd))} 屏幕内像素={area} rect={box}")

    kill_exe(exe)
    return bad


def main():
    exe = DEFAULT_EXE
    if "--exe" in sys.argv:
        exe = Path(sys.argv[sys.argv.index("--exe") + 1])
    if not exe.exists():
        print("找不到 exe:", exe)
        return 1

    print("== 回调逻辑 ==")
    bad = logic_checks()
    print("== 真开界面 ==", exe)
    bad += gui_checks(exe)
    print("问题数:", len(bad), bad or "")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
