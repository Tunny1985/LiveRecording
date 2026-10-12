"""检查「动过就打不开」：

真跑打包版 exe，逐个把文件改一改，看它是不是只弹一个报错框、不建主窗口；
再检查没人动时照常能开、而且跑过一次之后文件没有被程序自己写脏。
"""
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
DIST = APP / "dist" / "锋芒直播录屏"
INTERNAL = DIST / "_internal"
EXE = DIST / "锋芒直播录屏.exe"
MANIFEST = INTERNAL / "manifest.bin"
MAIN_TITLE = "锋芒直播录屏 1.0.0"
FATAL_TITLE = "锋芒直播录屏"

sys.path.insert(0, str(APP))
from core import guard  # noqa: E402

user32 = ctypes.windll.user32
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

WM_CLOSE = 0x0010
RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append((name, ok, got, want))
    print(f"{'OK ' if ok else 'BAD'} {name}: got={got!r} want={want!r}", flush=True)


def kill():
    subprocess.run(["taskkill", "/IM", EXE.name, "/F"], capture_output=True)
    subprocess.run(["taskkill", "/IM", "ffmpeg.exe", "/F"], capture_output=True)
    time.sleep(1.0)


def dialog_text(hwnd):
    """把报错框里的字读出来，方便确认弹的是不是我们那条。"""
    parts = []

    def cb(child, _):
        buf = ctypes.create_unicode_buffer(600)
        user32.GetWindowTextW(child, buf, 600)
        if buf.value:
            parts.append(buf.value)
        return True

    user32.EnumChildWindows(hwnd, ENUMPROC(cb), 0)
    return " / ".join(parts)


def probe(timeout=25):
    """返回 (结果, 报错框文字)：main=主窗口出来了；blocked=只弹了报错框；nothing=都没有。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if user32.FindWindowW(None, MAIN_TITLE):
            return "main", ""
        fatal = user32.FindWindowW(None, FATAL_TITLE)
        if fatal:
            text = dialog_text(fatal)
            time.sleep(0.4)
            user32.SendMessageW(fatal, WM_CLOSE, 0, 0)
            return "blocked", text
        time.sleep(0.5)
    return "nothing", ""


def run_case(name, expect):
    kill()
    subprocess.Popen([str(EXE)], cwd=str(DIST))
    got, text = probe()
    check(name, got, expect)
    if text:
        print("        报错框:", text.replace("\n", " "))
    kill()


def flip(path, offset=None):
    """把文件里一个字节改掉，返回原内容用来还原。"""
    original = path.read_bytes()
    pos = len(original) // 2 if offset is None else offset
    mutated = bytearray(original)
    mutated[pos] ^= 0xFF
    path.write_bytes(bytes(mutated))
    return original


def main():
    extra = INTERNAL / "我在测试.txt"
    extra.unlink(missing_ok=True)

    run_case("没人动过：正常打开", "main")

    # 程序自己跑一轮之后不能把 _internal 写脏，否则第二次就开不起来了
    ok, reason = guard.verify(INTERNAL, EXE)
    check("跑过一轮后完整性依然通过", ok, True)
    if not ok:
        print("        原因:", reason)

    original = flip(INTERNAL / "ui.bin")
    try:
        run_case("界面资源被改：拒绝启动", "blocked")
    finally:
        (INTERNAL / "ui.bin").write_bytes(original)

    original = flip(INTERNAL / "_asyncio.pyd", offset=1000)
    try:
        run_case("依赖文件被改：拒绝启动", "blocked")
    finally:
        (INTERNAL / "_asyncio.pyd").write_bytes(original)

    original = flip(MANIFEST, offset=100)
    try:
        run_case("完整性清单被改：拒绝启动", "blocked")
    finally:
        MANIFEST.write_bytes(original)

    saved = MANIFEST.read_bytes()
    MANIFEST.unlink()
    try:
        run_case("完整性清单被删：拒绝启动", "blocked")
    finally:
        MANIFEST.write_bytes(saved)

    extra.write_text("多出来的文件", encoding="utf-8")
    try:
        run_case("多了个文件：拒绝启动", "blocked")
    finally:
        extra.unlink(missing_ok=True)

    # exe 被改：不直接改真文件（改坏了 bootloader 就测不到我们的弹窗了），
    # 而是拿一份改过的副本来问 guard 本身
    copy = MANIFEST.parent / "_exe_copy.bin"
    copy.write_bytes(EXE.read_bytes() + b"x")
    try:
        ok, reason = guard.verify(INTERNAL, copy)
        check("主程序被改：校验不通过", ok, False)
        check("主程序被改：原因", reason, "程序主文件被修改过")
    finally:
        copy.unlink(missing_ok=True)

    run_case("改完全部还原：又能正常打开", "main")

    bad = [row for row in RESULTS if not row[1]]
    print("\n失败项：", bad if bad else "无")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
