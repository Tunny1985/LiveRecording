"""完整性校验：打包时把 exe 和 _internal 里每个文件算一遍 SHA256 存进清单，
启动时再算一遍对不上就不让开。

清单本身也走 vault 加密 + HMAC，所以改清单同样会被发现。清单放在 _internal 里，
和 exe 分开存，这样不存在「把清单塞进 exe 又去校验 exe」的循环依赖。
"""
import hashlib
import json
from pathlib import Path

from core import vault

MANIFEST_NAME = "manifest.bin"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entry(path):
    return {"size": path.stat().st_size, "sha256": sha256_file(path)}


def build_manifest(root, exe_path):
    root = Path(root)
    files = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        files[path.relative_to(root).as_posix()] = _entry(path)
    return {"exe": _entry(Path(exe_path)), "files": files}


def write_manifest(root, exe_path):
    root = Path(root)
    manifest = build_manifest(root, exe_path)
    target = root / MANIFEST_NAME
    target.write_bytes(vault.seal(json.dumps(manifest).encode("utf-8")))
    return manifest, target


def verify(root, exe_path):
    """返回 (是否通过, 说不通过的原因)。"""
    root = Path(root)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.exists():
        return False, "缺少完整性清单 manifest.bin"
    try:
        manifest = json.loads(vault.unseal(manifest_path.read_bytes()).decode("utf-8"))
    except (vault.VaultError, ValueError, UnicodeDecodeError):
        return False, "完整性清单被修改过"

    exe_path = Path(exe_path)
    want = manifest.get("exe") or {}
    if not exe_path.exists():
        return False, "找不到程序主文件"
    if exe_path.stat().st_size != want.get("size") or sha256_file(exe_path) != want.get("sha256"):
        return False, "程序主文件被修改过"

    expected = manifest.get("files") or {}
    for name, entry in sorted(expected.items()):
        path = root / name
        if not path.is_file():
            return False, f"缺少文件 {name}"
        if path.stat().st_size != entry.get("size") or sha256_file(path) != entry.get("sha256"):
            return False, f"文件被修改过：{name}"

    extra = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        if path.relative_to(root).as_posix() not in expected:
            extra.append(path.relative_to(root).as_posix())
    if extra:
        return False, "多了没有的文件：" + "、".join(extra[:5])
    return True, ""
