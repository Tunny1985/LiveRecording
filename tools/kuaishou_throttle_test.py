"""快手接口被限流时的两条规矩（不联网）：

1. 限流只是「没查到」的一种原因，不能盖掉别的链路已经查到的结果；
2. 没有分享链接的主播，再粘一条 APP 分享链接应该给这条记录补上，而不是报「已经在列表里」。

用法：python kuaishou_throttle_test.py
"""
import sys
import tempfile
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from core.config import Store  # noqa: E402
from core.kuaishou import API_COOLDOWN, THROTTLE_NOTE, KuaishouClient  # noqa: E402

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append((name, ok, got, want))
    print(f"{'OK ' if ok else 'BAD'} {name}: got={got!r} want={want!r}", flush=True)


def offline_client():
    """不联网的客户端：分享页、页面 SSR、榜单都能按需替换。"""
    client = KuaishouClient()
    client.bootstrap = lambda: None
    return client


def throttled_detail(client):
    """假装接口被限流：写下提示、返回 None。"""
    def fake(principal_id):
        client.last_note = THROTTLE_NOTE
        return None
    return fake


LIVE_INFO = {
    "live": True, "nickname": "在播的主播", "unique_id": "someone", "userid": "someone",
    "title": "标题", "user_count": 12, "avatar": "", "sec_uid": "", "web_rid": "",
    "flv": {"BLUE_RAY": "https://example/flv"}, "hls": {}, "default_res": "蓝光",
}


