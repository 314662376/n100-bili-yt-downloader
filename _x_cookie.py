"""从浏览器 cookie 库（Edge/Chrome 通用）导出指定站点的登录态。

为什么要单独写：浏览器运行时会对 Network\\Cookies 用独占共享模式打开，
任何进程（包括 robocopy /B）都读不了，必须先用卷影副本（VSS）把文件捞出来。
捞出来之后就是标准的 Chromium 加密：
  Local State → os_crypt.encrypted_key（base64 后去掉 "DPAPI" 5 字节）
             → CryptUnprotectData 得到 32 字节 AES-256 主密钥
  每条 encrypted_value 前缀 v10/v11 → AES-256-GCM(nonce=3:15, ct=15:)

用法：
  python _x_cookie.py <cookies.db> <Local State> <输出.txt> [域名过滤]
  例：python _x_cookie.py edge_default.db "Local State" x_cookies.txt x.com,twitter.com

安全：只打印 cookie 名字和条数，不打印任何值。
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import win32crypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CHROME_EPOCH_OFFSET = 11644473600  # 1601-01-01 → 1970-01-01 的秒数


def master_key(local_state: Path) -> bytes:
    d = json.loads(local_state.read_text(encoding='utf-8'))
    oc = d.get('os_crypt') or {}
    if oc.get('app_bound_encrypted_key'):
        # Edge 127+ 会额外放一个 App-Bound 密钥；只有当 cookie 前缀是 v20 时才用得上，
        # 这里只是提示，不影响 v10 的解密
        print('[提示] Local State 里有 app_bound_encrypted_key，但 v10 cookie 仍走 DPAPI')
    enc = oc.get('encrypted_key')
    if not enc:
        raise SystemExit('Local State 里没有 os_crypt.encrypted_key')
    raw = base64.b64decode(enc)
    if raw[:5] != b'DPAPI':
        raise SystemExit(f'encrypted_key 前缀异常: {raw[:5]!r}')
    return win32crypt.CryptUnprotectData(raw[5:], None, None, None, 0)[1]


def decrypt(key: bytes, host: str, blob: bytes) -> str:
    """解一条 encrypted_value。

    坑：Chromium 130 起在明文前面又加了 32 字节 SHA256(host_key)，用来把 cookie
    绑定到域名（防 Cookie 库被整个拷到别的站用）。不去掉这段，解出来的值前面会多
    32 个乱码字符，上传后认证必然失败。判断依据是这 32 字节正好等于域名哈希。
    """
    pre = blob[:3]
    if pre in (b'v10', b'v11'):
        try:
            plain = AESGCM(key).decrypt(blob[3:15], blob[15:], None)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f'AES-GCM 解密失败: {type(e).__name__}') from e
        if len(plain) > 32 and plain[:32] == hashlib.sha256(host.encode()).digest():
            plain = plain[32:]
        return plain.decode('utf-8', 'replace')
    if pre == b'v20':
        raise RuntimeError('v20（App-Bound Encryption），需要 IElevator COM，普通方式解不开')
    # 很老的版本：整个 blob 直接就是 DPAPI 密文
    plain = win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]
    if len(plain) > 32 and plain[:32] == hashlib.sha256(host.encode()).digest():
        plain = plain[32:]
    return plain.decode('utf-8', 'replace')


def chrome_ts(us: int) -> int:
    if not us:
        return 0
    return max(0, int(us / 1_000_000) - CHROME_EPOCH_OFFSET)


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 1
    db = Path(sys.argv[1])
    local_state = Path(sys.argv[2])
    out = Path(sys.argv[3])
    want = [w.strip().lower() for w in (sys.argv[4] if len(sys.argv) > 4 else '').split(',') if w.strip()]

    key = master_key(local_state)
    print(f'主密钥解出，长度 {len(key)} 字节')

    con = sqlite3.connect(f'file:{db.as_posix()}?mode=ro&immutable=1', uri=True)
    try:
        rows = con.execute(
            'select host_key, name, encrypted_value, path, is_secure, expires_utc '
            'from cookies').fetchall()
    finally:
        con.close()

    def hit(host: str) -> bool:
        h = host.lower().lstrip('.')
        return any(h == w or h.endswith('.' + w) for w in want) if want else True

    lines = []
    bad = []
    names = []
    for host, name, enc, path, secure, exp in rows:
        if not hit(host):
            continue
        try:
            val = decrypt(key, host, enc)
        except Exception as e:  # noqa: BLE001
            bad.append((host, name, str(e)))
            continue
        lines.append('\t'.join([
            host,
            'TRUE' if host.startswith('.') else 'FALSE',
            path or '/',
            'TRUE' if secure else 'FALSE',
            str(chrome_ts(exp)),
            name,
            val,
        ]))
        names.append(name)

    head = ('# Netscape HTTP Cookie File\n'
            '# 由本机浏览器登录态导出（N100 下载站）\n\n')
    out.write_text(head + '\n'.join(lines) + '\n', encoding='utf-8')
    try:
        out.chmod(0o600)
    except OSError:
        pass

    print(f'导出 {len(lines)} 条 -> {out}')
    print(f'cookie 名: {sorted(set(names))}')
    if bad:
        print(f'解不开 {len(bad)} 条:')
        for h, n, e in bad[:6]:
            print(f'   {h} {n}: {e}')
    must = {'auth_token', 'ct0'}
    miss = must - set(names)
    print('关键票据:', '齐全' if not miss else f'缺少 {sorted(miss)}')
    return 0 if not miss else 3


if __name__ == '__main__':
    sys.exit(main())
