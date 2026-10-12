"""打开源码版界面截图，方便改完布局先看效果。

用法：python ui_shot.py <输出png> [--exe <exe路径>]
截图前会把 demo 主播写进 config.json（原文件备份成 config.json.bak）。
"""
import ctypes
import json
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab

APP = Path(__file__).resolve().parents[1]
WORK = Path(__file__).resolve().parent / "_scratch"
WORK.mkdir(parents=True, exist_ok=True)
TITLE = "锋芒直播录屏 1.0.0"

DEMO_USERS = [
    {"type": "kuaishou", "nickname": "狗蛋传奇10月11日生日庆典", "userid": "5102833285",
     "sec_uid": "", "web_rid": "", "unique_id": "dandanzuiniu19972025",
     "avatar": "", "disable": False, "mode": "video",
     "share_url": "https://v.kuaishou.com/JE2dI2R5"},
    {"type": "kuaishou", "nickname": "aini526013", "userid": "aini526013", "sec_uid": "",
     "web_rid": "", "unique_id": "aini526013", "avatar": "", "disable": True,
     "mode": "video", "share_url": ""},
    {"type": "douyin", "nickname": "抖音示例主播", "userid": "1234567890123",
     "sec_uid": "MS4wLjABAAAAexample", "web_rid": "123456789", "unique_id": "",
     "avatar": "", "disable": False, "mode": "video", "share_url": ""},
]

user32 = ctypes.windll.user32
ctypes.windll.shcore.SetProcessDpiAwareness(2)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
NOTOPMOST, TOPMOST = wintypes.HWND(-2), wintypes.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040


def kill():
    subprocess.run(["taskkill", "/IM", "锋芒直播录屏.exe", "/F"], capture_output=True)
    subprocess.run(["taskkill", "/IM", "ffmpeg.exe", "/F"], capture_output=True)
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | "
         "Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*app.py*' } | "
         "ForEach-Object { $_.ProcessId }"],
        capture_output=True, text=True)
    for pid in out.stdout.split():
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)


def main():
    out_png = Path(sys.argv[1]) if len(sys.argv) > 1 else WORK / "ui_shot.png"
    exe = None
    if "--exe" in sys.argv:
        exe = Path(sys.argv[sys.argv.index("--exe") + 1])
    target_dir = exe.parent if exe else APP
    cfg = target_dir / "config.json"
    win = target_dir / "window.json"
    # 截图要往 config.json 里塞演示主播，跑完必须原样放回去：
    # 以前只在第一次跑时留一份 .bak，跑完不管，用户自己那份主播列表就被演示数据顶掉了。
    saved_cfg = cfg.read_bytes() if cfg.exists() else None
    saved_win = win.read_bytes() if win.exists() else None
    if saved_cfg is not None and not (target_dir / "config.json.bak").exists():
        shutil.copy2(cfg, target_dir / "config.json.bak")
    try:
        seed = {"config": {}, "user": DEMO_USERS}
        if saved_cfg is not None:
            seed["config"] = json.loads(saved_cfg.decode("utf-8")).get("config", {})
        cfg.write_text(json.dumps(seed, ensure_ascii=False, indent=1), encoding="utf-8")
        win.unlink(missing_ok=True)
        return shoot(target_dir, exe, out_png)
    finally:
        if saved_cfg is not None:
            cfg.write_bytes(saved_cfg)
        else:
            cfg.unlink(missing_ok=True)
        if saved_win is not None:
            win.write_bytes(saved_win)
        else:
            win.unlink(missing_ok=True)
        kill()


def shoot(target_dir, exe, out_png):
    kill()
    time.sleep(1)
    if exe:
        subprocess.Popen([str(exe)], cwd=str(target_dir))
    else:
        subprocess.Popen([str(APP / "venv" / "Scripts" / "pythonw.exe"), "app.py"], cwd=str(APP))
    hwnd = None
    for _ in range(80):
        time.sleep(0.5)
        hwnd = user32.FindWindowW(None, TITLE)
        if hwnd:
            break
    if not hwnd:
        print("窗口没起来")
        return 1
    time.sleep(8)
    user32.SetWindowPos(hwnd, NOTOPMOST, 20, 20, 0, 0, SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.SetWindowPos(hwnd, TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)
    time.sleep(1.0)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom),
                   all_screens=True).save(out_png)
    user32.SetWindowPos(hwnd, NOTOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)
    print("窗口:", (rect.right - rect.left, rect.bottom - rect.top), "->", out_png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
