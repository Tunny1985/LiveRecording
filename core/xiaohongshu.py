"""小红书直播网页接口：链接解析、开播状态、直播源地址。

链路（只走小红书自己的网页，不依赖第三方解析服务）：
  APP 分享短链 xhslink.com/o/xxxxx --跳转--> /livestream/<动态段>/<房间号>
  直播间地址 /livestream/<房间号> --手机 UA--> 页面里的 window.__INITIAL_STATE__ 就是
    服务端渲染时写进去的直播间数据：开播状态、房间标题、主播昵称和头像、拉流地址一次拿全
  房间号 --拼固定地址--> http://live.xhscdn.com/live/<房间号>.flv（主播没换流时一直有效）

两个必须记住的坑：
  1. 直播页只有 iOS 客户端 UA 才会服务端渲染出直播数据，PC UA 拿到的 liveStream 是空壳
     （roomId 为 0、没有 deeplink），所以这里所有请求都带 iOS UA。
  2. 主播主页 /user/profile/<id> 只能读出昵称和头像：服务端不给开播状态（实测主播正在播时
     他的主页里也没有房间号），这块官方放在要登录的接口后面。所以小红书只能按
     「直播间链接」监控，主页链接加不进来，见 _profile_hint()。

开播状态认 liveStream.liveStatus：success = 正在播，end = 这一场已经播完；
pageStatus=error 表示房间号不存在。已下播时 roomData.hostInfo 里还留着昵称和头像。
"""
import json
import re
from urllib.parse import parse_qs, unquote, urlparse

import requests

