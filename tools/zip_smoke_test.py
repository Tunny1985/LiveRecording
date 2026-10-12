"""把 outputs 里那个 zip 解到临时目录，直接跑里面的 exe —— 模拟用户拿到压缩包。

解压后文件内容不变，所以完整性校验应该照常通过、主窗口应该正常出来。
"""
import ctypes
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from ctypes import wintypes
from pathlib import Path

WORK = Path(__file__).resolve().parent / "_scratch"
WORK.mkdir(parents=True, exist_ok=True)
ZIP = Path(__file__).resolve().parents[1] / "dist" / "锋芒直播录屏.zip"
MAIN_TITLE = "锋芒直播录屏 1.0.0"
FATAL_TITLE = "锋芒直播录屏"

user32 = ctypes.windll.user32
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]


def main():
    target = Path(tempfile.mkdtemp(prefix="fm_zip_", dir=str(WORK)))
    print("解压到:", target)
    try:
        with zipfile.ZipFile(ZIP) as z:
            z.extractall(target)
            names = z.namelist()
        print("压缩包条目:", len(names))
        inner = target / "锋芒直播录屏"
        exe = inner / "锋芒直播录屏.exe"
        print("exe:", exe, exe.stat().st_size)
        if not (inner / "_internal" / "manifest.bin").exists():
            print("BAD 压缩包里没有 manifest.bin")
            return 1
        # 只盯着自己的界面文件（_internal/ui/ 那套）和用户数据，别把 pywebview 自带的
        # _internal/webview/js/*.js 也算进来
        leaked = [n for n in names
                  if "/ui/" in n or n.endswith(("config.json", "config.json.bak",
                                                "window.json", ".log"))
                  or "录像" in n]
        print("不该有的明文/用户文件:", leaked or "无")
        if leaked:
            return 1

        subprocess.Popen([str(exe)], cwd=str(inner))
        result = "nothing"
        deadline = time.time() + 45
        while time.time() < deadline:
            if user32.FindWindowW(None, MAIN_TITLE):
                result = "main"
                break
            if user32.FindWindowW(None, FATAL_TITLE):
                result = "blocked"
                break
            time.sleep(0.5)
        print("解压后直接运行的结果:", result, "(want main)")
        subprocess.run(["taskkill", "/IM", exe.name, "/F"], capture_output=True)
        subprocess.run(["taskkill", "/IM", "ffmpeg.exe", "/F"], capture_output=True)
        time.sleep(1)
        return 0 if result == "main" else 1
    finally:
        time.sleep(1)
        shutil.rmtree(target, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
