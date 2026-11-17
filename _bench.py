"""在 N100 上跑一次真实下载并采样资源占用。"""
import json
import os
import time
import urllib.request

API = 'http://127.0.0.1:8300'
URL = 'https://www.youtube.com/watch?v=PV_i3v1rMak'
QUALITY = 'best'


def _get(path):
    with urllib.request.urlopen(API + path, timeout=10) as r:
        return json.loads(r.read().decode('utf-8'))


def _post(path, obj):
    data = json.dumps(obj).encode('utf-8')
    req = urllib.request.Request(API + path, data=data,
                                 headers={'content-type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode('utf-8')


def cpu_times():
    with open('/proc/stat') as f:
        p = f.readline().split()[1:]
    v = [int(x) for x in p]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v), idle


def load1():
    with open('/proc/loadavg') as f:
        return f.read().split()[:3]


def meminfo():
    d = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, _, rest = line.partition(':')
            d[k] = int(rest.split()[0])
    return d


def temp():
    out = []
    base = '/sys/class/thermal'
    try:
        for z in sorted(os.listdir(base)):
            if not z.startswith('thermal_zone'):
                continue
            try:
                with open(f'{base}/{z}/temp') as f:
                    out.append(int(f.read().strip()) / 1000)
            except OSError:
                pass
    except OSError:
        pass
    return max(out) if out else None


def cg_cpu(container):
    """容器 cgroup 累计 CPU 微秒（v2 路径）。"""
    for base in ('/sys/fs/cgroup/system.slice', '/sys/fs/cgroup'):
        try:
            for name in os.listdir(base):
                if container in name:
                    with open(f'{base}/{name}/cpu.stat') as f:
                        for line in f:
                            if line.startswith('usage_usec'):
                                return int(line.split()[1])
        except OSError:
            continue
    return None


print('提交任务 ...', flush=True)
print(_post('/api/tasks', {'urls': URL, 'quality': QUALITY})[:120], flush=True)

prev_t, prev_i = cpu_times()
prev_cg = {c: cg_cpu(c) for c in ('ytdl-app', 'ytdl-pot')}
t0 = time.time()
peak = {'sys': 0.0, 'mem_app': 0, 'mem_pot': 0, 'temp': 0.0}
rows = []

while True:
    time.sleep(1.0)
    ct, ci = cpu_times()
    dt, di = ct - prev_t, ci - prev_i
    sys_pct = 100.0 * (dt - di) / dt if dt else 0.0
    prev_t, prev_i = ct, ci

    cg = {c: cg_cpu(c) for c in ('ytdl-app', 'ytdl-pot')}
    cg_pct = {}
    for c in cg:
        if cg[c] is not None and prev_cg.get(c) is not None:
            cg_pct[c] = 100.0 * (cg[c] - prev_cg[c]) / 1e6 / dt * 100 if dt else 0
        else:
            cg_pct[c] = 0.0
    prev_cg = cg

    st = _get('/api/state')
    task = st['tasks'][0] if st['tasks'] else {}
    m = meminfo()
    tp = temp() or 0.0
    mem_used_pct = 100.0 * (m['MemTotal'] - m['MemAvailable']) / m['MemTotal']
    s = task.get('state', '?')
    pct = task.get('pct') or 0
    spd = (task.get('speed') or 0) / 1048576

    peak['sys'] = max(peak['sys'], sys_pct)
    peak['temp'] = max(peak['temp'], tp)
    peak['mem_app'] = max(peak['mem_app'], cg_pct.get('ytdl-app', 0))

    print(f'{time.time()-t0:5.0f}s {s:12s} {pct:5.1f}% {spd:5.2f}MB/s | '
          f'系统CPU {sys_pct:5.1f}%  app {cg_pct.get("ytdl-app",0):5.1f}%  pot {cg_pct.get("ytdl-pot",0):4.1f}% | '
          f'内存用 {mem_used_pct:4.1f}% | load {" ".join(load1())} | {tp:.0f}°C', flush=True)
    rows.append((s, pct, sys_pct, spd, tp))
    if s in ('done', 'failed', 'canceled') or time.time() - t0 > 900:
        break

print('\n== 峰值 ==')
print(f'系统 CPU 峰值 {peak["sys"]:.1f}%  (4 核 → 约 {peak["sys"]*4/100:.2f} 核)')
print(f'app 容器 CPU 峰值 {peak["mem_app"]:.1f}%')
print(f'最高温度 {peak["temp"]:.0f}°C')
dls = [r for r in rows if r[0] == 'downloading']
if dls:
    avg = sum(r[2] for r in dls) / len(dls)
    print(f'下载阶段平均系统 CPU {avg:.1f}%（{len(dls)} 个采样点）')
print('最终状态:', rows[-1][0] if rows else '?')
