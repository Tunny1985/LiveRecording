r"""把源码项目打成压缩包，以后接着维护用。

和发布包（dist/锋芒直播录屏）不是一回事：这里只装写代码要用的东西，
不带 venv / dist / PyInstaller 的 build 中间产物 / 录像 / 配置 / 日志。

用法：python tools/make_src_zip.py [输出zip]
默认输出：<项目>\dist\锋芒直播录屏-源码.zip
"""
import sys
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else APP / "dist" / "锋芒直播录屏-源码.zip"
SKIP_DIRS = {"venv", "dist", "__pycache__", "录像", ".git", ".idea", "_scratch"}
SKIP_FILES = {"config.json", "config.json.bak", "window.json"}
SKIP_PREFIX = ("锋芒直播录屏.log",)
TOP = "锋芒直播录屏/"


def keep(rel):
    """build/ 只留 spec 文件，中间产物不要。"""
    parts = rel.parts
    if any(part in SKIP_DIRS for part in parts):
        return False
    if parts[0] == "build":
        return len(parts) == 2 and rel.suffix == ".spec"
    return not (rel.name in SKIP_FILES or rel.name.startswith(SKIP_PREFIX))


def main():
    if not (APP / "app.py").is_file():
        print("这里不像项目根目录:", APP)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    count = 0
    total = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(APP.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(APP)
            if not keep(rel):
                continue
            z.write(path, TOP + rel.as_posix())
            count += 1
            total += path.stat().st_size
    print("entries:", count, "uncompressed:", total)
    print("zip:", OUT, OUT.stat().st_size)
    with zipfile.ZipFile(OUT) as z:
        top = sorted({n.split("/", 1)[1].split("/")[0] for n in z.namelist() if "/" in n})
        print("顶层内容:", top)
        bad = [n for n in z.namelist()
               if "/venv/" in n or "/dist/" in n or "__pycache__" in n
               or "录像" in n or n.endswith((".log", ".bak"))]
        print("不该有的东西 (should be empty):", bad)
    return 0


if __name__ == "__main__":
    sys.exit(main())
