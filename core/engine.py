"""监测与录制引擎：定时查开播 -> 调 ffmpeg 录制 -> 维护界面需要的运行状态。"""
import os
import re
import shutil
import subprocess
import threading
import time
import winsound
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .config import APP_DIR
from .douyin import DouyinClient, DouyinError
from .kuaishou import THROTTLE_NOTE, KuaishouClient, KuaishouError
from .xiaohongshu import XiaohongshuClient, XiaohongshuError

LOG_PATH = APP_DIR / "锋芒直播录屏.log"
LOG_MAX_BYTES = 512 * 1024

FFMPEG_CANDIDATES = [
    APP_DIR / "ffmpeg.exe",
    Path(r"F:\Media\ffmpeg\ffmpeg.exe"),
    Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
]
# 拉流时带的 Referer：三个平台各自校验自己的站点
REFERERS = {
    "douyin": "https://live.douyin.com/",
    "kuaishou": "https://live.kuaishou.com/",
    "xiaohongshu": "https://www.xiaohongshu.com/",
}
BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def find_ffmpeg():
    for path in FFMPEG_CANDIDATES:
        if path.exists():
            return str(path)
    found = shutil.which("ffmpeg")
    return found or ""


def safe_name(text, fallback="主播"):
    cleaned = BAD_CHARS.sub("_", (text or "").strip()).strip(". ")
    return cleaned or fallback


def human_size(num_bytes):
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / 1024 ** 3:.2f}GB"
    if num_bytes >= 1024 ** 2:
        return f"{num_bytes / 1024 ** 2:.1f}MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f}KB"
    return f"{num_bytes}B"