# 小红书只认这一套客户端标识，换成 PC UA 就拿不到直播数据
MOBILE_HEADERS = {
    "user-agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
    "xy-common-params": "platform=iOS&sid=session.1722166379345546829388",
    "referer": "https://app.xhs.cn/",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
# 手机 UA 被风控拦住时拿它再试一次：主页页面 PC UA 也读得到，直播间页面只有手机 UA 才出数据
DESKTOP_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "referer": "https://www.xiaohongshu.com/",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
WEB = "https://www.xiaohongshu.com"
UNAVAILABLE_NOTE = "小红书直播间状态暂时读不到，稍后自动重试"
BLOCKED_HINT = ("小红书这次被要求先验证（短时间连着打开分享短链会这样），过几分钟再试；"
                f"也可以直接粘直播间地址 {WEB}/livestream/xxxxxxxx，这条不经过短链")
SHORT_FAIL_HINT = ("打不开这条小红书分享链接（网络不通，或者这条链接已经被收回了）。"
                   "请重新从直播间里「分享 → 复制链接」拿一条；"
                   f"也可以直接粘直播间地址 {WEB}/livestream/xxxxxxxx")

HOST_RE = re.compile(r"(?:xiaohongshu\.com|xhslink\.com|xhslink\.cn)", re.I)
SHORT_RE = re.compile(r"(?:xhslink\.com|xhslink\.cn)/[A-Za-z0-9/_\-]+", re.I)
# 直播间地址有两种写法：/livestream/570492037891274992 和 /livestream/dynpath34zkiVct/570492037891274992
LIVE_RE = re.compile(r"/livestream/(?:[A-Za-z0-9_\-]+/)?(\d{6,})")
PROFILE_RE = re.compile(r"/user/profile/([0-9A-Za-z_\-]{6,})")
LOGIN_RE = re.compile(r"/(?:website-login|login|captcha)", re.I)
# APP 里「分享 → 复制链接」给的是一整段话（一句话 + 地址），认链接之前先把地址抠出来
URL_RE = re.compile(
    r"(?:https?://)?[A-Za-z0-9\-.]*(?:xiaohongshu\.com|xhslink\.com|xhslink\.cn)/"
    r"[^\s\u3000，。！？、；：\"'）】》〕」]+",
    re.I,
)
TRAIL_RE = re.compile(r"[，。！？、；：）】》\"']+$")


class XiaohongshuError(Exception):
    pass


def looks_like_xiaohongshu(line):
    """只靠字符串判断是不是小红书链接（不联网，用来区分平台）。

    光杆的房间号不认：小红书的房间号和抖音的 web_rid 都是纯数字，认了会互相抢。
    """
    return bool(HOST_RE.search((line or "").strip()))


class XiaohongshuClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": MOBILE_HEADERS["user-agent"]})

    def bootstrap(self):
        """小红书不用预热 cookie，留着是为了和另外两个平台一样被引擎统一调用。"""

    # ---------- 链接解析 ----------

    def parse_input(self, line):
        """把用户粘的一行解析成 {room_id} / {profile} / {share_url}。

        注意用户粘进来的常常是整段分享文字，不是光秃秃一条链接，所以先抠地址。
        """
        url = _grab_url(line)
        if not url:
            return None
        match = LIVE_RE.search(url)
        if match:
            return {"room_id": match.group(1), "share_url": url}
        profile = PROFILE_RE.search(url)
        if profile:
            return {"profile": profile.group(1), "share_url": url}
        if SHORT_RE.search(url):
            return {"share_url": url}
        return None

    # ---------- 主播资料 / 开播状态 ----------

    def resolve(self, parsed):
        """解析成一条可以入库的主播记录。"""
        room_id = str(parsed.get("room_id") or "")
        share_url = str(parsed.get("share_url") or "")
        profile_id = str(parsed.get("profile") or "")
        blocked = False
        had_short = False
        if not room_id and share_url:
            had_short = bool(SHORT_RE.search(share_url))
            landed = self._expand_share(share_url)
            room_id = landed.get("room_id", "")
            profile_id = profile_id or landed.get("profile", "")
            blocked = bool(landed.get("blocked"))
        if not room_id and profile_id:
            # 主页链接认得出来是谁，但服务端不给开播状态：报错时把昵称带上，用户好对照
            raise XiaohongshuError(_profile_hint(self.profile_name(profile_id)))
        if not room_id:
            if blocked:
                raise XiaohongshuError(BLOCKED_HINT)
            raise XiaohongshuError(SHORT_FAIL_HINT if had_short else _profile_hint())
        info = self.room_info(room_id, share_url)
        info["userid"] = room_id
        info["unique_id"] = room_id
        info["web_rid"] = room_id
        return info

    def room_info(self, room_id, share_url=""):
        """开播状态和拉流地址。"""
        room_id = str(room_id or "")
        html, _final, blocked = self._open_page(room_id, share_url)
        state = _state_of(html) if html else None
        if state is None:
            if not room_id:
                raise XiaohongshuError(BLOCKED_HINT if blocked else _profile_hint())
            return self._blank(room_id, note=UNAVAILABLE_NOTE)
        stream = state.get("liveStream") or {}
        room_data = stream.get("roomData") or {}
        info = room_data.get("roomInfo") or {}
        host = room_data.get("hostInfo") or {}
        title = str(info.get("roomTitle") or "").strip()
        real_id = str(info.get("roomId") or "")
        # liveStatus：success = 正在播，end = 这一场已经播完；pageStatus=error 是房间号不存在。
        # 这三档要分开认，不然一场播完会被当成「读不到状态」，界面就一直挂着「无法确认」。
        live_status = str(stream.get("liveStatus") or "")
        page_status = str(stream.get("pageStatus") or "")
        if live_status == "success" and real_id and real_id != "0" and not _is_replay(title):
            return self._normalize(info, real_id, title, host)
        known = live_status == "end" or page_status == "error" or bool(real_id and real_id != "0")
        return self._blank(room_id, note="" if known else UNAVAILABLE_NOTE, host=host)

    # ---------- 取流 ----------

    def pick_stream_url(self, info, definition, stream_type):
        """小红书只给一个档位（原画），所以清晰度选择在这里没有回退的余地。"""
        table = info.get("hls" if stream_type == "m3u8" else "flv") or {}
        if not table:
            table = info.get("flv") or info.get("hls") or {}
        return next(iter(table.values()), "")

    def home_url(self, user):
        room_id = str(user.get("web_rid") or user.get("userid") or "")
        return f"{WEB}/livestream/{room_id}" if room_id else ""

    # ---------- 内部 ----------

    def _open_page(self, room_id, share_url):
        """抓直播页。每轮优先用固定的直播间地址：分享短链打太勤会被小红书拦去验证页。"""
        urls = []
        if room_id:
            urls.append(f"{WEB}/livestream/{room_id}")
        if share_url and share_url not in urls:
            urls.append(share_url)
        blocked = False
        for url in urls:
            html, final, hit_login = self._open(url)
            if html is not None:
                return html, final, False
            blocked = blocked or hit_login
        return None, "", blocked

    def _open(self, url):
        """抓一个页面，撞上「要登录 / 要验证」的页就换个 UA 再试一次。

        返回 (页面内容, 最终地址, 是不是被登录或验证页拦下了)。
        """
        blocked = False
        final = url
        for headers in (MOBILE_HEADERS, DESKTOP_HEADERS):
            try:
                resp = self.session.get(url, timeout=25, allow_redirects=True, headers=headers)
            except requests.RequestException:
                continue
            final = resp.url or url
            if resp.status_code == 200 and not LOGIN_RE.search(final):
                return resp.text, final, False
            if LOGIN_RE.search(final):
                blocked = True
        return None, final, blocked

    def _expand_share(self, share_url):
        """展开分享短链，返回 {room_id} / {profile} / {blocked}。

        小红书拦人时会把落地页换成登录或验证页。这两种要分开：前者是「主页链接本来就
        读不到开播状态」，后者是「你访问太快了，等会儿再来」，提示完全不一样。
        """
        try:
            resp = self.session.get(share_url, timeout=25, allow_redirects=True,
                                    headers=MOBILE_HEADERS)
        except requests.RequestException:
            return {}
        landed = self._locate((resp.url or "") + "\n" + (resp.text or "")[:4000])
        if landed:
            return landed
        if LOGIN_RE.search(resp.url or ""):
            return {"blocked": True}
        return {}

    @staticmethod
    def _locate(text):
        """从地址或页面开头认出直播间 / 主页，登录页的 redirectPath 参数也算一条线索。"""
        for candidate in (text, _redirect_target(text)):
            if not candidate:
                continue
            match = LIVE_RE.search(candidate)
            if match:
                return {"room_id": match.group(1)}
            profile = PROFILE_RE.search(candidate)
            if profile:
                return {"profile": profile.group(1)}
        return {}

    def profile_name(self, host_id):
        """主页链接拿不到开播状态，但认得出是谁，报错时把昵称带上。"""
        html, _final, _blocked = self._open(f"{WEB}/user/profile/{host_id}")
        state = _state_of(html) if html else None
        if not state:
            return ""
        user = ((state.get("profile") or {}).get("userInfo")) or {}
        return str(user.get("nickname") or "")

    def _normalize(self, info, room_id, title, host=None):
        link = _deeplink_params(info.get("deeplink") or "")
        flv, hls = _stream_urls(info, room_id)
        return {
            "live": bool(flv or hls),
            "live_known": True,
            "title": title,
            "user_count": _viewer_count(info.get("displayViewerCount")),
            "nickname": link.get("host_nickname") or _host_text(host, "nickName") or title,
            "userid": room_id,
            "sec_uid": "",
            "unique_id": room_id,
            "avatar": _host_avatar(host) or link.get("host_avatar", ""),
            "web_rid": room_id,
            "flv": flv,
            "hls": hls,
            "cover": info.get("roomCover", ""),
            "default_res": "原画",
            "note": "",
        }

    def _blank(self, room_id, note="", host=None):
        """没开播 / 读不到状态时的空记录。

        已下播时页面还会给着 hostInfo（昵称、头像），顺手带上，列表里名字就不会是空的。
        """
        host = host or {}
        return {
            "live": False,
            "live_known": note == "",
            "title": "",
            "user_count": 0,
            "nickname": _host_text(host, "nickName"),
            "userid": room_id,
            "sec_uid": "",
            "unique_id": room_id,
            "avatar": _host_avatar(host),
            "web_rid": room_id,
            "flv": {},
            "hls": {},
            "default_res": "",
            "note": note,
        }


def _state_of(html):
    """页面里 window.__INITIAL_STATE__ 就是服务端塞进去的直播数据（不是标准 JSON）。"""
    match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html or "")
    if not match:
        return None
    tail = html[match.end():]
    end = tail.find("</script>")
    if end >= 0:
        tail = tail[:end]
    tail = tail.strip().rstrip(";")
    try:
        return json.loads(tail)
    except ValueError:
        pass
    # 里面偶尔混着 JS 的 undefined，退一步把它当 null 再解一次
    try:
        return json.loads(re.sub(r"\bundefined\b", "null", tail))
    except ValueError:
        return None


