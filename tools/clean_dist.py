"""清掉 dist/锋芒直播录屏 里的运行残留（配置、窗口位置、日志），让发布包干净。"""
from pathlib import Path

DIST = Path(__file__).resolve().parents[1] / "dist" / "锋芒直播录屏"
SCRATCH = ("config.json", "config.json.bak", "window.json",
           "锋芒直播录屏.log", "锋芒直播录屏.log.old")

if not DIST.is_dir():
    raise SystemExit(f"没有 {DIST}")

for name in SCRATCH:
    path = DIST / name
    if path.is_file():
        path.unlink()
        print("removed", name)

print("dist:", sorted(p.name for p in DIST.iterdir()))
