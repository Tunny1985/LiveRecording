"""小红书链路实测：用加密的 ui.bin 跑真界面 + 真引擎，加一条小红书直播间链接，
看它认不认得出平台、能不能自己检测到开播并录起来。

用法：python tools/xhs_ui_test.py [直播间链接]
主播当时没开播的话，只验界面那部分，录制断言自动跳过（不算失败）。
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
from core import vault  # noqa: E402
from core.xiaohongshu import XiaohongshuClient, XiaohongshuError  # noqa: E402

LINK = "https://xhslink.com/o/AfYblO6KcOt"
WATCH_SECONDS = 45

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append((name, ok, got, want))
    print(f"{'OK ' if ok else 'BAD'} {name}: got={got!r} want={want!r}", flush=True)


def row_text(window, index=0):
    return window.evaluate_js(
        "(function () {"
        "  var li = document.querySelectorAll('li.row')[%d];"
        "  return li ? li.textContent.replace(/\\s+/g, ' ').trim() : '';"
        "})()" % index)


def driver(window, api, room_id):
    time.sleep(3)
    check("平台标记", window.evaluate_js(
        "document.querySelector('li.row .sub .platform').textContent"), "小红书")
    check("这一行认得出主播", window.evaluate_js(
        "document.querySelector('li.row .nick').textContent") != "未命名主播", True)

    if not room_id:
        print("主播当前没开播，跳过录制部分")
        api._engine.stop()
        window.destroy()
        return

    print("开始检测录制 ...", flush=True)
    api.toggle_run()
    deadline = time.time() + WATCH_SECONDS
    recorded = False
    while time.time() < deadline:
        time.sleep(2)
        text = row_text(window)
        if "录制中" in text:
            recorded = True
            print("   ", text, flush=True)
            break
    check("自己检测到开播并录制", recorded, True)
    if recorded:
        check("行里带上了时长", "时长" in row_text(window), True)
    api.toggle_run()
    api._engine.stop()
    window.destroy()


def main():
    link = sys.argv[1] if len(sys.argv) > 1 else LINK
    client = XiaohongshuClient()
    parsed = client.parse_input(link)
    if not parsed:
        print("链接不认得:", link)
        return 1
    info = client.resolve(parsed)
    room_id = str(info.get("web_rid") or "")
    print("直播间:", room_id, "在播:", info.get("live"), "昵称:", info.get("nickname"))

    record = {
        "type": "xiaohongshu", "nickname": info.get("nickname") or "小红书主播",
        "userid": room_id, "sec_uid": "", "web_rid": room_id, "unique_id": room_id,
        "avatar": info.get("avatar", ""), "disable": False, "mode": "video",
        "share_url": parsed.get("share_url", link),
    }

    cfg = APP / "config.json"
    backup = WORK / "_config_backup_xhs.json"
    shutil.copy2(cfg, backup)
    saved_save = None
    try:
        seed = json.loads(cfg.read_text(encoding="utf-8"))
        saved_save = seed.get("config", {}).get("save")
        seed["config"]["save"] = str(WORK / "xhs_ui_rec")
        seed["config"]["intervalTime"] = 5
        seed["user"] = [record]
        cfg.write_text(json.dumps(seed, ensure_ascii=False, indent=1), encoding="utf-8")

        ui_dir = vault.unpack_ui(APP / "dist" / "锋芒直播录屏" / "_internal" / "ui.bin")
        api = app_module.Api()
        api._store.users = [dict(record)]
        api._store.save()
        window = webview.create_window(f"{app_module.APP_NAME} {app_module.VERSION}",
                                       url=str(ui_dir / "index.html"), js_api=api,
                                       width=app_module.WINDOW_WIDTH,
                                       height=app_module.WINDOW_HEIGHT)
        api._window = window
        webview.start(func=lambda: driver(window, api, room_id if info.get("live") else ""))
        shutil.rmtree(ui_dir, ignore_errors=True)
    finally:
        shutil.copy2(backup, cfg)
        backup.unlink(missing_ok=True)
    del saved_save
    bad = [row for row in RESULTS if not row[1]]
    print("\n失败项：", bad if bad else "无")
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except XiaohongshuError as exc:
        print("小红书链接解析失败:", exc)
        sys.exit(2)
