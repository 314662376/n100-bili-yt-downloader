"""从 Edge 导出 bilibili 登录态。

坑：Edge 正在运行时，Cookies 库被独占锁住，sqlite 直接打开会报
"unable to open database file"（不是真的没 cookie）。必须先 robocopy /B
备份模式拷到镜像目录，再连镜像读。yt-dlp 的提取器还需要同级的 Local State
才能解密（DPAPI），所以镜像目录要把 Local State 一起还原。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

YTDL_DIR = Path(os.environ.get('YTDL_DIR') or (Path.home() / '.workbuddy' / 'binaries' / 'ytdlp'))
sys.path.insert(0, str(YTDL_DIR))
import ytdl  # noqa: E402

BROWSER = 'edge'
PYTHON = ytdl.PYTHON
LOGIN_KEYS = {'SESSDATA', 'bili_jct', 'DedeUserID', 'DedeUserID__ckMd5', 'sid'}
OUT = YTDL_DIR / 'bilibili_cookies.txt'
STAGE = Path(os.environ.get('TEMP', '.')) / 'bili_stage'


def _open(db: Path):
    return sqlite3.connect(f'file:{db}?mode=ro&immutable=1', uri=True)


def score(db: Path) -> tuple[int, float]:
    try:
        con = _open(db)
        try:
            rows = con.execute(
                "select distinct name from cookies where host_key like '%bilibili%'"
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return (0, 0.0)
    try:
        mt = db.stat().st_mtime
    except OSError:
        mt = 0.0
    return (len({r[0] for r in rows} & LOGIN_KEYS), mt)


def mirror(profile: Path, user_data: Path, dest: Path) -> Path | None:
    """把 profile 的 Network/Cookies + 顶层 Local State 拷成可读镜像"""
    shutil.rmtree(dest.parent, ignore_errors=True)
    (dest / 'Network').mkdir(parents=True, exist_ok=True)
    ls = user_data / 'Local State'
    if ls.exists():
        try:
            shutil.copy2(ls, dest.parent / 'Local State')
        except OSError:
            pass
    src = profile / 'Network' / 'Cookies'
    r = subprocess.run(
        ['robocopy', str(src.parent), str(dest / 'Network'), src.name,
         '/B', '/R:1', '/W:1', '/NJH', '/NJS', '/NP'],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        env={**os.environ, 'MSYS_NO_PATHCONV': '1'})
    dst = dest / 'Network' / 'Cookies'
    return dst if dst.exists() and r.returncode < 8 else None


def extract(profile_dir: Path, out: Path) -> tuple[bool, str]:
    code = (
        'import sys\n'
        'from http.cookiejar import MozillaCookieJar\n'
        'from yt_dlp.cookies import extract_cookies_from_browser\n'
        'prof, out = sys.argv[1], sys.argv[2]\n'
        'jar = extract_cookies_from_browser("%s", profile=prof)\n'
        'keep = [c for c in jar if "bilibili" in (c.domain or "")]\n'
        'j2 = MozillaCookieJar(out)\n'
        '[j2.set_cookie(c) for c in keep]\n'
        'j2.save(ignore_discard=True, ignore_expires=True)\n'
        'print(len(keep))\n'
    ) % BROWSER
    r = subprocess.run([str(PYTHON), '-c', code, str(profile_dir), str(out)],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        return False, (r.stderr or '')[-300:]
    return True, (r.stdout or '').strip()


def main() -> int:
    user_data = ytdl.browser_user_data_dir()
    if not user_data.is_dir():
        print(f'找不到 Edge User Data: {user_data}')
        return 1

    shutil.rmtree(STAGE, ignore_errors=True)
    best = None
    for prof in ytdl.list_profiles(user_data):
        db = prof / 'Network' / 'Cookies'
        if not db.exists():
            continue
        s, mt = score(db)
        src = prof
        if s == 0:
            # 直接打开失败或确实没有 → 试备份模式
            m = mirror(prof, user_data, STAGE / prof.name)
            if m is not None:
                s, mt = score(m)
                if s > 0:
                    src = STAGE / prof.name
        flag = '' if s == 0 else '  <== 有登录态'
        print(f'  {prof.name:12s} key={s} mtime={time.strftime("%m-%d %H:%M", time.localtime(mt))}{flag}')
        if s >= 2 and (best is None or (s, mt) > best[0]):
            best = ((s, mt), prof, src)

    if best is None:
        print('\n!! Edge 里没有 B 站登录态，请先在 Edge 打开 bilibili.com 登录')
        return 2

    (s, mt), prof, src = best
    print(f'\n选用 {prof.name}（{s} 个登录态 key）')

    out = OUT
    ok, msg = extract(src, out)
    if not (ok and out.exists() and out.stat().st_size > 200):
        print(f'!! 导出失败: {msg}')
        return 3

    txt = out.read_text(encoding='utf-8', errors='replace')
    names = [ln.split('\t')[5] for ln in txt.splitlines()
             if ln and not ln.startswith('#') and len(ln.split('\t')) >= 7]
    print(f'ok -> {out}（{len(names)} 条 bilibili cookie，导出 {msg} 条）')
    print('SESSDATA:', 'SESSDATA' in names, '| bili_jct:', 'bili_jct' in names)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
