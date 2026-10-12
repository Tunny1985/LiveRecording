"""只开一个：第二次启动要自己退出，并把已经在跑的那个窗口叫回前台。"""
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
TITLE = "锋芒直播录屏 1.0.0"
PYW = APP / "venv" / "Scripts" / "pythonw.exe"

user32 = ctypes.windll.user32
ctypes.windll.shcore.SetProcessDpiAwareness(2)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.MonitorFromWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


def on_screen_area(hwnd):
    """窗口有多少像素落在显示器上；-32000 那种「已显示但看不到」会得到 0。"""
    if not user32.MonitorFromWindow(hwnd, 0):
        return 0
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (rect.right - rect.left) * (rect.bottom - rect.top)

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append((name, ok, got, want))
    print(f"{'OK ' if ok else 'BAD'} {name}: got={got!r} want={want!r}", flush=True)


def running_apps(exe_name=None):
    """源码版按命令行找 app.py，打包版按进程名找 exe。"""
    if exe_name:
        ps = ("Get-CimInstance Win32_Process -Filter \"Name = '%s'\" | "
              "ForEach-Object { $_.ProcessId }" % exe_name)
    else:
        # 进程名先卡死在 python*，否则跑这条查询的 powershell 自己也会被算进来
        ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
              "Where-Object { $_.CommandLine -like '*app.py*' } | "
              "ForEach-Object { $_.ProcessId }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True)
    return [pid for pid in out.stdout.split() if pid.strip()]


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    for pid in running_apps():
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
    subprocess.run(["taskkill", "/IM", "锋芒直播录屏.exe", "/F"], capture_output=True)
    subprocess.run(["taskkill", "/IM", "ffmpeg.exe", "/F"], capture_output=True)
    time.sleep(1)

    cmd = [target] if target else [str(PYW), "app.py"]
    subprocess.Popen(cmd, cwd=str(Path(target).parent if target else APP))
    hwnd = 0
    for _ in range(80):
        time.sleep(0.5)
        hwnd = user32.FindWindowW(None, TITLE)
        if hwnd:
            break
    if not hwnd:
        print("第一个实例的窗口没起来")
        return 1
    time.sleep(6)
    first = running_apps(Path(target).name if target else None)
    print("第一个实例 pid:", first, flush=True)

    # 先把它缩下去（模拟用户点了最小化），再启动第二个
    user32.SendMessageW(hwnd, 0x0112, 0xF020, 0)      # WM_SYSCOMMAND / SC_MINIMIZE
    time.sleep(2)
    # 最小化按钮现在是缩到任务栏（不是收进托盘），所以窗口的 WS_VISIBLE 还在，
    # 要判的是它处在最小化状态
    check("最小化后窗口缩下去了", bool(user32.IsIconic(hwnd)), True)

    subprocess.Popen(cmd, cwd=str(Path(target).parent if target else APP))
    time.sleep(8)
    second = running_apps(Path(target).name if target else None)
    check("第二次启动没有多开进程", len(second), len(first))
    check("原来的窗口被叫回前台", bool(user32.IsWindowVisible(hwnd)), True)
    check("窗口不再是最小化状态", bool(user32.IsIconic(hwnd)), False)
    check("窗口确实在屏幕里（不是只剩任务栏按钮）", on_screen_area(hwnd) > 0, True)

    for pid in second:
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
    subprocess.run(["taskkill", "/IM", "ffmpeg.exe", "/F"], capture_output=True)
    bad = [row for row in RESULTS if not row[1]]
    print("\n失败项：", bad if bad else "无")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
