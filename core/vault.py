"""界面资源加密。

界面那几份文件（html / css / js / 图标）打包时不放在磁盘上明文摆着，而是加密成
一个 ui.bin，程序启动时在临时目录里解出来给 WebView2 用。这样想改名、换图标、
改文案的人就没法直接打开文件改一改再拿去卖了。

加密用的是标准库：SHA256 计数器当密钥流，HMAC-SHA256 做校验（密钥流本身不省，
但挡住随手改文件足够了；而且一旦改了，HMAC 对不上，程序直接不启动）。
"""
import hashlib
import hmac
import os
import shutil
import struct
import tempfile
from pathlib import Path

MAGIC = b"FMVAULT1"
CONTAINER_MAGIC = b"FMF1"
NONCE_LEN = 16
TAG_LEN = 32
# 打包脚本和程序里的这份必须一致；换一份等于重新打包
KEY = b"fengmang-live-screen-2026-10-11-vault-key"


class VaultError(Exception):
    """资源坏了或者被人改过。"""


def _keystream(nonce, size):
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(KEY + nonce + struct.pack(">Q", counter)).digest()
        counter += 1
    return bytes(out[:size])


def seal(payload):
    nonce = os.urandom(NONCE_LEN)
    body = bytes(a ^ b for a, b in zip(payload, _keystream(nonce, len(payload))))
    tag = hmac.new(KEY, nonce + body, hashlib.sha256).digest()
    return MAGIC + nonce + body + tag


def unseal(blob):
    if len(blob) < len(MAGIC) + NONCE_LEN + TAG_LEN or not blob.startswith(MAGIC):
        raise VaultError("资源文件格式不对")
    start = len(MAGIC)
    nonce = blob[start:start + NONCE_LEN]
    body = blob[start + NONCE_LEN:-TAG_LEN]
    tag = blob[-TAG_LEN:]
    want = hmac.new(KEY, nonce + body, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, want):
        raise VaultError("资源文件被修改过")
    return bytes(a ^ b for a, b in zip(body, _keystream(nonce, len(body))))


def pack_files(files):
    """{"index.html": b"...", ...} -> 一个自重描述的二进制块。"""
    out = bytearray(CONTAINER_MAGIC)
    out += struct.pack(">I", len(files))
    for name, data in sorted(files.items()):
        raw = name.encode("utf-8")
        out += struct.pack(">H", len(raw)) + raw
        out += struct.pack(">I", len(data)) + data
    return bytes(out)


def unpack_files(payload):
    if not payload.startswith(CONTAINER_MAGIC):
        raise VaultError("资源内容格式不对")
    pos = len(CONTAINER_MAGIC)
    count, = struct.unpack_from(">I", payload, pos)
    pos += 4
    files = {}
    for _ in range(count):
        name_len, = struct.unpack_from(">H", payload, pos)
        pos += 2
        name = payload[pos:pos + name_len].decode("utf-8")
        pos += name_len
        data_len, = struct.unpack_from(">I", payload, pos)
        pos += 4
        files[name] = payload[pos:pos + data_len]
        pos += data_len
    return files


def pack_dir(src, target=None):
    src = Path(src)
    files = {p.name: p.read_bytes() for p in sorted(src.iterdir()) if p.is_file()}
    if not files:
        raise VaultError(f"{src} 里没有可打包的文件")
    blob = seal(pack_files(files))
    if target:
        Path(target).write_bytes(blob)
    return blob


def unpack_ui(blob_path):
    """解开 ui.bin 到临时目录，返回那个目录。调用方退出时负责删掉。"""
    blob_path = Path(blob_path)
    if not blob_path.exists():
        raise VaultError(f"缺少资源文件 {blob_path.name}")
    files = unpack_files(unseal(blob_path.read_bytes()))
    target = Path(tempfile.mkdtemp(prefix="fm_ui_"))
    try:
        for name, data in files.items():
            # 名字里带路径分隔符的一律不要，免得解到临时目录外面去
            if name != Path(name).name:
                raise VaultError("资源里有非法文件名")
            (target / name).write_bytes(data)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target
