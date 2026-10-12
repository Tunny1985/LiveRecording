"""重新打包 dist/锋芒直播录屏：跑 PyInstaller，再把 ffmpeg.exe + README.md 放到 exe 旁边。

用法：python tools/build_app.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
PY = APP / "venv" / "Scripts" / "python.exe"
SPEC = APP / "build" / "锋芒直播录屏.spec"
DIST = APP / "dist" / "锋芒直播录屏"
# 打包时从这里取 ffmpeg.exe（仓库里不放这个 148MB 的大件）；换机器要改这一行
FFMPEG_SRC = Path(r"F:\Media\ffmpeg\ffmpeg.exe")


def run_pack(what, env):
    proc = subprocess.run([str(PY), str(HERE / "pack.py"), what], cwd=str(HERE), env=env)
    if proc.returncode != 0:
        raise SystemExit(f"pack {what} 失败")


def kill_running():
    subprocess.run(["taskkill", "/IM", "锋芒直播录屏.exe", "/F"], capture_output=True)
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
         "Where-Object { $_.CommandLine -like '*\\app.py*' -and $_.ProcessId -ne $PID } | "
         "ForEach-Object { $_.ProcessId }"],
        capture_output=True, text=True)
    for pid in out.stdout.split():
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)


def main():
    if not PY.exists():
        print("找不到", PY, "——先建虚拟环境：python -m venv venv，再装 requirements.txt")
        return 1
    kill_running()
    env = dict(os.environ, PYTHONUTF8="1")
    run_pack("ui", env)        # 先把 ui/ 加密成 ui.bin，spec 打包的就是它
    # 用 python -m PyInstaller，不碰 venv\Scripts\pyinstaller.exe：
    # 那个 exe 是在建虚拟环境时生成的，里面写死了当时的绝对路径，venv 一搬家就用不了
    cmd = [str(PY), "-m", "PyInstaller", "--noconfirm", "--distpath", str(APP / "dist"),
           "--workpath", str(APP / "build"), str(SPEC)]
    print("$", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(APP), env=env)
    if proc.returncode != 0:
        print("build FAILED", proc.returncode)
        return proc.returncode
    if not DIST.is_dir():
        print("missing", DIST)
        return 1
    if FFMPEG_SRC.exists():
        shutil.copy2(FFMPEG_SRC, DIST / "ffmpeg.exe")
    else:
        print("警告：没找到", FFMPEG_SRC, "，dist 里的 ffmpeg.exe 原样保留（录制要靠它）")
    shutil.copy2(APP / "README.md", DIST / "README.md")
    run_pack("manifest", env)  # 再按最终产物生成完整性清单
    print("dist:", sorted(p.name for p in DIST.iterdir()))
    print("exe size:", (DIST / "锋芒直播录屏.exe").stat().st_size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