def _deeplink_params(deeplink):
    """deeplink 是 xhsdiscover 开头的一串参数，里面有昵称、头像和 flv 地址。"""
    query = parse_qs(urlparse(str(deeplink)).query)
    out = {}
    for key in ("room_id", "host_id", "host_nickname", "host_avatar", "flvUrl"):
        if query.get(key):
            out[key] = unquote(query[key][0])
    return out


def _stream_urls(info, room_id):
    """把 pullConfig 拆成 flv / m3u8 两张表，并补上不带签名的固定地址。"""
    raw = info.get("pullConfig")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    plain = {"flv": "", "m3u8": ""}
    signed = {"flv": "", "m3u8": ""}
    if isinstance(raw, dict):
        for group in ("h264", "h265"):
            for entry in raw.get(group) or []:
                url = str((entry or {}).get("master_url") or "")
                if not url:
                    continue
                kind = "m3u8" if ".m3u8" in url else "flv"
                # 带签名的地址过一会儿就失效，同一个流的固定地址排在前面
                bucket = signed if "sign=" in url else plain
                bucket[kind] = bucket[kind] or url
    flv = plain["flv"] or signed["flv"]
    hls = plain["m3u8"] or signed["m3u8"]
    if room_id:
        flv = flv or f"http://live.xhscdn.com/live/{room_id}.flv"
        hls = hls or f"http://live-source-play.xhscdn.com/live/{room_id}.m3u8"
    if flv and not hls:
        hls = flv.replace(".flv", ".m3u8")
    if hls and not flv:
        flv = hls.replace(".m3u8", ".flv")
    return ({"原画": flv} if flv else {}), ({"原画": hls} if hls else {})


