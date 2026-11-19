"""定位并镜像浏览器里存着指定站点登录态的 cookie 库。

为什么需要这一步：Edge / Chrome 运行时会对 `Network\\Cookies` 用**独占共享模式**打开，
任何其它进程（包括 robocopy /B 备份模式）都读不了，sqlite 会报
"unable to open database file"。这时只能走卷影副本（VSS）：
卷快照是文件系统层的复制，完全绕过共享锁。

用法：
  python _mirror_ck.py                 # 扫全部 profile，列出 X 登录态在哪个库
  python _mirror_ck.py --mirror        # 顺便把被占用的库镜像到 %TEMP%\\ck_stage

输出最后一行是「可用的库路径」，直接喂给 _x_cookie.py。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
STAGE = Path(os.environ.get('TEMP', '.')) / 'ck_stage'
PWSH = Path(r'C:\Program Files\PowerShell\7\pwsh.exe')
VSS_ID_FILE = Path(os.environ.get('TEMP', '.')) / 'ck_vss_id.txt'

ROOTS = [
    ('edge', HOME / 'AppData/Local/Microsoft/Edge/User Data'),
    ('chrome', HOME / 'AppData/Local/Google/Chrome/User Data'),
]
# 站点的登录票据；命中任意一个才算"登录态"，光有 guest_id 不算
SITES = {
    'x': ('auth_token', 'ct0', 'twid'),
    'bilibili': ('SESSDATA', 'bili_jct', 'DedeUserID'),
    'youtube': ('SID', 'HSID', 'SSID', 'APISID', 'SAPISID', '__Secure-1PSID'),
}
# 只按精确域名后缀匹配，别用 %x.com 这种——会把 yandex.com / xnxx.com 一起捞进来
SITE_HOSTS = {
    'x': ('x.com', 'twitter.com'),
    'bilibili': ('bilibili.com',),
    'youtube': ('youtube.com',),
}

VSS_CREATE = r"""$ErrorActionPreference = 'Stop'
Start-Service VSS -ErrorAction SilentlyContinue
$r = Invoke-CimMethod -ClassName Win32_ShadowCopy -MethodName Create -Arguments @{ Volume = 'C:\'; Context = 'ClientAccessible' }
if ($r.ReturnValue -ne 0) { Write-Error "shadow create failed: $($r.ReturnValue)"; exit 1 }
$sc = Get-CimInstance Win32_ShadowCopy | Where-Object { $_.ID -eq $r.ShadowID }
# 追加而不是覆盖：一次运行里可能给多个 profile 建快照，覆盖会漏删
Add-Content -LiteralPath $env:TEMP\ck_vss_id.txt -Value $sc.ID -Encoding ASCII
Write-Output $sc.DeviceObject
"""

VSS_DELETE = r"""$ErrorActionPreference = 'SilentlyContinue'
$f = Join-Path $env:TEMP 'ck_vss_id.txt'
if (Test-Path -LiteralPath $f) {
  $ids = Get-Content -LiteralPath $f | Where-Object { $_.Trim() }
  $all = Get-CimInstance Win32_ShadowCopy
  foreach ($id in $ids) {
    $all | Where-Object { $_.ID -eq $id.Trim() } | Remove-CimInstance
  }
  Remove-Item -LiteralPath $f -Force
}
Stop-Service VSS
"""


def _pwsh(script: str) -> tuple[int, str]:
    """按本机约定：先落 .ps1 再 -File，避免 -Command 被引号拆坏"""
    p = Path(os.environ.get('TEMP', '.')) / '_ck_vss.ps1'
    # PowerShell 5 能读 UTF-8 BOM，7 无所谓，统一带 BOM 最稳
    p.write_text(script, encoding='utf-8-sig')
    exe = str(PWSH) if PWSH.exists() else 'pwsh'
    r = subprocess.run([exe, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(p)],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    return r.returncode, (r.stdout or '').strip() + (r.stderr or '').strip()


def targets():
    out = []
    for browser, root in ROOTS:
        if not root.is_dir():
            continue
        for db in sorted(root.glob('*/Network/Cookies')):
            if db.is_file():
                out.append((browser, db.parent.parent.name, db.parent.parent))
    return out


def probe(db: Path, site: str):
    hosts = SITE_HOSTS[site]
    con = sqlite3.connect(f'file:{db.as_posix()}?mode=ro&immutable=1', uri=True)
    try:
        total = con.execute('select count(*) from cookies').fetchone()[0]
        rows = con.execute('select host_key, name from cookies').fetchall()
    finally:
        con.close()
    kept = [(h, n) for h, n in rows
            if any(h.lstrip('.').lower() == x or h.lstrip('.').lower().endswith('.' + x)
                   for x in hosts)]
    return total, kept


def recover(browser: str, prof: str, dev: str, tag: str) -> tuple[Path | None, Path | None]:
    """从快照里把 cookie 库和 Local State 一起读出来。

    Local State 在 <User Data>/Local State（所有 profile 共用），解密主密钥在里面，
    少拿它 _x_cookie.py 就没法解。
    """
    dest = STAGE / tag
    dest.parent.mkdir(parents=True, exist_ok=True)
    sub = {'edge': r'Microsoft\Edge', 'chrome': r'Google\Chrome'}[browser]
    # 这里必须是字符串拼接，且只能用反斜杠：
    #   pathlib 会把 \\?\GLOBALROOT 前缀规范化坏；
    #   \\?\ 前缀的路径也不接受 "/"（会报 WinError 123 语法不正确）
    base = f'{dev}\\Users\\{Path.home().name}\\AppData\\Local\\{sub}\\User Data'
    src = f'{base}\\{prof}\\Network\\Cookies'
    try:
        shutil.copy2(src, dest)
    except OSError as e:
        print(f'    从快照读取失败: {e}')
        return None, None
    ls_dst = STAGE / f'Local State {browser}'
    try:
        shutil.copy2(f'{base}\\Local State', ls_dst)
    except OSError as e:
        print(f'    Local State 读取失败: {e}')
        ls_dst = None
    return dest, ls_dst


def main() -> int:
    # 独立模式：只清理上次留下的快照
    if '--cleanup' in sys.argv:
        rc, out = _pwsh(VSS_DELETE)
        print('快照已清理' if rc == 0 else f'清理失败: {out[:160]}')
        return 0

    site = 'x'
    for a in sys.argv[1:]:
        if a.startswith('--site='):
            site = a.split('=', 1)[1].strip().lower()
    if site not in SITES:
        print(f'未知站点 {site}，可选: {list(SITES)}')
        return 1
    want = set(SITES[site])

    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True, exist_ok=True)

    tg = targets()
    print(f'找到 {len(tg)} 个 profile，查 {site} 登录态\n')

    locked = []
    good = []          # (命中票据数, tag, 库路径, 是否来自快照)
    for browser, prof, src in tg:
        db = src / 'Network' / 'Cookies'
        try:
            total, kept = probe(db, site)
        except Exception:  # noqa: BLE001
            locked.append((browser, prof, src))
            print(f'  {browser}/{prof:14s} 被浏览器占用，稍后用快照读')
            continue
        hit = {n for _, n in kept} & want
        print(f'  {browser}/{prof:14s} 总 {total:5d}  {site} 相关 {len(kept):2d}  '
              f'票据 {sorted(hit) if hit else "无"}')
        if hit:
            good.append((len(hit), f'{browser}_{prof.replace(" ", "")}', db, False, None))

    dev = ''
    if locked:
        print('\n创建卷影副本（几秒钟）…')
        rc, out = _pwsh(VSS_CREATE)
        dev = out.splitlines()[-1].strip() if out else ''
        if rc != 0 or not dev.startswith('\\\\?\\'):
            print(f'  快照创建失败 rc={rc}: {out[:200]}')
            print('  兜底办法：完全退出浏览器后再跑一次（那时库不被占用，不需要快照）')
            dev = ''
        else:
            print(f'  快照: {dev}')
            for browser, prof, src in locked:
                tag = f'{browser}_{prof.replace(" ", "")}.db'
                p, _ls = recover(browser, prof, dev, tag)
                if p is None:
                    continue
                try:
                    total, kept = probe(p, site)
                except Exception as e:  # noqa: BLE001
                    print(f'  {browser}/{prof} 解析失败: {str(e)[:80]}')
                    continue
                hit = {n for _, n in kept} & want
                print(f'  {browser}/{prof:14s} 总 {total:5d}  {site} 相关 {len(kept):2d}  '
                      f'票据 {sorted(hit) if hit else "无"}')
                if hit:
                    good.append((len(hit), tag.replace('.db', ''), p, True, _ls))

    print('\n===== 结论 =====')
    if not good:
        print(f'没有任何 profile 存着 {site} 的登录票据')
        if dev:
            _pwsh(VSS_DELETE)
        return 2
    good.sort(reverse=True)
    # 正在使用的 profile 一定被浏览器锁着（走快照那条路），它的登录态才是最新的，
    # 优先选它；都没锁就按命中的票据数排
    good.sort(key=lambda x: (x[3], x[0]), reverse=True)
    n, tag, path, from_vss, ls_path = good[0]
    print(f'用 {tag}（命中 {n} 个票据）: {path}')
    if from_vss:
        print(f'下一步: python _x_cookie.py "{path}" "{ls_path}" out.txt x.com,twitter.com')
        print('注意：快照留着给你解密用，读完执行 python _mirror_ck.py --cleanup 删掉')
    else:
        print('这个 profile 没被占用，Local State 直接用浏览器目录里的那个即可')
    return 0


if __name__ == '__main__':
    sys.exit(main())
