const QUALITIES = ["原画", "蓝光", "高清", "标清", "流畅"];
const FORMATS = [
  { value: "ts", label: "录制TS" },
  { value: "mp4", label: "录制MP4" },
  { value: "flv", label: "录制FLV" },
];

const state = {
  config: {},
  users: [],
  running: false,
  selected: null,
  pendingSettings: null,
  transcodeFile: null,
  timer: null,
};

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

function toast(text, ms = 2200) {
  const el = $("toast");
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), ms);
}

/* 自己画的确认框：系统自带的 confirm 是块深色方框，和界面完全不是一个风格 */
function askConfirm(text, okText = "确定", title = "确认") {
  return new Promise((resolve) => {
    $("confirmTitle").textContent = title;
    $("confirmText").textContent = text;
    $("confirmOk").textContent = okText;
    const modal = $("confirmModal");
    const finish = (answer) => {
      modal.classList.add("hidden");
      $("confirmOk").onclick = null;
      $("confirmCancel").onclick = null;
      resolve(answer);
    };
    $("confirmOk").onclick = () => finish(true);
    $("confirmCancel").onclick = () => finish(false);
    modal.classList.remove("hidden");
  });
}

function fmtSize(bytes) {
  if (bytes >= 1024 ** 3) return (bytes / 1024 ** 3).toFixed(2) + "GB";
  if (bytes >= 1024 ** 2) return (bytes / 1024 ** 2).toFixed(1) + "MB";
  if (bytes >= 1024) return Math.round(bytes / 1024) + "KB";
  return bytes + "B";
}

function fmtCount(value) {
  if (!value) return "";
  if (value >= 10000) return "在线 " + (value / 10000).toFixed(1) + "万";
  return "在线 " + value;
}

/* ---------------- 渲染 ---------------- */

function ensureRow(list, user) {
  let li = list.querySelector(`li[data-uid="${user.userid}"]`);
  if (li) return li;
  li = document.createElement("li");
  li.className = "row";
  li.dataset.uid = user.userid;
  li.innerHTML = `
    <img class="avatar" alt="" />
    <div class="meta"><div class="nick"></div><div class="sub"></div></div>
    <div class="row-right">
      <div class="pills"></div>
      <div class="auto-ctl" data-role="auto-ctl">
        <span class="auto-label"></span>
        <button class="switch" data-role="auto"></button>
      </div>
    </div>`;
  const avatar = li.querySelector(".avatar");
  // 加载失败就藏起来，并把 url 清掉，下一轮刷新会再试一次
  avatar.addEventListener("error", () => {
    avatar.style.visibility = "hidden";
    avatar.dataset.url = "";
  });
  li.querySelector(".pills").append(dangerPill("移除", () => removeUser(user.userid)));
  // 开关同时充当「开始录制 / 暂停录制」：关掉之后点一下就能恢复自动录制
  li.querySelector('[data-role="auto-ctl"]').addEventListener("click", (event) => {
    event.stopPropagation();
    const current = state.users.find((item) => item.userid === user.userid);
    setAuto(user.userid, !!(current && current.disable));
  });
  li.addEventListener("click", () => selectUser(user.userid));
  li.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    selectUser(user.userid);
    openRowMenu(event.clientX, event.clientY);
  });
  return li;
}