def _is_replay(title):
    """页面上的回放也是 liveStatus=success，录它没有意义。"""
    return "回放" in (title or "")


def _profile_hint(name=""):
    """主页链接不是坏了，是小红书不给：把「该怎么办」说清楚，别让人反复试。"""
    who = f"「{name}」的主页" if name else "小红书主页"
    return (f"{who}链接读不到开播状态（官方把它放在了要登录的接口后面）。"
            "请在 APP 里点进 TA 的直播间，用「分享 → 复制链接」把那条直播间链接粘进来；"
            f"或者直接粘直播间地址 {WEB}/livestream/xxxxxxxx")


def _grab_url(text):
    """从「一句话 + 地址」里把地址抠出来，顺手去掉结尾带的中文标点。"""
    match = URL_RE.search(text or "")
    if not match:
        return ""
    url = TRAIL_RE.sub("", match.group(0).strip())
    return url if url.startswith("http") else "https://" + url


def _redirect_target(text):
    """登录页会把真正想去的地方塞在 redirectPath= 里，捞出来看看本来要跳哪。"""
    match = re.search(r"redirectPath=([^&\"'\s]+)", text or "")
    return unquote(match.group(1)) if match else ""


def _host_text(host, key):
    value = (host or {}).get(key)
    return str(value) if isinstance(value, str) else ""


def _host_avatar(host):
    """hostInfo.avatar 有时候是地址字符串，有时候是 {url: ...}，两种都认。"""
    value = (host or {}).get("avatar")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("url") or value.get("urlDefault") or "")
    return ""


def _viewer_count(value):
    """把「1万」「10」这类展示串折成数字给界面用。"""
    text = str(value or "").strip().replace(",", "")
    if not text:
        return 0
    match = re.match(r"([\d.]+)\s*(万|亿)?", text)
    if not match:
        return 0
    number = float(match.group(1))
    unit = match.group(2)
    if unit == "万":
        number *= 10000
    elif unit == "亿":
        number *= 100000000
    return int(number)
