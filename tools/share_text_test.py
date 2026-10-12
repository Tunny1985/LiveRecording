"""三个平台的「整段分享文字」解析自测（不联网）。

APP 里点「分享 → 复制链接」给的不是一条光秃秃的链接，而是一整句话加地址，比如
「我正在直播，快来围观！ https://xhslink.com/o/xxxx 复制这条消息」。
老代码会在这句话前面直接拼 https://，拼出一个带中文和空格的非法地址，
requests 抛 InvalidURL，上层却把这段垃圾原样存成了 share_url，之后每一轮都拿着它去请求，
最后被小红书/快手拦去登录页，提示成了「需要登录」，根因被盖住了。

这里用假的 session 把请求地址截下来看，确认不管外面包着多少话，发出去的都是一条合法地址。
"""
import sys
from pathlib import Path
from urllib.parse import urlsplit

import requests

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from core.douyin import DouyinClient, _grab_url as douyin_url  # noqa: E402
from core.kuaishou import KuaishouClient, looks_like_kuaishou  # noqa: E402
from core.kuaishou import _grab_url as kuaishou_url  # noqa: E402
from core.xiaohongshu import XiaohongshuClient, looks_like_xiaohongshu  # noqa: E402
from core.xiaohongshu import _grab_url as xhs_url  # noqa: E402

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append((name, ok, got, want))
    print(f"{'OK ' if ok else 'BAD'} {name}: got={got!r} want={want!r}", flush=True)


def legal(url, host):
    """requests 愿意发出去的绝对地址：https + 对得上域名，没有空格、没有中文。"""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != host:
        return False
    return not any(ch.isspace() or ord(ch) > 127 for ch in url)


def keep_urls(client):
    """把 session.get 换成只会记录地址的假请求，返回记录列表。"""
    seen = []

    def fake_get(url, *args, **kwargs):
        seen.append(url)
        raise requests.RequestException("测试里不联网")

    client.session.get = fake_get
    return seen


def main():
    print("--- 抠地址：整段话里只留链接，结尾的中文标点去掉")
    check("快手整段话", kuaishou_url("某某正在直播，快来围观 https://v.kuaishou.com/JE2dI2R5 复制此消息"),
          "https://v.kuaishou.com/JE2dI2R5")
    check("小红书整段话", xhs_url("我正在直播，快来围观！ https://xhslink.com/o/AfYblO6KcOt 复制这条消息"),
          "https://xhslink.com/o/AfYblO6KcOt")
    check("抖音整段话", douyin_url("8.88 abc:/ 复制打开抖音 https://v.douyin.com/iRabcdef/ 看TA直播"),
          "https://v.douyin.com/iRabcdef/")
    check("结尾中文逗号", kuaishou_url("看这个 https://v.kuaishou.com/JE2dI2R5，快"), 
          "https://v.kuaishou.com/JE2dI2R5")
    check("光杆快手号不是链接", kuaishou_url("aini526013"), "")
    check("没有链接的纯文字", xhs_url("今天天气不错"), "")

    print("--- 平台识别：整段话也要认得出是哪个平台")
    sentence_ks = "某某正在直播，快来围观 https://v.kuaishou.com/JE2dI2R5 复制此消息"
    sentence_xhs = "我正在直播，快来围观！ https://xhslink.com/o/AfYblO6KcOt 复制这条消息"
    check("识别快手", looks_like_kuaishou(sentence_ks), True)
    check("识别小红书", looks_like_xiaohongshu(sentence_xhs), True)
    check("快手整段话不是小红书", looks_like_xiaohongshu(sentence_ks), False)
    check("小红书整段话不是快手", looks_like_kuaishou(sentence_xhs), False)

    print("--- 快手：整段话进来的 share_url 必须是一条干净地址")
    ks = KuaishouClient()
    seen = keep_urls(ks)
    parsed = ks.parse_input(sentence_ks)
    check("存下来的 share_url 干净", parsed, {"share_url": "https://v.kuaishou.com/JE2dI2R5"})
    check("展开短链发出去的是合法地址", bool(seen) and legal(seen[0], "v.kuaishou.com"), True)
    if seen:
        print("   实际请求:", seen[0])
    check("光杆快手号照样认", ks.parse_input("aini526013"), {"principal_id": "aini526013"})

    print("--- 抖音：整段话进来的也不能拼出非法地址")
    dy = DouyinClient()
    seen = keep_urls(dy)
    dy.parse_input("8.88 abc:/ 复制打开抖音 https://v.douyin.com/iRabcdef/ 看TA直播")
    check("展开短链发出去的是合法地址", bool(seen) and legal(seen[0], "v.douyin.com"), True)
    if seen:
        print("   实际请求:", seen[0])
    check("抖音直播间地址直接认", dy.parse_input("https://live.douyin.com/123456789"),
          {"web_rid": "123456789"})

    print("--- 小红书：整段话和小红书自己的固定地址")
    xhs = XiaohongshuClient()
    check("整段话存下来就是那条短链", xhs.parse_input(sentence_xhs),
          {"share_url": "https://xhslink.com/o/AfYblO6KcOt"})
    check("直播间地址", xhs.parse_input("https://www.xiaohongshu.com/livestream/570488896491836575"),
          {"room_id": "570488896491836575",
           "share_url": "https://www.xiaohongshu.com/livestream/570488896491836575"})
    check("抖音链接不是小红书", xhs.parse_input("https://live.douyin.com/123456789"), None)
    check("快手链接不是小红书", xhs.parse_input("https://live.kuaishou.com/u/aini526013"), None)

    bad = [row for row in RESULTS if not row[1]]
    print("\n失败项：", bad if bad else "无")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
