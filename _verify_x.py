"""验证 N100 上的 X 登录态是否真的有效。

只看状态码，不打印任何 cookie 值。
auth_token 无效时 X 会返回 401 / "Could not authenticate you"。
"""
import json
import urllib.error
import urllib.request

import server

BEARER = ('AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs'
          '%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA')


def load_kv():
    kv = {}
    p = server.TW_COOKIE_FILE
    if not p.exists():
        return kv
    for ln in p.read_text(encoding='utf-8', errors='replace').splitlines():
        if not ln.strip() or ln.startswith('#'):
            continue
        parts = ln.split('\t')
        if len(parts) >= 7:
            kv[parts[5]] = parts[6]
    return kv


def main():
    kv = load_kv()
    print('cookie 条数:', len(kv), '| 有 auth_token:', 'auth_token' in kv,
          '| 有 ct0:', 'ct0' in kv)
    if 'auth_token' not in kv or 'ct0' not in kv:
        print('!! 关键票据缺失')
        return 1

    s = server.load_settings()
    px = s.get('proxy') or ''
    op = urllib.request.build_opener(urllib.request.ProxyHandler(
        {'http': px, 'https': px} if px else {}))

    cookie = '; '.join(f'{k}={v}' for k, v in kv.items()
                       if k in ('auth_token', 'ct0', 'twid', 'guest_id', 'kdt'))
    # 1.1 的 account/settings、verify_credentials 都已被 X 下线（一律 404 code 34），
    # 别拿它们判断 cookie 有效性。dm/inbox_initial_state 是需要登录的接口：
    # 200 = 登录态有效，401/403 = 失效。
    url = 'https://api.x.com/1.1/dm/inbox_initial_state.json'
    req = urllib.request.Request(url, headers={
        'authorization': 'Bearer ' + BEARER,
        'x-csrf-token': kv['ct0'],
        'cookie': cookie,
        'user-agent': server.UA,
        'x-twitter-active-user': 'yes',
        'x-twitter-auth-type': 'OAuth2Session',
        'x-twitter-client-language': 'zh-cn',
    })
    try:
        with op.open(req, timeout=30) as r:
            body = json.loads(r.read().decode('utf-8', 'replace'))
        print('HTTP', r.status, '-> 登录态有效')
        inbox = body.get('inbox_initial_state') or {}
        print('  user_id:', inbox.get('user_id') or body.get('user_id') or '(未返回)')
        return 0
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'replace')[:300]
        except Exception:  # noqa: BLE001
            pass
        print('HTTP', e.code, '-> 登录态无效或已过期')
        print('  返回:', detail)
        return 2
    except Exception as e:  # noqa: BLE001
        print('请求失败（可能只是代理问题，不代表 cookie 无效）:', type(e).__name__, str(e)[:160])
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