function fillRow(li, user) {
  // 头像只在地址变了才重新赋 src：每秒重建 <img> 会让图反复中断，有的头像就再也不出来
  const avatar = li.querySelector(".avatar");
  const wanted = user.avatar || "";
  if (avatar.dataset.url !== wanted) {
    avatar.dataset.url = wanted;
    avatar.style.visibility = "";
    avatar.referrerPolicy = "no-referrer";
    avatar.src = wanted;
  }

  const nick = li.querySelector(".nick");
  nick.textContent = user.nickname || "未命名主播";
  nick.title = user.title ? `正在播：${user.title}` : (user.nickname || "");

  const sub = li.querySelector(".sub");
  sub.textContent = "";
  const status = document.createElement("span");
  status.className = "status" + (user.recording ? " rec" : user.live ? " live" : "");
  status.textContent = user.status;
  sub.appendChild(status);
  const platform = document.createElement("span");
  const badge = { kuaishou: ["ks", "快手"], xiaohongshu: ["xhs", "小红书"] }[user.type];
  platform.className = "platform" + (badge ? " " + badge[0] : "");
  platform.textContent = badge ? badge[1] : "抖音";
  platform.title = platform.textContent;
  platform.dataset.platform = user.type || "douyin";
  sub.appendChild(platform);
  if (user.recording) {
    if (!state.config.hideTime) sub.appendChild(tag("时长 " + user.duration));
    if (!state.config.hideFileSize) sub.appendChild(tag("大小 " + user.size));
  }
  if (state.config.showUserCount && user.user_count) sub.appendChild(tag(fmtCount(user.user_count)));
  if (user.error) sub.appendChild(tag(user.error));

  li.classList.toggle("selected", state.selected === user.userid);
  const ctl = li.querySelector(".auto-ctl");
  const auto = li.querySelector('[data-role="auto"]');
  auto.classList.toggle("on", !user.disable);
  ctl.classList.toggle("on", !user.disable);
  ctl.classList.toggle("start", !!user.disable);
  ctl.querySelector(".auto-label").textContent = user.disable ? "开始录制" : "自动录制";
  ctl.title = user.disable
    ? "点一下恢复：开播后自动录制"
    : "点一下暂停：不再自动录制这位主播";
}

function renderUsers() {
  const list = $("userList");
  $("emptyHint").classList.toggle("hidden", state.users.length > 0);
  $("tabCount").textContent = state.users.length;
  state.users.forEach((user) => {
    const li = ensureRow(list, user);
    fillRow(li, user);
    list.appendChild(li);          // 已存在的行只是移动位置，不会重建
  });
  Array.from(list.children).forEach((li) => {
    if (!state.users.some((user) => user.userid === li.dataset.uid)) li.remove();
  });
}

function tag(text) {
  const span = document.createElement("span");
  span.className = "tag";
  span.textContent = text;
  return span;
}

function dangerPill(text, handler) {
  const button = document.createElement("button");
  button.className = "pill-danger";
  button.textContent = text;
  button.onclick = (event) => {
    event.stopPropagation();
    handler();
  };
  return button;
}

/* ---------------- 数据刷新 ---------------- */

let refreshing = false;
let lastRefreshError = 0;

async function refresh() {
  if (refreshing || !window.pywebview || !window.pywebview.api) return;   // 上一轮没回来就不再排新的
  refreshing = true;
  try {
    const data = await api().get_state();
    state.config = data.config;
    state.users = data.users;
    state.running = data.running;
    $("btnRun").textContent = data.running ? "停止检测" : "检测录制";
    $("btnQuality").textContent = data.config.definition || "蓝光";
    const format = FORMATS.find((item) => item.value === data.config.outFormat) || FORMATS[0];
    $("btnFormat").textContent = format.label;
    $("versionText").textContent = "当前版本: " + data.version;
    $("settingsVersion").textContent = "Version: " + data.version;
    renderUsers();
    if (!$("logsModal").classList.contains("hidden")) {
      $("logBox").textContent = (data.logs || []).join("\n");
      $("logBox").scrollTop = $("logBox").scrollHeight;
    }
  } catch (err) {
    // 出错要说出来，否则界面会一直空着像卡死一样
    console.error("refresh failed", err);
    if (Date.now() - lastRefreshError > 10000) {
      lastRefreshError = Date.now();
      toast("读取状态失败：" + (err && err.message ? err.message : err), 5000);
    }
  } finally {
    refreshing = false;
  }
}

function selectUser(userid) {
  state.selected = userid;
  $("rowActions").classList.toggle("hidden", !userid);
  renderUsers();
}

async function setAuto(userid, enabled) {
  await api().set_auto(userid, enabled);
  await refresh();
}

