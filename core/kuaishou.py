"""快手网页接口：链接解析、开播状态、直播源地址。

链路（全部走快手自己的网页接口，不依赖第三方服务）：
  分享短链 v.kuaishou.com/xxxxx --跳转--> 直播间 /u/<id> 或 主页 /profile/<id>
  v.kuaishou.com/<code> --手机 UA--> 分享页 SSR：页面里 window.INIT_STATE 就是接口
    原样返回的 JSON，昵称 / 头像 / 开播状态 / 全部清晰度地址一次拿全（首选，不会被限流）
  <id> --/live_api/liveroom/livedetail--> 开播状态 + playUrls（次选，IP 被限流时返回 result 2，
    撞上之后 5 分钟内不再重试，免得一直卡在被限流的状态里）
  <id> --直播间页面 SSR--> 同上（备用，页面里就有 __INITIAL_STATE__）
  <id> --直播间列表（分区榜单）--> 兜底索引：只覆盖正在开播且在榜的直播间

拉流地址在 playUrls[].adaptationSet.representation[] 里，按 qualityType 分档：
  STANDARD 高清 / HIGH 超清 / SUPER 蓝光4M / BLUE_RAY 蓝光质臻
分享页 SSR 的档位名是 standard / high / super / blueRay，这里统一映射成上面的档位。
"""
import json
import re
import time

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# 分享页只有手机 UA 才会服务端渲染出直播数据，PC UA 只会拿到一个空壳
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
REFERER = "https://live.kuaishou.com/"
API = "https://live.kuaishou.com/live_api/"

# 连着查，快手会回 result 2（OPERATE_TOO_FAST）。撞上之后先歇 API_COOLDOWN 秒，
# 还撞就翻倍（最多 4 倍）：一直重试只会让这个 IP 一直待在限流状态里。
# 注意这是按 IP 的，换 UA / cookie / Referer 都没用，只能少撞。
API_COOLDOWN = 300
THROTTLE_NOTE = "快手接口返回「请求过快」，本机 IP 被限流，稍后自动重试"

# 分享页 SSR 的档位 -> 接口档位
SHARE_QUALITY = {
    "standard": "STANDARD",
    "high": "HIGH",
    "super": "SUPER",
    "blueRay": "BLUE_RAY",
    "uhd4k": "UHD4K",
    "wqhd2k": "WQHD2K",
}

# 界面上的清晰度 -> 快手档位（按优先级回退）
QUALITY_ORDER = {
    "原画": ["UHD4K", "WQHD2K", "BLUE_RAY", "SUPER", "HIGH", "STANDARD"],
    "蓝光": ["BLUE_RAY", "SUPER", "UHD4K", "WQHD2K", "HIGH", "STANDARD"],
    "高清": ["HIGH", "STANDARD", "SUPER", "BLUE_RAY", "UHD4K", "WQHD2K"],
    "标清": ["STANDARD", "HIGH", "SUPER", "BLUE_RAY", "UHD4K", "WQHD2K"],
    "流畅": ["STANDARD", "HIGH", "SUPER", "BLUE_RAY", "UHD4K", "WQHD2K"],
}

SHORT_RE = re.compile(
    r"(?:v\.kuaishou\.com|kuaishou\.com/f|chenzhongtech\.com/f)/([A-Za-z0-9_\-]{4,})"
)
ROOM_RE = re.compile(
    r"(?:live\.kuaishou\.com/(?:u|live)/|(?:m\.gifshow\.com|livev?\.m\.chenzhongtech\.com)/fw/live/)"
    r"([A-Za-z0-9_\-.]{3,40})"
)
PROFILE_RE = re.compile(
    r"(?:live\.kuaishou\.com/|www\.kuaishou\.com/|kuaishou\.com/)(?:profile|user)/([A-Za-z0-9_\-.]{3,40})"
)
HOST_RE = re.compile(r"(?:kuaishou|gifshow|chenzhongtech)\.com", re.I)
ID_RE = re.compile(r"^[A-Za-z0-9_\-.]{3,40}$")
# APP 里「分享 → 复制链接」给的是一整段话（一句话 + 地址），认链接之前先把地址抠出来
URL_RE = re.compile(
    r"(?:https?://)?[A-Za-z0-9\-.]*(?:kuaishou\.com|gifshow\.com|chenzhongtech\.com)/"
    r"[^\s\u3000，。！？、；：\"'）】》〕」]+",
    re.I,
)
TRAIL_RE = re.compile(r"[，。！？、；：）】》\"']+$")