def human_duration(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def log_to_file(line):
    """同时写一份到程序目录的日志文件：打包后没有黑窗口，出问题只能靠它。"""
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            LOG_PATH.replace(LOG_PATH.with_name(LOG_PATH.name + ".old"))
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


class Recorder(threading.Thread):
    """单个主播的录制线程：ffmpeg -c copy，断流/换流后自动另起一段。"""

    def __init__(self, engine, user):
        super().__init__(daemon=True)
        self.engine = engine
        self.user = user
        self.userid = user["userid"]
        self.stop_flag = threading.Event()
        self.restart_flag = threading.Event()
        self.proc = None
        self.session_seconds = 0.0
        self.session_bytes = 0
        self.current_file = ""
        self.last_error = ""
        self.finished_reason = ""
        self.started_at = time.time()
        self.stream_key = ""
        self.use_headers = False
        self.referer = REFERERS.get(user.get("type"), REFERERS["douyin"])
        self.fast_failures = 0
        self.last_count_write = 0.0

    # ---- 对外 ----
    def stop(self, reason="停止"):
        self.finished_reason = reason
        self.stop_flag.set()
        self._kill()

    def request_restart(self, reason="换流分段"):
        self.finished_reason = reason
        self.restart_flag.set()

    # ---- 内部 ----
    def _kill(self):
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

    def _folder(self):
        cfg = self.engine.store.config
        tag = self.user.get("unique_id") or self.userid
        folder = Path(cfg["save"]) / f"{safe_name(self.user.get('nickname'))}_{safe_name(tag, 'id')}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _base(self):
        tag = self.user.get("unique_id") or self.userid
        return f"{safe_name(self.user.get('nickname'))}_{safe_name(tag, 'id')}"

    def _ext(self):
        if self.user.get("mode") == "audio":
            return "aac"
        cfg = self.engine.store.config
        return cfg["outFormat"] if cfg["outFormat"] in ("ts", "mp4", "flv") else "ts"

    def _build_cmd(self, url, out_path, with_stamp):
        cmd = [self.engine.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-nostdin"]
        if self.use_headers:
            cmd += ["-headers", "Referer: " + self.referer + "\r\n"]
        cmd += ["-rw_timeout", "15000000", "-i", url]
        if self.user.get("mode") == "audio":
            cmd += ["-vn", "-c:a", "copy"]
            container = "adts"
        else:
            cmd += ["-c", "copy"]
            container = {"ts": "mpegts", "mp4": "mp4", "flv": "flv"}[self._ext()]
        if with_stamp:
            cmd += ["-f", "segment", "-reset_timestamps", "1", "-strftime", "1"]
            if self.engine.store.config["recTime"]:
                cmd += ["-segment_time", str(int(self.engine.store.config["recTime"]) * 60)]
            if self.engine.store.config["recFileSize"] and container in ("mpegts", "mp4"):
                cmd += [
                    "-segment_size",
                    str(int(self.engine.store.config["recFileSize"]) * 1024 * 1024),
                ]
            pattern = out_path.with_name(out_path.stem + "_%Y-%m-%d_%H%M%S" + out_path.suffix)
            cmd += [str(pattern)]
        else:
            cmd += ["-f", container, str(out_path)]
        return cmd

    def _run_ffmpeg(self, url, with_stamp):
        folder = self._folder()
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = folder / f"{self._base()}_{stamp}.{self._ext()}"
        cmd = self._build_cmd(url, out_path, with_stamp)
        self.engine.log(f"[{self.user.get('nickname')}] 开始录制：{out_path.name}")
        self.current_file = str(out_path)
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            # stderr 用 DEVNULL：没人读的管道写满 64KB 会把 ffmpeg 卡死，录制就断了
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return self.proc

    def run(self):
        cfg = self.engine.store.config
        with_stamp = bool(cfg["recTime"] or cfg["recFileSize"])
        while not self.stop_flag.is_set():
            url = self.engine.current_stream_url(self.userid)
            if not url:
                self.last_error = "没有拿到直播源地址"
                self.engine.log(f"[{self.user.get('nickname')}] {self.last_error}")
                break
            self.stream_key = urlparse(url).path
            self.restart_flag.clear()
            started = time.time()
            proc = self._run_ffmpeg(url, with_stamp)
            elapsed = 0.0
            while True:
                if self.stop_flag.is_set():
                    self._kill()
                    self.finished_reason = self.finished_reason or "停止"
                    return
                code = proc.poll()
                elapsed = time.time() - started
                self.session_seconds += 1
                self.session_bytes = self._session_bytes()
                self.engine.update_recording(self.userid, self)
                if code is not None:
                    break
                if self._limit_reached():
                    self.finished_reason = "达到限制"
                    self._kill()
                    break
                if self.restart_flag.is_set():
                    self._kill()
                    break
                new_url = self.engine.current_stream_url(self.userid)
                if new_url:
                    new_key = urlparse(new_url).path
                    if new_key and self.stream_key and new_key != self.stream_key:
                        if cfg["checkPK"]:
                            self.engine.log(f"[{self.user.get('nickname')}] 直播源变化，自动分段")
                            self._kill()
                            break
                        url = new_url
                time.sleep(1)
            if self.stop_flag.is_set():
                return
            if self.finished_reason == "达到限制":
                self.engine.log(f"[{self.user.get('nickname')}] 达到限制，停止录制")
                return
            if elapsed < 6:
                self.fast_failures += 1
                if not self.use_headers:
                    self.use_headers = True
                    self.engine.log(f"[{self.user.get('nickname')}] 起流失败，带 Referer 重试")
                    continue
                if self.fast_failures >= 3:
                    self.last_error = "起流失败，已停止"
                    self.engine.log(f"[{self.user.get('nickname')}] {self.last_error}")
                    return
            else:
                self.fast_failures = 0
            if not self.engine.is_live(self.userid):
                return
            time.sleep(3)

    def _session_bytes(self):
        if not self.current_file:
            return 0
        folder = Path(self.current_file).parent
        if not folder.exists():
            return 0
        total = 0
        for item in folder.glob(f"{self._base()}*"):
            try:
                if item.stat().st_mtime >= self.started_at - 5:
                    total += item.stat().st_size
            except OSError:
                continue
        return total

    def _limit_reached(self):
        cfg = self.engine.store.config
        if cfg["fileTime"] and self.session_seconds >= int(cfg["fileTime"]) * 60:
            return True
        if cfg["fileSize"] and self.session_bytes >= int(cfg["fileSize"]) * 1024 * 1024:
            return True
        return False


class Engine:
    def __init__(self, store):
        self.store = store
        self.clients = {
            "douyin": DouyinClient(),
            "kuaishou": KuaishouClient(),
            "xiaohongshu": XiaohongshuClient(),
        }
        self.client = self.clients["douyin"]
        self.ffmpeg = find_ffmpeg()
        self.runtime = {}
        self.recorders = {}
        self.running = False
        self.logs = deque(maxlen=400)
        self._thread = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._warned_block = False
        self._ks_share_hinted = set()

    def client_for(self, user):
        return self.clients.get((user or {}).get("type") or "douyin", self.client)

    # ---- 日志 / 状态 ----

    def log(self, message):
        line = f"{datetime.now().strftime('%H:%M:%S')} {message}"
        self.logs.append(line)
        log_to_file(f"{datetime.now().strftime('%Y-%m-%d')} {line}")

    def _slot(self, userid):
        return self.runtime.setdefault(str(userid), {
            "status": "尚未检测", "live": False, "title": "", "user_count": 0,
            "duration": 0, "size": 0, "file": "", "error": "", "checked_at": 0,
            "stream_url": "", "recording": False, "offline_votes": 0,
            "skip_session": False, "note": "",
        })

    def snapshot(self):
        users = []
        for user in self.store.users:
            slot = self._slot(user["userid"])
            users.append({
                **user,
                "status": slot["status"],
                "live": slot["live"],
                "title": slot["title"],
                "user_count": slot["user_count"],
                "duration": human_duration(slot["duration"]),
                "size": human_size(slot["size"]),
                "file": slot["file"],
                "error": slot["error"],
                "recording": slot["recording"],
                "skip_session": slot["skip_session"],
            })
        return {
            "running": self.running,
            "users": users,
            "logs": list(self.logs)[-120:],
            "ffmpeg": self.ffmpeg,
            "config": dict(self.store.config),
        }

    # ---- 运行控制 ----

    def start(self):
        if self.running:
            return
        if not self.ffmpeg:
            self.log("找不到 ffmpeg.exe，请把它放到程序目录")
            return
        self.running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.log("开始检测录制")

    def stop(self):
        self.running = False
        self._stop.set()
        for recorder in list(self.recorders.values()):
            recorder.stop("手动停止")
        self.recorders.clear()
        # 停止后要把每行的「录制中 / 时长 / 大小」清掉，否则界面会一直挂着旧状态
        for slot in self.runtime.values():
            slot.update({
                "status": "尚未检测", "live": False, "recording": False,
                "title": "", "user_count": 0, "duration": 0, "size": 0,
                "file": "", "stream_url": "", "error": "", "offline_votes": 0,
                "skip_session": False, "note": "",
            })
        self.log("停止检测录制")

    def _loop(self):
        for client in self.clients.values():
            client.bootstrap()
        while not self._stop.is_set():
            users = [u for u in self.store.users if not u["disable"]]
            if users:
                with self._lock:
                    for client in self.clients.values():
                        client.bootstrap()
                for user in users:
                    if self._stop.is_set():
                        break
                    try:
                        self._check(user)
                    except Exception as exc:  # noqa: BLE001
                        # 网络抽风、接口改版都不该让整个检测线程悄悄死掉
                        self.log(f"[{user.get('nickname')}] 检测异常：{exc!r}")
            try:
                self._reap()
            except Exception as exc:  # noqa: BLE001
                self.log(f"清理录制状态异常：{exc!r}")
            interval = max(5, int(self.store.config["intervalTime"] or 15))
            for _ in range(interval):
                if self._stop.is_set():
                    break
                time.sleep(1)
        for recorder in list(self.recorders.values()):
            recorder.stop("停止")
        self.recorders.clear()

    def _check(self, user):
        slot = self._slot(user["userid"])
        try:
            info = self._fetch_info(user)
        except Exception as exc:  # noqa: BLE001
            slot["error"] = f"检测失败：{exc}"
            # 偶发的接口抽风不要打断正在进行的录制，也别把状态来回改
            if not slot["recording"] and slot["status"] == "尚未检测":
                slot["status"] = "检测失败"
            self.log(f"[{user.get('nickname')}] 检测失败：{exc}")
            # WinError 10013（WSAEACCES）：本机安全软件拦下了没签名的程序联网。
            # 这不是接口问题，说清楚原因，别让人以为是软件坏了。
            if not self._warned_block and ("10013" in str(exc) or "WSAEACCES" in str(exc)):
                self._warned_block = True
                self.log("提示：联网被本机安全软件拦截（WinError 10013）。请在该软件的"
                         "「联网控制」里把 锋芒直播录屏.exe 和同目录的 ffmpeg.exe 设为「允许」，"
                         "再重新点一次「检测录制」。")
            return
        slot["checked_at"] = time.time()
        slot["error"] = ""
        slot["title"] = info.get("title", "")
        slot["user_count"] = info.get("user_count", 0)
        # 快手接口被限流（result 2）时界面还是显示「未开播」，但得让人知道原因
        note = info.get("note") or ""
        if note != slot.get("note"):
            slot["note"] = note
            if note:
                self.log(f"[{user.get('nickname')}] {note}")
        if note == THROTTLE_NOTE:
            self._hint_kuaishou_share(user)
        # userid 是这一行的主键（列表、录制目录、配置都靠它），检测过程中不许改。
        # 快手分享页给的是数字 user_id、接口兜底给的是快手号，来回覆盖会把条目搞乱。
        for key in ("nickname", "avatar", "web_rid", "unique_id", "sec_uid"):
            if info.get(key) and info[key] != user.get(key):
                self.store.update_user(user["userid"], **{key: info[key]})
                user[key] = info[key]
        was_live = slot["live"]
        # 抖音主页接口偶尔漏掉开播字段，单次「没开播」先不算数，
        # 连续两次才算真下播，避免录制被误停后反复重启。
        # 快手接口限流（result 2）时根本查不到状态，既不算开播也不算下播，保持上一轮结论。
        known = info.get("live_known", True)
        if info.get("live"):
            slot["offline_votes"] = 0
            slot["live"] = True
        elif not known:
            slot["offline_votes"] = 0
        else:
            votes = slot["offline_votes"] + 1
            slot["offline_votes"] = votes
            slot["live"] = was_live and votes < 2
        url = self.client_for(user).pick_stream_url(
            info, self.store.config["definition"], self.store.config["streamType"]
        )
        # 下播后清掉「本轮已手动停止」，下次开播照常录
        if not slot["live"]:
            slot["skip_session"] = False
        if url:
            slot["stream_url"] = url
        elif not slot["live"]:
            slot["stream_url"] = ""
        if slot["live"] and not was_live:
            self.log(f"[{user.get('nickname')}] 开播：{info.get('title', '')[:40]}")
            self._notify_live(user)
        if slot["live"] and user.get("mode") != "audio" and not url:
            slot["error"] = "未取到直播源"
        recorder = self.recorders.get(user["userid"])
        # 只有「检测录制」开着、并且没被右键「停止录制」按掉才真的去录；
        # 右键的「立即检测开播」也只查状态，不录。
        if slot["live"] and url and recorder is None and self.running and not slot["skip_session"]:
            recorder = Recorder(self, dict(user))
            self.recorders[user["userid"]] = recorder
            slot["recording"] = True
            slot["status"] = "录制中"
            recorder.start()
        elif not slot["live"] and recorder is not None:
            recorder.stop("已下播")
            self.recorders.pop(user["userid"], None)
            slot["recording"] = False
            slot["status"] = "未开播"
            slot["duration"] = 0
            slot["size"] = 0
        elif recorder is None:
            if not slot["live"]:
                slot["status"] = "未开播"
            elif slot["skip_session"]:
                slot["status"] = "已停止录制"
            else:
                slot["status"] = "直播中"
        # 这一轮没查到状态：界面写清楚「无法确认」，原因压成一句话摆在同一行上，
        # 完整说明留在日志里（那一行太长会把列表挤花）
        if not known and not slot["recording"]:
            slot["status"] = "无法确认"
            slot["error"] = slot["error"] or (note or "状态暂时读不到，稍后自动重试")

    def _hint_kuaishou_share(self, user):
        """快手接口被限流时，没有分享链接的主播查不到状态。

        分享页 SSR 不吃这个限流，所以让他补一条 APP 分享链接就行：同一位主播再粘一次
        不会再添一行，只是把缺的分享链接补上。一位主播只提醒一次。
        """
        if str(user.get("type") or "") != "kuaishou" or user.get("share_url"):
            return
        uid = str(user.get("userid") or "")
        if not uid or uid in self._ks_share_hinted:
            return
        self._ks_share_hinted.add(uid)
        self.log(f"[{user.get('nickname')}] 这位主播是拿快手号 / PC 链接加的，只能走快手的接口，"
                 "而这台机器的 IP 被快手限流了。在快手 APP 里进 TA 的直播间，用「分享 → 复制链接」"
                 "拿一条 v.kuaishou.com 分享链接，粘到右边「添加主播」框里：同一位主播不会再添一行，"
                 "只会给这条记录补上分享链接，之后就不再吃这个限流。")

    def _fetch_info(self, user):
        """直播间接口（web_rid）状态最准，优先用它；再用主页接口补齐资料。"""
        if (user or {}).get("type") == "kuaishou":
            pid = str(user.get("unique_id") or user.get("userid") or "")
            share_url = str(user.get("share_url") or "")
            if not pid and not share_url:
                raise KuaishouError("缺少快手主播 ID")
            return self.clients["kuaishou"].room_info(pid, share_url)
        if (user or {}).get("type") == "xiaohongshu":
            room_id = str(user.get("web_rid") or user.get("userid") or "")
            if not room_id:
                raise XiaohongshuError("缺少小红书直播间 ID")
            return self.clients["xiaohongshu"].room_info(
                room_id, str(user.get("share_url") or "")
            )
        web_rid = str(user.get("web_rid") or "")
        sec_uid = str(user.get("sec_uid") or "")
        if web_rid:
            info = self.client.room_by_web_rid(web_rid)
            if info and (info.get("live") or info.get("sec_uid")):
                return info
        if sec_uid:
            return self.client.profile(sec_uid)
        if web_rid:
            raise DouyinError("拿不到该直播间的状态")
        raise DouyinError("缺少 sec_uid / web_rid")

    def _reap(self):
        for userid, recorder in list(self.recorders.items()):
            if not recorder.is_alive():
                self.recorders.pop(userid, None)
                slot = self._slot(userid)
                slot["recording"] = False
                slot["status"] = "未开播" if not slot["live"] else "直播中"
                slot["duration"] = 0
                slot["size"] = 0
                if recorder.finished_reason == "达到限制":
                    slot["status"] = "已停止（达到限制）"
                if recorder.last_error:
                    slot["error"] = recorder.last_error

    def _notify_live(self, user):
        if not self.store.config["mp3play"]:
            return
        sound = APP_DIR / "kaibo.mp3"
        if sound.exists():
            try:
                winsound.PlaySound(
                    str(sound), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
                )
            except RuntimeError:
                pass

    # ---- 供录制线程读取 ----

    def current_stream_url(self, userid):
        return self._slot(userid).get("stream_url", "")

    def is_live(self, userid):
        return bool(self._slot(userid).get("live"))

    def update_recording(self, userid, recorder):
        slot = self._slot(userid)
        slot["duration"] = int(recorder.session_seconds)
        slot["size"] = recorder.session_bytes
        slot["file"] = recorder.current_file
        now = time.time()
        gap = max(10, int(self.store.config["intervalTime"] or 15))
        if (self.store.config["showUserCount"] and slot["user_count"]
                and now - recorder.last_count_write >= gap):
            recorder.last_count_write = now
            self._write_count_log(recorder, slot["user_count"])

    def _write_count_log(self, recorder, count):
        path = Path(recorder.current_file).with_name(
            Path(recorder.current_file).stem + "_在线人数.txt"
        )
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now().strftime('%H:%M:%S')} {count}\n")
        except OSError:
            pass

    # ---- 手动操作 ----

    def check_now(self, userid):
        user = self.store.find(userid)
        if not user:
            return "主播不存在"
        self._check(user)
        slot = self._slot(userid)
        if slot["status"] == "尚未检测":
            slot["status"] = "直播中" if slot["live"] else "未开播"
        return slot["status"]

    def stop_user(self, userid):
        """停止某个主播的录制并清掉运行状态（用于移除主播）。"""
        recorder = self.recorders.pop(str(userid), None)
        if recorder:
            recorder.stop("已移除")
        self.runtime.pop(str(userid), None)

    def stop_user_recording(self, userid):
        """右键「停止录制」：只停这一位，本轮直播不再自动续录（下播后自动恢复）。"""
        user = self.store.find(userid)
        if not user:
            return "主播不存在"
        slot = self._slot(userid)
        slot["skip_session"] = True
        recorder = self.recorders.pop(str(userid), None)
        if recorder:
            recorder.stop("手动停止")
            self.log(f"[{user.get('nickname')}] 已停止录制")
        else:
            self.log(f"[{user.get('nickname')}] 已设为停止录制")
        slot.update({"recording": False, "duration": 0, "size": 0, "file": ""})
        slot["status"] = "已停止录制" if slot["live"] else "未开播"
        return "已停止录制" if recorder else "当前没有在录制"

    def resume_user_recording(self, userid):
        """右键「开始录制」：把这一位的手动停止撤掉，交给下一轮检测去录。"""
        user = self.store.find(userid)
        if not user:
            return "主播不存在"
        slot = self._slot(userid)
        slot["skip_session"] = False
        self.log(f"[{user.get('nickname')}] 已恢复录制")
        if slot["live"]:
            slot["status"] = "直播中"
            return "已恢复，下一轮检测开始录制"
        return "已恢复，开播后自动录制"

    def stream_url_of(self, userid):
        user = self.store.find(userid)
        if not user:
            return ""
        slot = self._slot(userid)
        if slot["stream_url"]:
            return slot["stream_url"]
        info = self._fetch_info(user)
        return self.client_for(user).pick_stream_url(
            info, self.store.config["definition"], self.store.config["streamType"]
        )

    def open_dir(self, userid):
        user = self.store.find(userid)
        tag = (user or {}).get("unique_id") or userid
        folder = Path(self.store.config["save"]) / (
            f"{safe_name((user or {}).get('nickname'))}_{safe_name(str(tag), 'id')}"
        )
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)  # noqa: S606
        return str(folder)