async function removeUser(userid) {
  const user = state.users.find((item) => item.userid === userid);
  const name = (user && (user.nickname || user.unique_id)) || "这位主播";
  const ok = await askConfirm(`把「${name}」从主播列表里移除？`, "移除");
  if (!ok) return;
  await api().remove_user(userid);
  state.selected = null;
  $("rowActions").classList.add("hidden");
  await refresh();
}

/* ---------------- 菜单 ---------------- */

function hideMenus() {
  ["mainMenu", "rowMenu", "qualityMenu", "formatMenu"].forEach((id) =>
    $(id).classList.add("hidden"));
}

function openMenu(id, x, y) {
  hideMenus();
  const menu = $(id);
  menu.classList.remove("hidden");
  const rect = menu.getBoundingClientRect();
  menu.style.left = Math.min(x, window.innerWidth - rect.width - 8) + "px";
  menu.style.top = Math.min(y, window.innerHeight - rect.height - 8) + "px";
}

function openRowMenu(x, y) {
  // 这一项在「停止录制 / 开始录制」之间切换，没在播也没在录时置灰
  const user = state.users.find((item) => item.userid === state.selected);
  const button = $("rowMenu").querySelector('[data-row="stoprec"]');
  const recording = !!(user && user.recording);
  button.textContent = recording ? "停止录制" : "开始录制";
  button.disabled = !user || (!recording && !user.live);
  openMenu("rowMenu", x, y);
}

function buildChoiceMenu(id, options, current, onPick) {
  const menu = $(id);
  menu.innerHTML = "";
  options.forEach((option) => {
    const button = document.createElement("button");
    button.textContent = option.label + (option.value === current ? "  ✓" : "");
    button.onclick = () => onPick(option.value);
    menu.appendChild(button);
  });
}

document.addEventListener("click", (event) => {
  if (!event.target.closest(".menu") && !event.target.closest("#btnMenu")
      && !event.target.closest("#btnQuality") && !event.target.closest("#btnFormat")) {
    hideMenus();
  }
});

$("btnMenu").onclick = (event) => {
  const rect = event.target.getBoundingClientRect();
  openMenu("mainMenu", rect.right - 150, rect.bottom + 4);
};

$("btnQuality").onclick = (event) => {
  const rect = event.target.getBoundingClientRect();
  buildChoiceMenu("qualityMenu",
    QUALITIES.map((q) => ({ value: q, label: q })),
    state.config.definition,
    async (value) => {
      hideMenus();
      await api().apply_settings({ definition: value });
      toast("清晰度：" + value);
      refresh();
    });
  openMenu("qualityMenu", rect.left - 40, rect.top - 8 * QUALITIES.length - 12);
};

$("btnFormat").onclick = (event) => {
  const rect = event.target.getBoundingClientRect();
  buildChoiceMenu("formatMenu", FORMATS.map((f) => ({ value: f.value, label: f.label })),
    state.config.outFormat,
    async (value) => {
      hideMenus();
      await api().apply_settings({ outFormat: value });
      toast("输出格式：" + value.toUpperCase());
      refresh();
    });
  openMenu("formatMenu", rect.left - 40, rect.top - 8 * FORMATS.length - 12);
};

$("mainMenu").onclick = async (event) => {
  const key = event.target.dataset.menu;
  if (!key) return;
  hideMenus();
  if (key === "settings") openSettings();
  if (key === "transcode") openTranscode();
  if (key === "savedir") await api().open_save_dir();
  if (key === "readme") $("readmeModal").classList.remove("hidden");
  if (key === "logs") {
    $("logsModal").classList.remove("hidden");
    const data = await api().get_state();
    $("logBox").textContent = (data.logs || []).join("\n");
  }
};