class KuaishouError(Exception):
    pass


def looks_like_kuaishou(line):
    """只靠字符串判断是不是快手链接（不联网，用来区分平台）。"""
    line = (line or "").strip()
    if not line:
        return False
    if HOST_RE.search(line):
        return True
    # 光杆 ID：抖音的 web_rid 是纯数字，快手的 ID 一般是字母数字混排
    return bool(ID_RE.match(line)) and not line.isdigit()


class KuaishouClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Referer": REFERER,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self.last_bootstrap = 0.0
        self.last_note = ""
        self._api_block_until = 0.0
        self._throttled = 0
        self._index = {}
        self._index_at = 0.0

    def bootstrap(self):
        """先访问一次首页拿 cookie（did / kpn），后面的接口更稳。"""
        if time.time() - self.last_bootstrap < 1800:
            return
        try:
            self.session.get(REFERER, timeout=20)
            self.last_bootstrap = time.time()
        except requests.RequestException:
            pass

    # ---------- 链接解析 ----------

    def parse_input(self, line):
        """把用户粘的一行链接解析成 {principal_id: ...}。

        分享短链能直接展开出快手号；展不开（网络不通 / 链接被限流）时也照样收下，
        后面靠分享页 SSR 拿数据，所以 share_url 一定要跟着记录一起存。
        """
        line = (line or "").strip()
        if not line:
            return None
        # 用户粘进来的常常是「一句话 + 链接」，先抠出地址再认，不然 share_url 会带着整段话
        url = _grab_url(line) or line
        for pattern in (ROOM_RE, PROFILE_RE):
            match = pattern.search(url)
            if match:
                return {"principal_id": match.group(1)}
        if SHORT_RE.search(url):
            parsed = {"share_url": url}
            target = self._expand_short(url)
            if target and target != url and not SHORT_RE.search(target):
                inner = self.parse_input(target)
                if inner:
                    parsed.update(inner)
            return parsed
        if ID_RE.match(url) and not url.isdigit():
            return {"principal_id": url}
        return None

    def _expand_short(self, line):
        """展开 v.kuaishou.com 短链，拿跳转后的真实地址。"""
        url = _grab_url(line) or (line if line.startswith("http") else "https://" + line)
        try:
            resp = self.session.get(url, timeout=20, allow_redirects=True)
        except requests.RequestException:
            return ""
        if resp.url and resp.url != url:
            return resp.url
        # 少数短链只给一个中转页，页面里再带真实地址
        for pattern in (ROOM_RE, PROFILE_RE):
            match = pattern.search(resp.text[:4000])
            if match:
                return match.group(0)
        return ""

    # ---------- 主播资料 / 开播状态 ----------

    def resolve(self, parsed):
        """解析成一条可以入库的主播记录（拿不到资料也要能入库，昵称先用 ID 顶上）。"""
        pid = str(parsed.get("principal_id") or "")
        info = self.room_info(pid, parsed.get("share_url", ""))
        info.setdefault("unique_id", pid)
        info.setdefault("nickname", "")
        info["userid"] = str(info.get("userid") or pid)
        return info

    def room_info(self, principal_id, share_url=""):
        """开播状态 + 拉流地址：优先分享页 SSR，其次接口，最后页面 / 榜单兜底。"""
        principal_id = str(principal_id)
        self.last_note = ""
        self.bootstrap()
        share = self.share_info(share_url) if share_url else None
        known = False
        if share is not None and share.get("live_known"):
            info = share
            known = True
        else:
            # 没有分享链接，或者只是主页分享（里面没有开播状态）时，走接口链路
            info = self._detail(principal_id)
            if info is None:
                info = self._page_state(principal_id)
            if info is None:
                info = self._board_lookup(principal_id)
            if info is not None:
                known = True
            if info is None:
                info = share or {}
        info.pop("live_known", None)
        # 没开播时 detail 返回的作者信息是空的，这时候名字就先用 ID
        info.setdefault("nickname", "")
        pid = info.get("unique_id") or principal_id
        info["userid"] = str(info.get("userid") or pid)
        if not info.get("unique_id"):
            info["unique_id"] = str(pid)
        info.setdefault("live", False)
        # 只要有任意一条链路给出了结果，这一轮的状态就是确定的。
        # 以前这里还多要求「没被限流」，于是接口一限流、兜底链路明明查到了在播，
        # 界面还是显示「无法确认」——命中时显示的是被限流，不是查不到。
        #
        # 一条链路都没给出结果时才算查不到：交给界面显示「无法确认」，不能当成「未开播」，
        # 否则拿快手号加的主播会永远不录。
        info["live_known"] = bool(known)
        if self.last_note and not known and not info.get("note"):
            info["note"] = self.last_note
        return info

    # ---------- 首选链路：分享页 SSR ----------

    def share_info(self, share_url):
        """打开分享链接（手机 UA），解析页面里的 window.INIT_STATE。

        这是快手分享页服务端渲染时就塞进 HTML 的接口原始返回，不用签名、不受接口限流影响，
        昵称、头像、开播状态、在线人数、全部清晰度地址一次拿全。
        """
        for attempt in range(2):
            info = self._share_info_once(share_url)
            if info:
                return info
            if attempt == 0:
                time.sleep(0.8)
        return None

    def _share_info_once(self, share_url):
        final, html = self._fetch_share(share_url)
        if not html:
            return None
        state = _instate_of(html)
        if not state:
            return None
        pid = _id_from_url(final)
        entry = _pick_share_entry(state, pid)
        if entry is None:
            return None
        return _normalize_share(entry, pid)

    def _fetch_share(self, share_url):
        url = _grab_url(share_url) or (
            share_url if share_url.startswith("http") else "https://" + share_url)
        try:
            resp = self.session.get(
                url, timeout=25, allow_redirects=True,
                headers={"User-Agent": MOBILE_UA, "Referer": "https://v.kuaishou.com/",
                         "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            )
        except requests.RequestException:
            return "", ""
        return resp.url or url, resp.text

    def _detail(self, principal_id):
        """快手自己的直播间接口：一次拿到作者、开播状态和全部清晰度地址。

        这条接口是按 IP 限流的：连着查会回 result 2（OPERATE_TOO_FAST）。撞上之后先按
        API_COOLDOWN 歇着，还在报快就往后翻倍放，别一直去撞——撞得越勤，这个 IP 恢复得越慢。
        """
        if time.time() < self._api_block_until:
            self.last_note = THROTTLE_NOTE
            return None
        try:
            resp = self.session.get(
                API + "liveroom/livedetail",
                params={"principalId": principal_id, "authToken": ""},
                headers={"Referer": f"{REFERER}u/{principal_id}"},
                timeout=20,
            )
        except requests.RequestException:
            return None
        if resp.status_code != 200 or not resp.content:
            return None
        try:
            data = (resp.json() or {}).get("data") or {}
        except ValueError:
            return None
        author = data.get("author") or {}
        if not author.get("id"):
            # 接口没给出作者（未开播 / 服务器抽风），交给下面的备用链路
            if data.get("result") == 2:
                self._count_throttle()
            return None
        self._throttled = 0
        stream = data.get("liveStream") or {}
        return self._normalize(stream, author, principal_id,
                               title=stream.get("caption") or stream.get("title") or "")

    def _count_throttle(self):
        """被限流一次就把冷却时间翻一倍（最多翻到 4 倍），把请求频率压下来。"""
        self._throttled = min(self._throttled + 1, 4)
        self.last_note = THROTTLE_NOTE
        self._api_block_until = time.time() + API_COOLDOWN * self._throttled

    def _page_state(self, principal_id):
        """直播间页面的 SSR：页面里的 __INITIAL_STATE__.liveroom 就带着播放地址。"""
        try:
            resp = self.session.get(
                f"{REFERER}u/{principal_id}", timeout=20,
                headers={"Referer": REFERER,
                         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            )
        except requests.RequestException:
            return None
        state = _page_state_of(resp.text)
        if not state:
            return None
        room = (((state.get("liveroom") or {}).get("playList") or [{}])[0] or {})
        author = room.get("author") or {}
        if not author.get("id"):
            return None
        info = self._normalize(room.get("liveStream") or {}, author, principal_id)
        info["title"] = info.get("title") or room.get("caption", "")
        info["user_count"] = info.get("user_count") or _as_int(room.get("watchingCount"))
        info["live"] = bool(room.get("isLiving")) or info["live"]
        return info

    def _normalize(self, stream, author, principal_id, title="", user_count=0):
        flv, hls = _split_streams(stream)
        return {
            "live": bool(flv or hls) or bool(author.get("living")),
            "title": title or stream.get("caption", "") or stream.get("title", ""),
            "user_count": _as_int(user_count or author.get("watchingCount")),
            "nickname": author.get("name", ""),
            "userid": str(author.get("id") or principal_id),
            "sec_uid": "",
            "unique_id": str(author.get("id") or principal_id),
            "avatar": _avatar_url(author),
            "web_rid": "",
            "flv": flv,
            "hls": hls,
            "default_res": (stream.get("quality") or ""),
        }

    # ---------- 兜底：直播间榜单索引 ----------

    def _board_lookup(self, principal_id):
        """接口和页面都拿不到时的兜底：从分区榜单里找这位主播。

        榜单只列正在开播的直播间，所以命中就一定在播；没命中不能判断成未开播。
        结果缓存 90 秒，一整个检测轮次只拉一次。
        """
        if time.time() - self._index_at > 90:
            self._index = self._build_index()
            self._index_at = time.time()
        return self._index.get(str(principal_id))

    def _build_index(self):
        index = {}
        try:
            resp = self.session.get(API + "category/simple", timeout=20)
            games = [item["gameId"] for item in (resp.json()["data"]["hotCategoryList"] or [])]
        except (requests.RequestException, ValueError, KeyError, TypeError):
            games = []
        for game_id in games[:6]:
            try:
                resp = self.session.get(
                    API + "gameboard/list", timeout=20,
                    params={"gameId": game_id, "page": 1, "pageSize": 20, "filterType": 0},
                )
                rooms = (resp.json().get("data") or {}).get("list") or []
            except (requests.RequestException, ValueError, AttributeError):
                continue
            for room in rooms:
                author = room.get("author") or {}
                pid = str(author.get("id") or "")
                if pid:
                    index[pid] = self._normalize(
                        {"playUrls": room.get("playUrls")}, author, pid,
                        title=room.get("caption", ""),
                        user_count=_as_int(room.get("watchingCount")),
                    )
        return index

    # ---------- 取流 / 主页 ----------

    def pick_stream_url(self, info, definition, stream_type):
        table = info.get("hls" if stream_type == "m3u8" else "flv") or {}
        if not table:
            table = info.get("flv") or info.get("hls") or {}
        for key in QUALITY_ORDER.get(definition, QUALITY_ORDER["蓝光"]):
            if table.get(key):
                return table[key]
        return next(iter(table.values()), "")

    def home_url(self, user):
        pid = user.get("unique_id") or user.get("userid")
        return f"{REFERER}u/{pid}" if pid else ""


def _page_state_of(html):
    match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html)
    if not match:
        return None
    try:
        return json.JSONDecoder().raw_decode(html, match.end())[0]
    except ValueError:
        return None


# ---------- 分享页 SSR 解析 ----------

def _deshift(key):
    """分享页把每个接口的 key 逐字符 -1 藏了一下（"tusjoh" 其实是 "string"）。"""
    return "".join(chr(ord(ch) - 1) for ch in key)


def _instate_of(html):
    """分享页的状态是 window.INIT_STATE = {...}，后面跟的是接口原样返回的 JSON。"""
    match = re.search(r"window\.INIT_STATE\s*=\s*", html)
    if not match:
        return None
    try:
        return json.JSONDecoder().raw_decode(html, match.end())[0]
    except ValueError:
        return None


def _id_from_url(url):
    """从跳转后的地址里抠出快手号：/fw/live/<id>、/fw/user/<id>、/u/<id>。"""
    match = re.search(r"/(?:fw/(?:live|user)|u|profile)/([A-Za-z0-9_\-.]{3,40})", url or "")
    return match.group(1) if match else ""


def _entry_ids(entry):
    """一条 SSR 数据里可能带的几个身份字段，用来确认是不是我们要找的主播。"""
    live_user = ((entry.get("liveStream") or {}).get("user") or {})
    profile = ((entry.get("userProfile") or {}).get("profile") or {})
    ids = set()
    for source in (live_user, profile):
        for key in ("kwaiId", "user_id", "principalId", "eid"):
            value = source.get(key)
            if value:
                ids.add(str(value))
    return ids


def _pick_share_entry(state, pid):
    """SSR 里可能缓存了好几条（同一个 JS 文件里带前一次分享的数据），按身份字段挑对的。"""
    candidates = []
    for key, value in (state or {}).items():
        if not isinstance(value, dict):
            continue
        decoded = _deshift(key)
        if "byUser" in decoded:
            candidates.append(value)
        elif "user/profile" in decoded:
            candidates.append(value)
    if not candidates:
        return None
    if pid:
        for entry in candidates:
            if pid in _entry_ids(entry):
                return entry
    return candidates[0] if len(candidates) == 1 else None


def _normalize_share(entry, pid):
    """把分享页 SSR 的数据整理成和接口链路一样的字段结构。"""
    stream = entry.get("liveStream") or {}
    live_user = stream.get("user") or {}
    profile = ((entry.get("userProfile") or {}).get("profile") or {})

    def pick(key):
        return live_user.get(key) or profile.get(key) or ""

    flv, hls = _split_share_streams(stream)
    nickname = pick("user_name")
    if not nickname:
        # 兜底：分享标题里就写着名字（"我正在快手看 XXX 的直播" / "分享了@XXX 的快手主页"）
        title = (entry.get("shareInfo") or {}).get("shareTitle") or ""
        match = re.search(r"[看分享了@]\s*@?([^的]{1,40}?)的", title)
        nickname = match.group(1).strip() if match else ""
    unique_id = str(pick("kwaiId") or pid or "")
    return {
        "live": bool(stream.get("living")),
        "title": stream.get("caption") or "",
        "user_count": _as_int(entry.get("currentWatching")),
        "nickname": nickname,
        "userid": str(pick("user_id") or unique_id),
        "sec_uid": "",
        "unique_id": unique_id,
        "avatar": pick("headurl") or "",
        "web_rid": "",
        "flv": flv,
        "hls": hls,
        "default_res": SHARE_QUALITY.get(stream.get("quality") or "", ""),
        # 主页分享链接里本来就没有开播状态，别把「没数据」当成「没开播」
        "live_known": bool(stream),
    }


def _split_share_streams(stream):
    """分享页的地址：hlsPlayUrl / playUrls[] / multiResolutionHlsPlayUrls[]，统一拆成 flv + m3u8。"""
    flv, hls = {}, {}
    for entry in stream.get("playUrls") or []:
        url = (entry or {}).get("url") or ""
        if not url:
            continue
        if ".m3u8" in url:
            hls.setdefault("BLUE_RAY", url)
        else:
            flv.setdefault("BLUE_RAY", url)
    for item in stream.get("multiResolutionHlsPlayUrls") or []:
        quality = SHARE_QUALITY.get((item or {}).get("type") or "", "")
        urls = (item or {}).get("urls") or []
        url = (urls[0] or {}).get("url") if urls else ""
        if quality and url:
            hls.setdefault(quality, url)
    main = stream.get("hlsPlayUrl") or ""
    if main:
        hls.setdefault("HD", main)
    if not hls and not flv:
        return {}, {}
    return flv, hls


def _avatar_url(author):
    """头像字段各版本不一样：字符串、urls 列表都见过。"""
    for key in ("avatar", "headurl", "headUrl"):
        value = author.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict) and first.get("url"):
                return first["url"]
            if isinstance(first, str):
                return first
    return ""


def _split_streams(stream):
    """把 playUrls 拆成 {清晰度档位: 地址}，flv 和 m3u8 分开。"""
    flv, hls = {}, {}
    entries = stream.get("playUrls")
    if isinstance(entries, dict):
        entries = list(entries.values())
    for entry in entries or []:
        reps = ((entry or {}).get("adaptationSet") or {}).get("representation") or []
        for rep in reps:
            url = rep.get("url") or ""
            quality = rep.get("qualityType") or ""
            if not url or not quality:
                continue
            (hls if ".m3u8" in url else flv).setdefault(quality, url)
    return flv, hls


def _grab_url(text):
    """从「一句话 + 地址」里把地址抠出来，顺手去掉结尾带的中文标点。"""
    match = URL_RE.search(text or "")
    if not match:
        return ""
    url = TRAIL_RE.sub("", match.group(0).strip())
    return url if url.startswith("http") else "https://" + url


def _as_int(value):
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0
