"""界面交互实测：关掉自动录制后能不能再打开、移除主播走不走自己的确认框、
设置里「关闭到托盘」开关点了能不能存进 config.json。

用法：python ui_actions_test.py [--packed]
加 --packed 就用发布版 dist\\锋芒直播录屏\\_internal\\ui.bin 里那份界面跑一遍
（默认跑的是源码 ui\\ 目录）。
"""
import json
import shutil
import sys
import time
from pathlib import Path

import webview

APP = Path(__file__).resolve().parents[1]
WORK = Path(__file__).resolve().parent / "_scratch"
WORK.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(APP))

import app as app_module  # noqa: E402


def pick_ui_dir():
    """默认用源码 ui\\；--packed 时把发布版加密的 ui.bin 解到临时目录再用。"""
    if "--packed" not in sys.argv:
        return APP / "ui", None
    from core import vault

    blob = APP / "dist" / "锋芒直播录屏" / "_internal" / "ui.bin"
    unpacked = vault.unpack_ui(blob)
    return unpacked, unpacked

USERS = [
    {"type": "kuaishou", "nickname": "甲主播", "userid": "jia", "sec_uid": "", "web_rid": "",
     "unique_id": "jia", "avatar": "", "disable": True, "mode": "video", "share_url": ""},
    {"type": "douyin", "nickname": "乙主播", "userid": "5666", "sec_uid": "MS4wLjABAAAAx",
     "web_rid": "5666", "unique_id": "", "avatar": "", "disable": False, "mode": "video",
     "share_url": ""},
    {"type": "xiaohongshu", "nickname": "丙主播", "userid": "570492037891274992", "sec_uid": "",
     "web_rid": "570492037891274992", "unique_id": "570492037891274992", "avatar": "",
     "disable": False, "mode": "video", "share_url": "https://xhslink.com/o/AfYblO6KcOt"},
]

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append((name, ok, got, want))
    print(f"{'OK ' if ok else 'BAD'} {name}: got={got!r} want={want!r}", flush=True)


def driver(window, api):
    time.sleep(3)
    store = api._store
    check("起始：甲主播 disable", store.find("jia")["disable"], True)
    check("三行的平台标记", window.evaluate_js(
        "Array.from(document.querySelectorAll('li.row .sub .platform'))"
        ".map(function (e) { return e.textContent; })"),
        ["快手", "抖音", "小红书"])

    # 第一行是「开始录制」状态，点一下开关应该恢复自动录制
    window.evaluate_js("document.querySelectorAll('li.row')[0]"
                       ".querySelector('[data-role=\"auto-ctl\"]').click();")
    time.sleep(1.5)
    check("点开关后：甲主播 disable", store.find("jia")["disable"], False)
    label = window.evaluate_js("document.querySelectorAll('li.row')[0]"
                               ".querySelector('.auto-label').textContent")
    check("恢复后该行文字", label, "自动录制")

    # 再点一次应该关掉
    window.evaluate_js("document.querySelectorAll('li.row')[0]"
                       ".querySelector('[data-role=\"auto-ctl\"]').click();")
    time.sleep(1.5)
    check("再点一次：甲主播 disable", store.find("jia")["disable"], True)
    label = window.evaluate_js("document.querySelectorAll('li.row')[0]"
                               ".querySelector('.auto-label').textContent")
    check("关掉后该行文字", label, "开始录制")

    # 原生 confirm 不能再出现：移除必须走自己的确认框
    window.evaluate_js("document.querySelectorAll('li.row')[0]"
                       ".querySelector('.pill-danger').click();")
    time.sleep(1.2)
    hidden = window.evaluate_js("document.getElementById('confirmModal').classList.contains('hidden')")
    check("移除时弹出的是自己的确认框", hidden, False)
    check("还没确认时主播还在", store.find("jia") is not None, True)
    window.evaluate_js("document.getElementById('confirmOk').click();")
    time.sleep(1.5)
    check("确认后主播被移除", store.find("jia") is None, True)

    # 设置里那个「关闭到托盘」开关：点一下应该能存进 config.json
    before = store.config.get("closeToTray")
    window.evaluate_js("openSettings();")
    time.sleep(1.2)
    check("设置打开时开关状态跟配置一致",
          window.evaluate_js("document.getElementById('setCloseTray')"
                             ".classList.contains('on')"), bool(before))
    window.evaluate_js("document.getElementById('setCloseTray').click();")
    window.evaluate_js("document.getElementById('btnApplySettings').click();")
    time.sleep(2.0)
    check("点开关再应用：内存里的配置变了", store.config.get("closeToTray"), not before)
    saved = json.loads(Path(store.path).read_text(encoding="utf-8"))["config"]["closeToTray"]
    check("点开关再应用：写进了 config.json", saved, not before)

    api._engine.stop()
    window.destroy()


def main():
    cfg = APP / "config.json"
    ui, tmp_ui = pick_ui_dir()
    print("界面来自:", ui)
    backup = WORK / "_config_backup3.json"
    shutil.copy2(cfg, backup)
    seed = json.loads(cfg.read_text(encoding="utf-8"))
    seed["user"] = USERS
    cfg.write_text(json.dumps(seed, ensure_ascii=False, indent=1), encoding="utf-8")
    api = app_module.Api()
    api._store.users = [dict(user) for user in USERS]
    api._store.save()
    api._engine.log = lambda *args, **kwargs: None
    api._store.config["save"] = str(WORK / "ui_actions_rec")
    window = webview.create_window(f"{app_module.APP_NAME} {app_module.VERSION}",
                                   url=str(ui / "index.html"), js_api=api,
                                   width=app_module.WINDOW_WIDTH, height=app_module.WINDOW_HEIGHT)
    api._window = window
    try:
        webview.start(func=lambda: driver(window, api))
    finally:
        shutil.copy2(backup, cfg)
        backup.unlink(missing_ok=True)
        if tmp_ui:
            shutil.rmtree(tmp_ui, ignore_errors=True)
    bad = [row for row in RESULTS if not row[1]]
    print("\n失败项：", bad if bad else "无")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