$("rowMenu").onclick = async (event) => {
  const key = event.target.dataset.row;
  const userid = state.selected;
  if (!key || !userid) return;
  hideMenus();
  if (key === "check") {
    const status = await api().check_now(userid);
    toast("检测结果：" + status);
  }
  if (key === "audio" || key === "video" || key === "vr") {
    await api().set_mode(userid, key);
    toast({ audio: "仅录制音频", video: "默认录制视频", vr: "录制VR直播" }[key]);
  }
  if (key === "home") await api().open_home(userid);
  if (key === "dir") await api().open_dir(userid);
  if (key === "remove") await removeUser(userid);
  if (key === "stoprec") {
    const user = state.users.find((item) => item.userid === userid);
    const message = user && user.recording
      ? await api().stop_recording(userid)
      : await api().resume_recording(userid);
    toast(message);
  }
  if (key === "copy") {
    const result = await api().copy_stream(userid);
    toast(result.url ? "直播源已复制到剪贴板" : (result.error || "没有拿到直播源"));
  }
  refresh();
};

$("rowActions").onclick = async (event) => {
  const key = event.target.dataset.act;
  if (!key || !state.selected) return;
  if (key === "home") await api().open_home(state.selected);
  if (key === "dir") await api().open_dir(state.selected);
  if (key === "copy") {
    const result = await api().copy_stream(state.selected);
    toast(result.url ? "直播源已复制到剪贴板" : (result.error || "没有拿到直播源"));
  }
};

/* ---------------- 检测录制 ---------------- */

$("btnRun").onclick = async () => {
  const result = await api().toggle_run();
  if (result.error) toast(result.error);
  refresh();
};

/* ---------------- 添加主播 ---------------- */

$("btnAdd").onclick = async () => {
  const text = $("addText").value;
  if (!text.trim()) {
    toast("先粘贴主播主页链接");
    return;
  }
  $("addResult").textContent = "正在解析…";
  const result = await api().add_users(text);
  const lines = [];
  if (result.added.length) lines.push("已添加：" + result.added.join("、"));
  if ((result.updated || []).length) lines.push("已补上分享链接：" + result.updated.join("、"));
  result.failed.forEach((item) => lines.push(`失败：${item.line}（${item.reason}）`));
  $("addResult").textContent = lines.join("\n") || "没有可添加的链接";
  if (result.added.length || (result.updated || []).length) $("addText").value = "";
  refresh();
};

/* ---------------- 设置 ---------------- */

function readSettings() {
  return {
    intervalTime: Number($("setInterval").value) || 15,
    save: $("setSave").value,
    streamType: $("setStream").value,
    fileTime: Number($("setFileTime").value),
    fileSize: Number($("setFileSize").value),
    recTime: Number($("setRecTime").value),
    recFileSize: Number($("setRecSize").value),
    checkPK: $("setCheckPK").classList.contains("on"),
    hideTime: $("setHideTime").classList.contains("on"),
    hideFileSize: $("setHideSize").classList.contains("on"),
    showUserCount: $("setShowCount").classList.contains("on"),
    mp3play: $("setMp3").classList.contains("on"),
    closeToTray: $("setCloseTray").classList.contains("on"),
  };
}

function openSettings() {
  const cfg = state.config;
  showSettingsPane("basic");
  $("setInterval").value = cfg.intervalTime;
  $("setSave").value = cfg.save;
  $("setStream").value = cfg.streamType;
  $("setFileTime").value = cfg.fileTime;
  $("setFileSize").value = cfg.fileSize;
  $("setRecTime").value = cfg.recTime;
  $("setRecSize").value = cfg.recFileSize;
  setSwitch("setCheckPK", cfg.checkPK);
  setSwitch("setHideTime", cfg.hideTime);
  setSwitch("setHideSize", cfg.hideFileSize);
  setSwitch("setShowCount", cfg.showUserCount);
  setSwitch("setMp3", cfg.mp3play);
  setSwitch("setCloseTray", cfg.closeToTray);
  updateSliderLabels();
  $("settingsModal").classList.remove("hidden");
}

function showSettingsPane(name) {
  document.querySelectorAll(".side-item").forEach((item) =>
    item.classList.toggle("active", item.dataset.pane === name));
  $("paneBasic").classList.toggle("hidden", name !== "basic");
  $("paneAbout").classList.toggle("hidden", name !== "about");
}