def test_backoff():
    print("--- 被限流后冷却时间要往后翻倍")
    client = offline_client()
    start = time.time()
    client._count_throttle()
    check("第一次：5 分钟", round((client._api_block_until - start) / 60), API_COOLDOWN // 60)
    for _ in range(6):
        client._count_throttle()
    check("一直撞也不会无限往后放（最多 4 倍）", client._throttled, 4)
    check("冷却上限 20 分钟", round((client._api_block_until - time.time()) / 60), 20)

    calls = []
    client.session.get = lambda *a, **k: calls.append(a)
    check("冷却期内 _detail 直接返回 None", client._detail("someone"), None)
    check("冷却期内不再发请求", calls, [])
    check("冷却期内还是那句提示", client.last_note, THROTTLE_NOTE)


def test_fallback_wins():
    print("--- 接口被限流，但兜底链路查到了：要按查到的算")
    client = offline_client()
    client.share_info = lambda url: None
    client._detail = throttled_detail(client)
    client._page_state = lambda principal_id: None
    client._board_lookup = lambda principal_id: dict(LIVE_INFO)
    info = client.room_info("someone")
    check("在播", info["live"], True)
    check("状态是确定的，不是「无法确认」", info["live_known"], True)
    check("不挂限流提示", info.get("note") or "", "")

    print("--- 一条链路都查不到：才算真的查不到")
    client = offline_client()
    client.share_info = lambda url: None
    client._detail = throttled_detail(client)
    client._page_state = lambda principal_id: None
    client._board_lookup = lambda principal_id: None
    info = client.room_info("someone")
    check("不算开播", info["live"], False)
    check("状态未知（界面显示无法确认）", info["live_known"], False)
    check("提示写明是被限流", info["note"], THROTTLE_NOTE)

    print("--- 有分享链接时走分享页，压根不碰受限的接口")
    client = offline_client()
    client.share_info = lambda url: {"live": False, "live_known": True, "nickname": "分享页给的名字",
                                     "unique_id": "someone", "userid": "someone", "avatar": "",
                                     "title": "", "user_count": 0, "flv": {}, "hls": {},
                                     "default_res": "", "sec_uid": "", "web_rid": ""}
    client._detail = lambda principal_id: (_ for _ in ()).throw(AssertionError("不该打接口"))
    info = client.room_info("someone", "https://v.kuaishou.com/xxxx")
    check("用分享页的结果", info["nickname"], "分享页给的名字")
    check("状态确定", info["live_known"], True)


def test_fill_share_link():
    print("--- 同一位主播补粘分享链接：补上，而不是报重复")
    import app as app_module

    tmp = Path(tempfile.mkdtemp(prefix="fm_ks_")) / "config.json"
    try:
        api = app_module.Api()
        api._store = Store(tmp)                      # 换成临时配置，别动用户那份
        api._engine.log = lambda *a, **k: None       # 日志静音，测试输出干净
        api._store.users = []
        api._store.add_user({"type": "kuaishou", "nickname": "ainii526013", "userid": "5102833285",
                             "unique_id": "ainii526013", "sec_uid": "", "web_rid": "",
                             "avatar": "", "disable": False, "mode": "video", "share_url": ""})
        record = {"type": "kuaishou", "nickname": "狗蛋传奇", "userid": "5102833285",
                  "unique_id": "dandanzuiniu19972025", "sec_uid": "", "web_rid": "",
                  "avatar": "https://example/a.jpg", "disable": False, "mode": "video",
                  "share_url": "https://v.kuaishou.com/JE2dI2R5"}
        api._resolve_line = lambda line: dict(record)

        result = api.add_users("https://v.kuaishou.com/JE2dI2R5")
        check("没有多添一行", len(api._store.users), 1)
        check("算「补上分享链接」不算重复", result["updated"], ["狗蛋传奇"])
        check("没有失败项", result["failed"], [])
        saved = api._store.find("5102833285")
        check("分享链接存下来了", saved["share_url"], "https://v.kuaishou.com/JE2dI2R5")
        check("顺手把快手号换成了真名", saved["nickname"], "狗蛋传奇")

        # 已经有分享链接了，再粘一次就该按重复挡下来
        result = api.add_users("https://v.kuaishou.com/JE2dI2R5")
        check("这一次才算重复", [item["reason"] for item in result["failed"]],
              ["已经在列表里了：狗蛋传奇"])
        check("还是只有一行", len(api._store.users), 1)

        # 抖音的记录不该被快手的分享链接改到
        api._resolve_line = lambda line: {"type": "douyin", "nickname": "别人", "userid": "5102833285",
                                          "unique_id": "", "sec_uid": "", "web_rid": "",
                                          "avatar": "", "disable": False, "mode": "video",
                                          "share_url": "https://v.douyin.com/abc/"}
        result = api.add_users("https://v.douyin.com/abc/")
        check("平台不一样就不动它", result["updated"], [])
        check("分享链接没被串台", api._store.find("5102833285")["share_url"],
              "https://v.kuaishou.com/JE2dI2R5")
    finally:
        import shutil
        shutil.rmtree(tmp.parent, ignore_errors=True)


def test_hint():
    print("--- 被限流时，日志里要给出「去补一条分享链接」这个办法（一位主播只提一次）")
    import shutil

    from core.engine import Engine

    tmp = Path(tempfile.mkdtemp(prefix="fm_ks_hint_")) / "config.json"
    try:
        store = Store(tmp)
        store.users = []
        store.add_user({"type": "kuaishou", "nickname": "某主播", "userid": "5102833285",
                        "unique_id": "aini526013", "sec_uid": "", "web_rid": "",
                        "avatar": "", "disable": False, "mode": "video", "share_url": ""})
        engine = Engine(store)
        logs = []
        engine.log = logs.append
        engine._fetch_info = lambda user: {
            "live": False, "live_known": False, "nickname": "某主播", "note": THROTTLE_NOTE}
        user = store.users[0]
        engine._check(user)
        engine._check(user)
        check("日志里写了限流原因", sum(1 for line in logs if THROTTLE_NOTE in line), 1)
        check("给了「补一条分享链接」的办法",
              sum(1 for line in logs if "分享 → 复制链接" in line), 1)

        # 已经有分享链接的主播不该被这么提示
        store.update_user("5102833285", share_url="https://v.kuaishou.com/JE2dI2R5")
        engine._ks_share_hinted.clear()
        engine._check(store.users[0])
        check("有分享链接的主播不提示", sum(1 for line in logs if "分享 → 复制链接" in line), 1)
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def main():
    test_backoff()
    test_fallback_wins()
    test_fill_share_link()
    test_hint()
    bad = [row for row in RESULTS if not row[1]]
    print("\n失败项：", bad if bad else "无")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
