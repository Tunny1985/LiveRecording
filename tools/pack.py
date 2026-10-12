"""打包辅助（路径按本文件位置推算，项目挪到哪都能用）：

    python tools/pack.py ui        把 ui/ 加密成 ui.bin（PyInstaller 之前跑）
    python tools/pack.py manifest  给 exe + _internal 生成完整性清单（PyInstaller 之后跑）
"""
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
DIST = APP / "dist" / "锋芒直播录屏"
EXE_NAME = "锋芒直播录屏.exe"

sys.path.insert(0, str(APP))

from core import guard, vault  # noqa: E402


def pack_ui():
    blob = vault.pack_dir(APP / "ui", APP / "ui.bin")
    names = sorted(vault.unpack_files(vault.unseal(blob)))
    print("ui.bin:", len(blob), "bytes;里面装了:", names)


def pack_manifest():
    internal = DIST / "_internal"
    exe = DIST / EXE_NAME
    if not internal.is_dir() or not exe.exists():
        print("FAIL: 先构建 dist")
        return 1
    manifest, target = guard.write_manifest(internal, exe)
    print("manifest.bin:", target, target.stat().st_size, "bytes;",
          len(manifest["files"]), "个文件 + exe")
    return 0


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "ui"
    if what == "ui":
        pack_ui()
        return 0
    if what == "manifest":
        return pack_manifest()
    print("用法: pack.py ui|manifest")
    return 1


if __name__ == "__main__":
    sys.exit(main())