document.querySelectorAll(".side-item").forEach((item) => {
  item.onclick = () => showSettingsPane(item.dataset.pane);
});

function setSwitch(id, value) {
  $(id).classList.toggle("on", !!value);
}

function updateSliderLabels() {
  const fileTime = Number($("setFileTime").value);
  const fileSize = Number($("setFileSize").value);
  const recTime = Number($("setRecTime").value);
  const recSize = Number($("setRecSize").value);
  $("valFileTime").textContent = fileTime ? fileTime + "分钟" : "不限制";
  $("valFileSize").textContent = fileSize ? fileSize + "MB" : "不限制";
  $("valRecTime").textContent = recTime ? recTime + "分钟" : "不限制";
  $("valRecSize").textContent = recSize ? recSize + "MB" : "不分段";
}

["setFileTime", "setFileSize", "setRecTime", "setRecSize"].forEach((id) => {
  $(id).oninput = updateSliderLabels;
});

["setCheckPK", "setHideTime", "setHideSize", "setShowCount", "setMp3", "setCloseTray"].forEach((id) => {
  $(id).onclick = () => $(id).classList.toggle("on");
});

$("btnPickFolder").onclick = async () => {
  const folder = await api().pick_folder($("setSave").value);
  if (folder) $("setSave").value = folder;
};

$("btnApplySettings").onclick = async () => {
  await api().apply_settings(readSettings());
  $("settingsModal").classList.add("hidden");
  toast("设置已应用");
  refresh();
};

document.querySelectorAll("[data-close]").forEach((button) => {
  button.onclick = () => $(button.dataset.close).classList.add("hidden");
});

/* ---------------- 视频转码 ---------------- */

async function loadFiles() {
  const files = await api().list_recordings();
  const list = $("fileList");
  list.innerHTML = "";
  state.transcodeFile = null;
  $("btnDoTranscode").disabled = true;
  if (!files.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "保存目录里还没有 ts / flv 录像";
    list.appendChild(li);
    return;
  }
  files.forEach((file) => {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "fname";
    name.textContent = file.name;
    const meta = document.createElement("span");
    meta.className = "fmeta";
    meta.textContent = file.folder + " · " + fmtSize(file.size);
    li.appendChild(name);
    li.appendChild(meta);
    li.onclick = () => {
      list.querySelectorAll("li").forEach((item) => item.classList.remove("selected"));
      li.classList.add("selected");
      state.transcodeFile = file.path;
      $("btnDoTranscode").disabled = false;
      $("transcodeHint").textContent = "已选择：" + file.name;
    };
    list.appendChild(li);
  });
}

function openTranscode() {
  $("transcodeModal").classList.remove("hidden");
  loadFiles();
}

$("btnRefreshFiles").onclick = loadFiles;

$("btnDoTranscode").onclick = async () => {
  if (!state.transcodeFile) return;
  $("btnDoTranscode").disabled = true;
  $("transcodeHint").textContent = "转码中…";
  const result = await api().transcode(state.transcodeFile);
  $("transcodeHint").textContent = result.ok ? "完成：" + result.target : "失败：" + result.error;
  $("btnDoTranscode").disabled = false;
};

$("btnTranscode").onclick = openTranscode;

/* ---------------- 启动 ---------------- */

let booted = false;

function boot() {
  // pywebviewready 有极小概率早于脚本注册监听，没等到就自己轮询，别让列表一直空着
  if (booted || !window.pywebview || !window.pywebview.api) return false;
  booted = true;
  refresh();
  state.timer = setInterval(refresh, 1000);
  return true;
}

window.addEventListener("pywebviewready", boot);

document.addEventListener("DOMContentLoaded", () => {
  if (boot()) return;
  const wait = setInterval(() => {
    if (boot()) clearInterval(wait);
  }, 250);
  setTimeout(() => clearInterval(wait), 30000);
});
