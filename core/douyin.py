"""抖音网页接口：链接解析、开播状态、直播源地址。

链路（全部走抖音自己的网页接口，不依赖第三方服务）：
  主播主页链接 --解析--> sec_uid
  sec_uid --/aweme/v1/web/user/profile/other/--> 开播状态 + room_data
  room_data --解析--> web_rid + stream_url.flv_pull_url / hls_pull_url_map
  直播间链接 live.douyin.com/<web_rid> --webcast/room/web/enter/--> 同上
"""
import json
import re
import time

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 清晰度 -> 抖音分辨率档位（按优先级回退）
QUALITY_ORDER = {
    "原画": ["FULL_HD1", "HD1", "SD1", "SD2"],
    "蓝光": ["FULL_HD1", "HD1", "SD1", "SD2"],
    "高清": ["HD1", "SD1", "SD2", "FULL_HD1"],
    "标清": ["SD1", "SD2", "HD1", "FULL_HD1"],
    "流畅": ["SD2", "SD1", "HD1", "FULL_HD1"],
}

FEED_PARAMS = {
    "aid": "6383", "app_name": "douyin_web", "live_id": "1", "device_platform": "web",
    "language": "zh-CN", "enter_from": "web_live", "cookie_enabled": "true",
    "screen_width": "1920", "screen_height": "1080", "browser_language": "zh-CN",
    "browser_platform": "Win32", "browser_name": "Chrome", "browser_version": "126.0.0.0",
}

LIVE_URL_RE = re.compile(r"live\.douyin\.com/([0-9]{6,})")
ROOM_ID_RE = re.compile(r"/webcast/(?:reflow/)?(\d{6,})")
SHORT_URL_RE = re.compile(r"v\.douyin\.com/[A-Za-z0-9_\-]+")
SEC_UID_RE = re.compile(r"(?:user/|sec_uid=)(MS4wLjAB[A-Za-z0-9_\-]+)")
BARE_RID_RE = re.compile(r"^\d{6,}$")
# APP 的「复制链接」给的是一整段话（一句话 + 地址），展开短链前先把地址抠出来
URL_RE = re.compile(
    r"(?:https?://)?[A-Za-z0-9\-.]*douyin\.com/[^\s\u3000，。！？、；：\"'）】》〕」]+", re.I)
TRAIL_RE = re.compile(r"[，。！？、；：）】》\"']+$")


class DouyinError(Exception):
    pass


def _first_url(node):
    if isinstance(node, dict):
        urls = node.get("url_list") or []
        if urls:
            return urls[0]
    return ""


class DouyinClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self.last_bootstrap = 0.0

    def bootstrap(self):
        """拿一次首页 cookie（ttwid），后续接口更稳。"""
        if time.time() - self.last_bootstrap < 1800:
            return
        try:
            self.session.get("https://live.douyin.com/", timeout=20)
            self.last_bootstrap = time.time()
        except requests.RequestException:
            pass

    # ---------- 直播间 ----------

    def room_by_web_rid(self, web_rid):
        params = dict(FEED_PARAMS)
        params["web_rid"] = str(web_rid)
        url = "https://live.douyin.com/webcast/room/web/enter/"
        resp = self.session.get(
            url, params=params,
            headers={"Referer": f"https://live.douyin.com/{web_rid}"}, timeout=20,
        )
        if resp.status_code != 200 or not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:
            return {}
        rooms = ((data.get("data") or {}).get("data") or [])
        owner = ((data.get("data") or {}).get("user") or {})
        if not rooms:
            return self._owner_only(owner, web_rid)
        return self._normalize(rooms[0], owner, web_rid)

    # ---------- 主播主页 ----------

    def profile(self, sec_uid):
        url = (
            "https://www.douyin.com/aweme/v1/web/user/profile/other/"
            "?device_platform=webapp&aid=6383&sec_user_id=" + sec_uid
        )
        resp = self.session.get(
            url, headers={"Referer": f"https://www.douyin.com/user/{sec_uid}"}, timeout=20,
        )
        if resp.status_code != 200:
            raise DouyinError(f"主页接口返回 {resp.status_code}")
        data = resp.json()
        user = data.get("user") or {}
        if not user:
            raise DouyinError("没拿到主播资料（链接是否正确 / 是否需要登录）")
        info = {
            "nickname": user.get("nickname", ""),
            "userid": str(user.get("uid", "")),
            "unique_id": user.get("unique_id", ""),
            "sec_uid": user.get("sec_uid", sec_uid),
            "avatar": _first_url(user.get("avatar_thumb")) or _first_url(user.get("avatar_larger")),
            "follow_status": user.get("follow_status", 0),
            "live_status": user.get("live_status", 0),
            "room_id": str(user.get("room_id_str") or user.get("room_id") or "0"),
        }
        info.update(self._from_room_data(user.get("room_data")))
        return info

    def _from_room_data(self, room_data):
        """user.room_data 是开播时才有的一串 JSON，里面有 web_rid 和真实拉流地址。"""
        if isinstance(room_data, str) and room_data:
            try:
                room_data = json.loads(room_data)
            except ValueError:
                room_data = None
        if not isinstance(room_data, dict):
            return {}
        owner = room_data.get("owner") or {}
        normalized = self._normalize(room_data, owner, owner.get("web_rid", ""))
        normalized["live"] = True
        # room_data 里偶尔有空白字段，别让它盖掉主页接口已经拿到的资料
        return {key: value for key, value in normalized.items() if value not in ("", None, {})}

    # ---------- 规范化 ----------

    def _owner_only(self, owner, web_rid):
        return {
            "live": False,
            "title": "",
            "user_count": 0,
            "nickname": owner.get("nickname", ""),
            "userid": str(owner.get("id_str", "")),
            "sec_uid": owner.get("sec_uid", ""),
            "unique_id": owner.get("unique_id", ""),
            "avatar": _first_url(owner.get("avatar_thumb")),
            "web_rid": str(web_rid),
        }

    def _normalize(self, room, owner, web_rid):
        stream = room.get("stream_url") or {}
        return {
            "live": room.get("status") == 2,
            "title": room.get("title", ""),
            "user_count": _as_int(room.get("user_count_str") or room.get("user_count")),
            "nickname": owner.get("nickname", ""),
            "userid": str(owner.get("id_str", "")),
            "sec_uid": owner.get("sec_uid", ""),
            "unique_id": owner.get("unique_id", ""),
            "avatar": _first_url(owner.get("avatar_thumb")),
            "web_rid": str(owner.get("web_rid") or web_rid or ""),
            "flv": dict(stream.get("flv_pull_url") or {}),
            "hls": dict(stream.get("hls_pull_url_map") or {}),
            "default_res": stream.get("default_resolution") or "",
        }

    # ---------- 链接解析 ----------

    def parse_input(self, line):
        """把用户粘的一行链接解析成 {sec_uid} 或 {web_rid}。"""
        line = line.strip()
        if not line:
            return None
        if BARE_RID_RE.match(line):
            return {"web_rid": line}
        if LIVE_URL_RE.search(line):
            return {"web_rid": LIVE_URL_RE.search(line).group(1)}
        sec = SEC_UID_RE.search(line)
        if sec:
            return {"sec_uid": sec.group(1)}
        if SHORT_URL_RE.search(line):
            target = self._expand_short(line)
            if target:
                return self.parse_input(target)
            return None
        return None

    def _expand_short(self, line):
        # 用户粘进来的常常是「一句话 + 链接」，直接拼 https:// 会拼出一个非法地址
        url = _grab_url(line) or (line if line.startswith("http") else "https://" + line)
        try:
            resp = self.session.get(url, timeout=20, allow_redirects=True)
        except requests.RequestException:
            return ""
        for candidate in (resp.url, resp.text[:2000]):
            match = LIVE_URL_RE.search(candidate) or SEC_UID_RE.search(candidate)
            if match:
                return match.group(0)
        return ""

    def resolve(self, parsed):
        """解析成一条可以入库的主播记录。"""
        self.bootstrap()
        if "web_rid" in parsed:
            room = self.room_by_web_rid(parsed["web_rid"])
            if not room.get("sec_uid"):
                raise DouyinError("拿不到该直播间的主播资料")
            room.setdefault("live", False)
            return room
        info = self.profile(parsed["sec_uid"])
        return info

    # ---------- 取流 ----------

    def pick_stream_url(self, info, definition, stream_type):
        table = info.get("hls" if stream_type == "m3u8" else "flv") or {}
        if not table:
            table = info.get("flv") or info.get("hls") or {}
        for key in QUALITY_ORDER.get(definition, QUALITY_ORDER["蓝光"]):
            if table.get(key):
                return table[key]
        return next(iter(table.values()), "")


def _grab_url(text):
    """从「一句话 + 地址」里把地址抠出来，顺手去掉结尾带的中文标点。"""
    match = URL_RE.search(text or "")
    if not match:
        return ""
    url = TRAIL_RE.sub("", match.group(0).strip())
    return url if url.startswith("http") else "https://" + url


def _as_int(value):
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0
