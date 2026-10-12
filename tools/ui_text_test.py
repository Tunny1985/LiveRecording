"""检查界面文案：顶栏说明的位置、使用说明里那行微信，并且这些内容是从加密的
ui.bin 里解出来的（也就是用户装到手上那份）。

用法: python ui_text_test.py
"""
import ctypes
import shutil
import sys
import time
from ctypes import wintypes
from pathlib import Path

import webview
from PIL import ImageGrab

APP = Path(__file__).resolve().parents[1]
WORK = Path(__file__).resolve().parent / "_scratch"
WORK.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(APP))

from core import vault  # noqa: E402

TITLE = "锋芒直播录屏 1.0.0"
NOTE = "锋芒直播录屏，永久免费给同行的录屏工具。"

user32 = ctypes.windll.user32
ctypes.windll.shcore.SetProcessDpiAwareness(2)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append((name, ok, got, want))
    print(f"{'OK ' if ok else 'BAD'} {name}: got={got!r} want={want!r}", flush=True)


def shot(png):
    hwnd = user32.FindWindowW(None, TITLE)
    if not hwnd:
        return
    user32.SetWindowPos(hwnd, wintypes.HWND(-2), 20, 20, 0, 0, 0x0001 | 0x0040)
    user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002)
    time.sleep(0.8)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom),
                   all_screens=True).save(png)
    user32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, 0x0001 | 0x0002)
    print("截图:", png)


def driver(window, api, ui_dir):
    time.sleep(3)
    print("界面来自:", ui_dir)
    check("顶栏说明文字", window.evaluate_js("document.querySelector('.top-note').textContent"), NOTE)
    check("说明排在视频转码前面",
          window.evaluate_js("document.querySelector('.top-actions').firstElementChild.className"),
          "top-note")
    check("视频转码还是第二个",
          window.evaluate_js("document.querySelector('.top-actions').children[1].textContent"),
          "视频转码")
    check("平台示例一共三行", window.evaluate_js(
        "document.querySelectorAll('.formats .format-row').length"), 3)
    check("第三行是小红书", window.evaluate_js(
        "document.querySelectorAll('.formats .format-row')[2]"
        ".querySelector('.pl-tag').textContent"), "小红书")
    # 150% 缩放的屏上 1px 会被算成 0.666667px，所以只确认「有线」而不是量宽度
    check("平台之间有分隔线", window.evaluate_js(
        "(function () {"
        "  var s = getComputedStyle(document.querySelectorAll('.formats .format-row')[1]);"
        "  return s.borderTopStyle + ':' + (parseFloat(s.borderTopWidth) > 0);"
        "})()"), "solid:true")
    check("每条示例都标了链接类型", window.evaluate_js(
        "document.querySelectorAll('.formats .fmt i').length"), 9)
    check("三种链接类型都在", window.evaluate_js(
        "(function () {"
        "  var t = [].map.call(document.querySelectorAll('.formats .fmt i'),"
        "    function (e) { return e.textContent; });"
        "  return ['PC链接', 'APP直播间', 'APP主页'].every(function (k) {"
        "    return t.indexOf(k) >= 0; });"
        "})()"), True)
    check("示例地址用 xxx 占位", window.evaluate_js(
        "[].every.call(document.querySelectorAll('.formats code'),"
        "  function (e) { return /xxx$/.test(e.textContent); })"), True)

    window.evaluate_js("document.getElementById('readmeModal').classList.remove('hidden');")
    time.sleep(0.8)
    check("使用说明最后一行", window.evaluate_js(
        "document.querySelector('#readmeModal .dialog-body').lastElementChild.textContent"),
        "微信DK17179")
    check("说明正文还在", window.evaluate_js(
        "document.querySelectorAll('#readmeModal .doc-text').length"), 3)
    shot(WORK / "ui_readme.png")

    api._engine.stop()
    window.destroy()


def main():
    ui_dir = vault.unpack_ui(APP / "ui.bin")
    api = __import__("app").Api()
    api._engine.log = lambda *args, **kwargs: None
    window = webview.create_window(TITLE, url=str(ui_dir / "index.html"), js_api=api,
                                   width=1120, height=760)
    api._window = window
    try:
        webview.start(func=lambda: driver(window, api, ui_dir))
    finally:
        shutil.rmtree(ui_dir, ignore_errors=True)
    bad = [row for row in RESULTS if not row[1]]
    print("\n失败项：", bad if bad else "无")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
