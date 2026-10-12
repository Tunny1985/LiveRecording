"""真开界面验一验窗口「最小化 / 关闭」两条路走得对不对。

现在的约定：
  * 点标题栏上那个最小化按钮 -> 走 Windows 自己的最小化，缩到任务栏，不进托盘；
  * 点右上角 × -> 看设置里「关闭到托盘」：
        打开   -> 只把窗口收进右下角托盘，程序继续在后台监听、开播照样录；
        没打开 -> 直接退出程序。

驱动方式是真的：托盘那段照着 pystray 的 win32 后端来 —— 它注册一个类名是
``SystemTrayIcon`` 的隐藏窗口，左键点图标时 shell 会给它发 ``WM_NOTIFY``
(WM_USER+11) 带 ``WM_LBUTTONUP``，这里就照原样 PostMessage，程序自己的回调会跑。
关闭用的是真的 ``WM_CLOSE``，跟用户点 × 一模一样。

用法：python tray_repro.py [rounds] [--src | --exe <exe>] [--cover] [--poison]

``--cover`` 在点托盘图标之前压一个全屏窗口，顺带检查「窗口回来了但被压在别的窗口后面」。

``--poison`` 是负对照：窗口收进托盘以后硬把它挪到最小化那个 -32000 占位坐标，再点托盘
图标。这条本来就可能报问题（Windows 记着占位坐标时靠 ShowWindow 拉不回来），留着是为了
说明「为什么关窗口前要先 restore() 再 hide()」。负对照那一段不计进问题数。
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
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.GetTopWindow.restype = wintypes.HWND
user32.GetTopWindow.argtypes = [wintypes.HWND]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

GW_HWNDNEXT = 2
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008

WM_NOTIFY_PYSTRAY = 0x400 + 11     # pystray._util.win32.WM_NOTIFY
WM_LBUTTONUP = 0x0202
WM_CLOSE = 0x0010
SW_MINIMIZE = 6
SW_RESTORE = 9

STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def config_path(exe, src_mode):
    """程序读的是 exe 旁边那份 config.json；源码模式读项目根目录那份。"""
    if src_mode:
        return APP / "config.json"
    return Path(exe).parent / "config.json"


def set_close_to_tray(path, on):
    """把「关闭到托盘」改成 on，其余字段原样不动。"""
    path = Path(path)
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("config", {})["closeToTray"] = bool(on)
    data.setdefault("user", [])
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def process_alive(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def kill(exe):
    subprocess.run(["taskkill", "/IM", Path(exe).name, "/F"], capture_output=True)
    subprocess.run(["taskkill", "/IM", "ffmpeg.exe", "/F"], capture_output=True)
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and "
                    "$_.CommandLine -like '*app.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                   capture_output=True)


def tray_windows(pid):
    found = []

    def cb(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid:
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if "SystemTrayIcon" in buf.value:
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def snapshot(hwnd):
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "iconic": bool(user32.IsIconic(hwnd)),
        "fg": user32.GetForegroundWindow() == hwnd,
        "rect": (rect.left, rect.top, rect.right, rect.bottom),
        "on_monitor": bool(user32.MonitorFromWindow(hwnd, 2)),   # MONITOR_DEFAULTTONEAREST never 0
    }


def intersect_area(rect):
    """屏幕上有多少像素属于这个窗口（所有显示器加一起）。"""
    left, top, right, bottom = rect
    total = 0
    monitors = []
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def cb(_h, _dc, lprc, _data):
        m = lprc.contents
        monitors.append((m.left, m.top, m.right, m.bottom))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    for ml, mt, mr, mb in monitors:
        w = max(0, min(right, mr) - max(left, ml))
        h = max(0, min(bottom, mb) - max(top, mt))
        total += w * h
    return total, monitors


def app_owns_point(app_hwnd, x, y):
    """那个点最上面的窗口是不是 app 自己的窗口树。

    WebView2 的页面内容跑在单独的 msedgewebview2.exe 进程里，比 PID 不可靠，
    所以这里比顶层窗口 —— 页面内容仍然是 app 窗口的子窗口。
    """
    top = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    if not top:
        return False
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    return int(user32.GetAncestor(top, 2)) == int(user32.GetAncestor(app_hwnd, 2))


def window_info(hwnd):
    if not hwnd:
        return None
    cls = ctypes.create_unicode_buffer(120)
    user32.GetClassNameW(hwnd, cls, 120)
    title = ctypes.create_unicode_buffer(160)
    user32.GetWindowTextW(hwnd, title, 160)
    p = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
    return {"hwnd": int(hwnd), "class": cls.value, "title": title.value, "pid": p.value,
            "topmost": bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST),
            "visible": bool(user32.IsWindowVisible(hwnd))}


def describe_above(app_hwnd, cover_hwnd):
    """列出压在 app 上面、可见的普通（非置顶）窗口，看是谁挡住了它。"""
    app_root = user32.GetAncestor(app_hwnd, 2) or app_hwnd       # GA_ROOT
    cover_root = user32.GetAncestor(cover_hwnd, 2) if cover_hwnd else 0
    above, saw_app, saw_cover = [], False, False
    h = user32.GetTopWindow(None)
    while h:
        if int(h) == int(app_root):
            saw_app = True
            break
        if int(h) == int(cover_root):
            saw_cover = True
        if user32.IsWindowVisible(h) and not (user32.GetWindowLongW(h, GWL_EXSTYLE) & WS_EX_TOPMOST):
            buf = ctypes.create_unicode_buffer(120)
            user32.GetWindowTextW(h, buf, 120)
            if buf.value:
                above.append(buf.value)
        h = user32.GetWindow(h, GW_HWNDNEXT)
    return {"app_root": int(app_root), "cover_root": int(cover_root),
            "found_app": saw_app, "found_cover": saw_cover,
            "visible_normal_above": above[:8]}


def cover_window():
    """在 app 之上压一个全屏窗口，返回 (tk, root)；调用方负责 destroy。"""
    import tkinter as tk

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.configure(bg="#204060")
    root.update()
    root.lift()
    user32.SetForegroundWindow(root.winfo_id())
    time.sleep(0.6)
    return tk, root


def wait_window(title, timeout=40.0):
    deadline = time.time() + timeout
    hwnd = 0
    while time.time() < deadline:
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            return hwnd
        time.sleep(0.5)
    return 0


def launch(exe, cmd):
    if cmd:
        return subprocess.Popen(cmd, cwd=str(APP))
    return subprocess.Popen([str(exe)], cwd=str(Path(exe).parent))


def poison_rect(hwnd):
    """负对照用：把窗口硬挪到最小化那个 -32000 占位坐标。"""
    user32.SetWindowPos.restype = ctypes.c_bool
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SetWindowPos(hwnd, None, -32000, -32000, 0, 0, 0x0001 | 0x0004)   # NOSIZE | NOZORDER


def main():
    args = list(sys.argv[1:])
    exe = DEFAULT_EXE
    cmd = None
    if "--exe" in args:
        i = args.index("--exe")
        exe = Path(args[i + 1])
        del args[i:i + 2]
    elif "--src" in args:
        args.remove("--src")
        exe = APP / "venv" / "Scripts" / "pythonw.exe"
        cmd = [str(exe), "app.py"]
    cover = "--cover" in args
    if cover:
        args.remove("--cover")
    poison = "--poison" in args
    if poison:
        args.remove("--poison")
    rounds = int(args[0]) if args else 2
    cfg = config_path(exe, cmd is not None)

    problems = 0
    root = None

    # ---------- 第一段：开关打开 ----------
    set_close_to_tray(cfg, True)
    kill(exe)
    time.sleep(1.2)
    launch(exe, cmd)
    hwnd = wait_window(TITLE)
    if not hwnd:
        print("FAIL: window never appeared")
        return 1
    time.sleep(6)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    print(f"hwnd {hwnd} pid {pid.value} 关闭到托盘=开 start {snapshot(hwnd)}")

    for i in range(rounds):
        if not user32.IsWindow(hwnd):
            print(f"round {i}: 窗口没了（进程还在吗 {process_alive(pid.value)}）")
            problems += 1
            break
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.5)

        # 1) 最小化按钮：只缩到任务栏，不能藏进托盘
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        time.sleep(0.9)
        s_min = snapshot(hwnd)
        bad_min = []
        if not s_min["visible"]:
            bad_min.append("窗口不见了（最小化不该收进托盘）")
        if not s_min["iconic"]:
            bad_min.append("没进任务栏")
        print(f"round {i}: 最小化后 {s_min} -> "
              f"{'BAD ' + '; '.join(bad_min) if bad_min else 'ok（缩到任务栏）'}")
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.7)

        # 2) 点 ×：应该收进托盘，进程继续后台跑
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        time.sleep(2.2)
        s_close = snapshot(hwnd)
        alive = process_alive(pid.value)
        bad_close = []
        if s_close["visible"]:
            bad_close.append("点 × 之后窗口还在屏幕上（没收进托盘）")
        if not alive:
            bad_close.append("点 × 之后进程退出了（应该继续后台跑）")
        if not s_close["on_monitor"]:
            bad_close.append("off-screen")
        print(f"round {i}: 点 × 之后 {s_close} 进程还在={alive} -> "
              f"{'BAD ' + '; '.join(bad_close) if bad_close else 'ok（收进托盘）'}")

        if poison:
            poison_rect(hwnd)
            print("         负对照：把窗口挪到 -32000 ->", snapshot(hwnd)["rect"])

        if cover:
            if root is None:
                _tk, root = cover_window()
            else:
                root.lift()
                user32.SetForegroundWindow(root.winfo_id())
            root.update()
            time.sleep(0.5)

        # 3) 点托盘图标：窗口必须真的回到屏幕上
        icons = tray_windows(pid.value)
        for t in icons:
            user32.PostMessageW(t, WM_NOTIFY_PYSTRAY, 1, WM_LBUTTONUP)
        time.sleep(3.0)
        s_click = snapshot(hwnd)
        area, monitors = intersect_area(s_click["rect"])
        left, top, right, bottom = s_click["rect"]
        on_top = app_owns_point(hwnd, (left + right) // 2, (top + bottom) // 2)
        bad_show = []
        if not s_click["visible"]:
            bad_show.append("still-hidden")
        if s_click["iconic"]:
            bad_show.append("iconic")
        if area == 0:
            bad_show.append("off-screen")
        if cover and not on_top:
            bad_show.append("behind-cover")
        if bad_show and not poison:
            problems += 1
        print(f"round {i}: 点托盘图标后 {s_click} visible_area={area} monitors={monitors} "
              f"on_top={on_top} tray_windows={len(icons)} -> "
              f"{'BAD ' + ','.join(bad_show) if bad_show else 'ok'}"
              + ("（负对照，不计问题）" if bad_show and poison else ""))
        print("         z-order:", describe_above(hwnd, root.winfo_id() if root is not None else 0))
        print("         屏幕中心那个窗口:",
              window_info(user32.WindowFromPoint(wintypes.POINT((left + right) // 2, (top + bottom) // 2))))

        if bad_min or bad_close:
            problems += 1

    if root is not None:
        root.destroy()
    kill(exe)
    time.sleep(1.2)

    # ---------- 第二段：开关关掉，点 × 必须真的退出 ----------
    set_close_to_tray(cfg, False)
    launch(exe, cmd)
    hwnd = wait_window(TITLE)
    if not hwnd:
        print("FAIL: 关掉开关之后窗口没起来")
        kill(exe)
        print("problems:", problems + 1)
        return 1
    time.sleep(6)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    print(f"hwnd {hwnd} pid {pid.value} 关闭到托盘=关")
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    gone = False
    for _ in range(20):
        time.sleep(0.5)
        if not process_alive(pid.value):
            gone = True
            break
    print("开关关着点 × ->", "ok（进程退出了）" if gone else "BAD（进程还在，应该退出）")
    if not gone:
        problems += 1
    kill(exe)
    print("problems:", problems)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
