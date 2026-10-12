"""小红书接口自测：在播 / 已下播 / 房间号不存在 / 主页链接，这几种要分得清。

用法：python tools/xhs_client_test.py [正在播的直播间链接]
不传参数时只跑前几种（都不依赖当时有没有人在播）。
"""
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from core.xiaohongshu import XiaohongshuClient, XiaohongshuError  # noqa: E402

# 这个房间早就播完了，拿它当「已下播」的固定样本
ENDED_ROOM = "570492037891274992"

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append((name, ok, got, want))
    print(f"{'OK ' if ok else 'BAD'} {name}: got={got!r} want={want!r}", flush=True)


def main():
    client = XiaohongshuClient()

    print("--- 已下播的直播间（固定样本）")
    info = client.room_info(ENDED_ROOM)
    check("没在播", info["live"], False)
    check("状态是明确的（不是无法确认）", info["live_known"], True)
    check("取流地址为空", client.pick_stream_url(info, "原画", "flv"), "")
    print("   昵称:", info["nickname"], "| 头像:", info["avatar"][:60])

    print("--- 不存在的房间号")
    info = client.room_info("123456")
    check("没在播", info["live"], False)
    check("状态是明确的", info["live_known"], True)

    print("--- 主播主页链接要说清楚为什么加不了")
    try:
        client.resolve({"profile": "5c2ecbde000000000601d6e2",
                        "share_url": "https://www.xiaohongshu.com/user/profile/5c2ecbde000000000601d6e2"})
    except XiaohongshuError as exc:
        message = str(exc)
        check("提示里说明是主页链接", "主页" in message, True)
        check("提示里教了怎么拿直播间链接", "分享" in message and "复制链接" in message, True)
        print("   ", message)
    else:
        check("主页链接应该被拒绝", "没抛异常", "抛 XiaohongshuError")

    print("--- APP 分享出来的主页短链")
    try:
        client.resolve({"share_url": "https://xhslink.cn/o/5fTH9iUjul"})
    except XiaohongshuError as exc:
        print("   ", str(exc)[:160])
        check("被拒绝或者提示被限流", ("主页" in str(exc)) or ("验证" in str(exc)), True)
    else:
        check("主页短链不该被当成直播间", "没抛异常", "抛 XiaohongshuError")

    print("--- 抖音 / 快手链接不该被认成小红书")
    for line in ("https://live.douyin.com/123456789", "https://live.kuaishou.com/u/aini526013"):
        check("不是小红书：" + line[:34], client.parse_input(line), None)

    if len(sys.argv) > 1:
        print("--- 正在播的直播间:", sys.argv[1])
        info = client.resolve(client.parse_input(sys.argv[1]))
        check("在播", info["live"], True)
        check("拿到了取流地址", bool(client.pick_stream_url(info, "原画", "flv")), True)
        print("   昵称:", info["nickname"], "| 房间:", info["web_rid"])
        print("   flv:", client.pick_stream_url(info, "原画", "flv"))
    else:
        print("（没传直播间链接，跳过「在播」那一段）")

    bad = [row for row in RESULTS if not row[1]]
    print("\n失败项：", bad if bad else "无")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
