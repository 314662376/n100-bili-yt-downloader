"""N100 离线下载站 —— 服务端。

职责：
  - SQLite 持久化任务，进程重启后 running 任务自动回到队列（靠 .part 续传）
  - 线程池按并发数取任务，yt-dlp 进程内调用，进度/速度/ETA 400ms 落库
  - SSE 推给网页，Range 流式下载给浏览器，全量发完即标记"已取回"并定时删文件

坑位（与单机版 exe 同源，改动前先读）：
  1. player_client 默认 web_embedded，这是绕过该账号 SABR-only 实验的唯一可用值
  2. 格式排序必须用 res（短边），否则竖屏 1080x1920 会掉到 480p
  3. 机器房 IP 出口必须带 cookies + PO token，否则 YouTube 判机器人
  4. NODE_OPTIONS 必须清掉，否则 node 带 preload 起不来、n 签名解不出
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import threading
import time
import traceback
import urllib.request
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlsplit

os.environ.pop('NODE_OPTIONS', None)

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

# ────────────────────────────── 配置 ──────────────────────────────

DATA_DIR = Path(os.environ.get('DATA_DIR', '/data'))
DL_DIR = DATA_DIR / 'downloads'
COOKIE_FILE = DATA_DIR / 'cookies.txt'
SETTINGS_FILE = DATA_DIR / 'settings.json'
DB_FILE = DATA_DIR / 'tasks.db'

APP_PASSWORD = os.environ.get('APP_PASSWORD', '').strip()
DEFAULT_PROXY = os.environ.get('YTDL_PROXY', 'http://host.docker.internal:7890')
POT_URL = os.environ.get('POT_URL', 'http://bgutil:4416')
NODE_BIN = os.environ.get('NODE_BIN') or shutil.which('node') or '/usr/local/bin/node'
FFMPEG_BIN = shutil.which('ffmpeg') or '/usr/bin/ffmpeg'
FFMPEG_DIR = str(Path(FFMPEG_BIN).parent)

for d in (DATA_DIR, DL_DIR):
    d.mkdir(parents=True, exist_ok=True)

QUALITY = {
    'best': ('最高画质（≤1080p）', 1080, ''),
    '4k': ('4K（2160p）', 2160, '_4K'),
    '720': ('720p', 720, '_720p'),
    'audio': ('仅音频（MP3）', 0, '_audio'),
}

# B 站和 YouTube 是两套完全不同的东西，不能共用一套参数：
#  - B 站走直连（实测 30MB/s，走代理反而绕远），YouTube 必须走代理
#  - 登录态分两个文件，否则 YouTube 的 cookie 每 24h 刷新会把 B 站的冲掉
#  - B 站非大会员上限 1080P（qn=80），再往上是 1080P 高码率/4K，都要会员
BILI_COOKIE_FILE = DATA_DIR / 'bilibili_cookies.txt'
BILI_HOSTS = ('bilibili.com', 'bilibili.tv', 'b23.tv', 'acg.tv')
BILI_MAX_HEIGHT = 1080

# X（原 Twitter）：
#  - 必须走代理，容器直连 x.com 是 000，走 mihomo 才是 200
#  - 登录态单独一份，别和 YouTube 的 cookies.txt 混用：yt-dlp 会每 24h 刷新并重写
#    YouTube 的 cookie 文件，X 的登录态混在里面会被整个冲掉
#  - 公开推文匿名就能取（实测 x.com/jack/status/20 能正常解析），
#    但敏感内容/私密账号/被风控时需要登录态兜底，所以文件仍然保留
TW_COOKIE_FILE = DATA_DIR / 'twitter_cookies.txt'
TW_HOSTS = ('x.com', 'twitter.com', 'mobile.twitter.com', 't.co')
# 油猴脚本在页面里抓到的是 X 的媒体 CDN 直链（video.twimg.com/.../vid/avc1/1080x1920/xx.mp4）。
# 这类直链是公开的、不需要登录态，把它当 twitter 处理：不走高度过滤、直接取整份文件。
TW_CDN_HOSTS = ('video.twimg.com',)


def platform_of(url: str) -> str:
    """按域名判平台，不看路径——番剧/分P/短链的路径形态太多，域名最稳。"""
    try:
        host = (urlsplit(url).hostname or '').lower()
    except ValueError:
        return 'other'
    if any(host == h or host.endswith('.' + h) for h in BILI_HOSTS):
        return 'bilibili'
    if host == 'youtu.be' or host.endswith('youtube.com'):
        return 'youtube'
    if any(host == h or host.endswith('.' + h) for h in TW_HOSTS):
        return 'twitter'
    if any(host == h or host.endswith('.' + h) for h in TW_CDN_HOSTS):
        return 'twitter'
    return 'other'


def safe_name(s: str, limit: int = 120) -> str:
    """把前端给的"文件名提示"洗成能落盘的字符串。

    Windows→N100 的链路两头都可能挑刺：/ \\ : * ? " < > | 一律换掉，
    控制字符去掉，末尾的点和空格去掉（Windows 上非法），超长按字符截断。
    """
    s = re.sub(r'[\x00-\x1f\x7f]', ' ', str(s or ''))
    s = re.sub(r'[\\/:*?"<>|]', '-', s)
    s = re.sub(r'\s+', ' ', s).strip(' .')
    return s[:limit] or 'video'


def is_tw_cdn(url: str) -> bool:
    """是不是 X 媒体 CDN 直链（页面里抓来的那种，不需要任何接口探测）。"""
    try:
        host = (urlsplit(url).hostname or '').lower()
    except ValueError:
        return False
    return any(host == h or host.endswith('.' + h) for h in TW_CDN_HOSTS)


def plan(platform: str, quality: str) -> tuple[int, str, bool]:
    """把画质选项翻译成 (高度上限, 文件名后缀, 是否仅音频)。

    B 站没有 4K 这一档（大会员才有），选 4K 就按 1080P 下，不报错也不降级成怪东西。
    """
    _, height, suffix = QUALITY[quality]
    if quality == 'audio':
        return 0, '_audio', True
    if platform == 'bilibili':
        h = min(height or BILI_MAX_HEIGHT, BILI_MAX_HEIGHT)
        suffix = '' if quality in ('best', '4k') else f'_{h}p'
        return h, suffix, False
    if platform == 'twitter':
        # X 的可选档位本来就少，而且竖屏视频的 height 是「长边」（1080×1920 记成 height=1920），
        # 用 height<=N 过滤会把高清全滤掉——实测 1080P 竖屏被降到 480×852。
        # 所以不做高度过滤，直接取最高码率那一档（竖屏 1080P 也就 80MB 上下，不值得为省体积降画质）。
        return 0, '', False
    return height, suffix, False

DEFAULTS = {
    'concurrency': 1,
    'proxy': DEFAULT_PROXY,
    'quality': 'best',
    'player_client': 'web_embedded',
    'min_free_gb': 8,
    'delete_grace_sec': 60,
}

ACTIVE_STATES = ('extracting', 'downloading', 'merging')


def log(*a):
    print('[ytdl]', *a, flush=True)


# ────────────────────────────── 设置 ──────────────────────────────

_settings_lock = threading.Lock()


def load_settings() -> dict:
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text('utf-8')))
    except Exception:
        pass
    s['concurrency'] = max(1, min(4, int(s.get('concurrency') or 1)))
    return s


def save_settings(patch: dict) -> dict:
    with _settings_lock:
        s = load_settings()
        if 'concurrency' in patch:
            s['concurrency'] = max(1, min(4, int(patch['concurrency'])))
        if 'quality' in patch and patch['quality'] in QUALITY:
            s['quality'] = patch['quality']
        if 'proxy' in patch:
            s['proxy'] = str(patch['proxy'] or '').strip()
        if 'player_client' in patch:
            pc = str(patch['player_client'] or 'web_embedded').strip()
            s['player_client'] = pc or 'web_embedded'
        if 'min_free_gb' in patch:
            s['min_free_gb'] = max(1, min(200, int(patch['min_free_gb'])))
        SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), 'utf-8')
        return s


# ────────────────────────────── 数据库 ──────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  url          TEXT    NOT NULL,
  quality      TEXT    NOT NULL,
  title        TEXT    NOT NULL DEFAULT '',
  state        TEXT    NOT NULL DEFAULT 'queued',
  pct          REAL    NOT NULL DEFAULT 0,
  done_bytes   INTEGER NOT NULL DEFAULT 0,
  total_bytes  INTEGER NOT NULL DEFAULT 0,
  speed        REAL    NOT NULL DEFAULT 0,
  eta          INTEGER,
  file         TEXT    NOT NULL DEFAULT '',
  size         INTEGER NOT NULL DEFAULT 0,
  error        TEXT    NOT NULL DEFAULT '',
  attempts     INTEGER NOT NULL DEFAULT 0,
  created_at   INTEGER NOT NULL,
  started_at   INTEGER NOT NULL DEFAULT 0,
  finished_at  INTEGER NOT NULL DEFAULT 0,
  retrieved_at INTEGER NOT NULL DEFAULT 0,
  delete_at    INTEGER NOT NULL DEFAULT 0,
  platform     TEXT    NOT NULL DEFAULT '',
  all_parts    INTEGER NOT NULL DEFAULT 0,
  mode         TEXT    NOT NULL DEFAULT 'queue',
  part_index   INTEGER NOT NULL DEFAULT 0,
  name_hint    TEXT    NOT NULL DEFAULT '',
  referer      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_state ON tasks(state);
"""

# 老库升级：新加的列用 ALTER 补，不要求用户删库
MIGRATIONS = (
    ("platform", "ALTER TABLE tasks ADD COLUMN platform TEXT NOT NULL DEFAULT ''"),
    ("all_parts", "ALTER TABLE tasks ADD COLUMN all_parts INTEGER NOT NULL DEFAULT 0"),
    ("mode", "ALTER TABLE tasks ADD COLUMN mode TEXT NOT NULL DEFAULT 'queue'"),
    ("part_index", "ALTER TABLE tasks ADD COLUMN part_index INTEGER NOT NULL DEFAULT 0"),
    # 油猴脚本直传 X 媒体直链时，CDN 上只有一个随机文件名（oWohfiQdHBKfiUHX.mp4），
    # 所以由前端把「作者 + 正文 + 推文 ID」当文件名提示带过来，服务端照抄
    ("name_hint", "ALTER TABLE tasks ADD COLUMN name_hint TEXT NOT NULL DEFAULT ''"),
    ("referer", "ALTER TABLE tasks ADD COLUMN referer TEXT NOT NULL DEFAULT ''"),
)


class DB:
    def __init__(self, path: Path):
        self._lock = threading.RLock()
        self._con = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._con.row_factory = sqlite3.Row
        with self._lock:
            self._con.execute('PRAGMA journal_mode=WAL')
            self._con.execute('PRAGMA synchronous=NORMAL')
            self._con.executescript(SCHEMA)
            cols = {r[1] for r in self._con.execute('PRAGMA table_info(tasks)')}
            for name, sql in MIGRATIONS:
                if name not in cols:
                    self._con.execute(sql)
                    log(f'迁移: 新增列 {name}')
            # 老任务没有 platform，按 URL 回填一次
            self._con.execute(
                "UPDATE tasks SET platform='bilibili' WHERE platform='' AND "
                "(url LIKE '%bilibili.com%' OR url LIKE '%b23.tv%' OR url LIKE '%bilibili.tv%')")
            self._con.execute(
                "UPDATE tasks SET platform='youtube' WHERE platform='' AND "
                "(url LIKE '%youtube.com%' OR url LIKE '%youtu.be%')")
            self._con.commit()

    def add(self, url: str, quality: str, platform: str = '', all_parts: bool = False,
            mode: str = 'queue', part_index: int = 0, name_hint: str = '',
            referer: str = '') -> int:
        with self._lock:
            cur = self._con.execute(
                'INSERT INTO tasks(url, quality, state, created_at, platform, all_parts, mode, '
                'part_index, name_hint, referer) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (url, quality, 'queued', int(time.time()),
                 platform or platform_of(url), 1 if all_parts else 0,
                 'direct' if mode == 'direct' else 'queue', int(part_index or 0),
                 (name_hint or '')[:160], (referer or '')[:300]))
            self._con.commit()
            return int(cur.lastrowid)

    def count_active_platform(self, platform: str) -> int:
        with self._lock:
            q = ','.join('?' * len(ACTIVE_STATES))
            return self._con.execute(
                f'SELECT COUNT(*) FROM tasks WHERE state IN ({q}) AND platform=?',
                (*ACTIVE_STATES, platform)).fetchone()[0]

    def get(self, tid: int):
        with self._lock:
            return self._con.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()

    def find(self, where: str, args=()) :
        with self._lock:
            return self._con.execute(f'SELECT * FROM tasks WHERE {where}', args).fetchall()

    def list(self, limit: int = 120):
        with self._lock:
            return self._con.execute(
                'SELECT * FROM tasks ORDER BY id DESC LIMIT ?', (limit,)).fetchall()

    def count_active(self) -> int:
        with self._lock:
            q = ','.join('?' * len(ACTIVE_STATES))
            return self._con.execute(
                f'SELECT COUNT(*) FROM tasks WHERE state IN ({q})', ACTIVE_STATES).fetchone()[0]

    def claim_next(self, skip_platform: str = ''):
        """原子地把最早一条 queued 改成 extracting，避免多线程抢同一条。

        skip_platform 用来做平台隔离：B 站任务独占一路，不被 YouTube 挤占。
        排序里 direct 优先：直下任务是人正盯着等结果的，插到离线队列前面。
        """
        with self._lock:
            sql = "SELECT * FROM tasks WHERE state='queued'"
            args: tuple = ()
            if skip_platform:
                sql += ' AND platform!=?'
                args = (skip_platform,)
            row = self._con.execute(
                sql + " ORDER BY (mode='direct') DESC, id LIMIT 1", args).fetchone()
            if not row:
                return None
            cur = self._con.execute(
                "UPDATE tasks SET state='extracting', started_at=?, attempts=attempts+1 "
                "WHERE id=? AND state='queued'", (int(time.time()), row['id']))
            self._con.commit()
            return self.get(row['id']) if cur.rowcount else None

    def update(self, tid: int, **kw):
        if not kw:
            return
        cols = ','.join(f'{k}=?' for k in kw)
        with self._lock:
            self._con.execute(f'UPDATE tasks SET {cols} WHERE id=?', (*kw.values(), tid))
            self._con.commit()

    def remove(self, tid: int) -> None:
        """物理删掉一条记录行（不是标 removed，标 removed 还会留在列表里）。"""
        with self._lock:
            self._con.execute('DELETE FROM tasks WHERE id=?', (tid,))
            self._con.commit()

    def reset_stale(self) -> int:
        """进程重启：把上次没跑完的弄回队列。"""
        with self._lock:
            q = ','.join('?' * len(ACTIVE_STATES))
            cur = self._con.execute(
                f"UPDATE tasks SET state='queued', speed=0, eta=NULL "
                f"WHERE state IN ({q})", ACTIVE_STATES)
            self._con.commit()
            return cur.rowcount


db = DB(DB_FILE)


# ────────────────────────────── yt-dlp 引擎 ──────────────────────────────

def in_playlist(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if 'list' not in parse_qs(parts.query):
        return False
    q = parse_qs(parts.query)
    if 'v' in q:
        return True
    segs = [s for s in parts.path.split('/') if s]
    if parts.netloc.endswith('youtu.be') and segs:
        return True
    return len(segs) >= 2 and segs[0] in ('shorts', 'live', 'embed', 'v')


class Canceled(Exception):
    pass


def human_err(msg: str) -> str:
    low = msg.lower()
    if 'sign in to confirm' in low or 'not a bot' in low or 'login_required' in low:
        return 'YouTube 要求验证：登录态(cookies)过期或被判机器人，请到设置里重新上传 cookies'
    if 'no video could be found' in low:
        return '这条推文里没有可下载的视频（纯文字/纯图片）；如果页面明明有视频，就是内容被限制、匿名读不到，需要上传 X 的 cookies'
    # X 的报错五花八门，这几种本质都是"匿名接口不给看了"，要引导去补登录态
    if 'authorization: denied' in low or 'failed to parse json' in low or 'guest token' in low:
        return 'X 的匿名接口被风控拦了：补一份 X 的 cookies，或者换个代理节点再试'
    if 'nsfw' in low or 'not authorized' in low or 'age-restricted' in low:
        return '该内容需要 X 登录态（敏感内容 / 私密账号 / 地区限制）'
    if '429' in low or 'rate limit' in low or 'too many requests' in low:
        return '被限流（429）：等几分钟再试，或者换一个代理节点'
    if 'tweet' in low and ('deleted' in low or 'not found' in low):
        return '推文已删除或不可见'
    if 'ffmpeg' in low and 'not found' in low:
        return 'ffmpeg 不可用，镜像里缺少 ffmpeg'
    if 'unable to download' in low and 'ejs' in low:
        return 'JS 挑战组件下载失败：代理不通 GitHub，检查代理'
    if 'requested format is not available' in low:
        return '该清晰度不可用（源端没有这个分辨率），换"最高画质"重试'
    if '412' in low or 'too many requests' in low:
        return 'B 站限流（412）：并发开太高，等几分钟再试，或把并发降到 1'
    if '-352' in low or 'risk control' in low or '风控' in msg:
        return 'B 站风控拦截：登录态失效或在异地，到设置里重新上传 B 站 cookies'
    if '-404' in low and 'bilibili' in low:
        return 'B 站返回 404：视频已删除/地区限制/需要大会员'
    if 'premium' in low or '大会员' in msg or 'vip' in low:
        return '该视频的最高清晰度需要大会员，已按非会员最高档（1080P）处理'
    if 'unexpected_eof' in low or 'ssl' in low or 'connection reset' in low:
        return '代理连接被中断（SSL/连接重置）：点「重试」就行，反复出现就换个代理节点'
    if 'timed out' in low or 'timeout' in low:
        return '网络超时：代理节点不稳定，换个节点或降低并发'
    return msg.strip().splitlines()[-1][:280] if msg.strip() else '下载失败'


class Engine:
    def __init__(self):
        self.cancel: set[int] = set()
        self.last_log: dict[int, str] = {}
        self.vid: dict[int, str] = {}
        self._stop = False
        self._flush: dict[int, float] = {}
        self._threads: dict[int, threading.Thread] = {}
        # 本任务写过哪些文件。番剧/合集取消时要靠这个精确清理，
        # 不能靠"目录里新的都删"——会把并行任务的产物一起删掉
        self.files: dict[int, set] = {}

    # ---------- 调度 ----------
    def start(self):
        db.reset_stale()
        threading.Thread(target=self._scheduler, name='sched', daemon=True).start()
        threading.Thread(target=self._janitor, name='janitor', daemon=True).start()

    def _scheduler(self):
        while not self._stop:
            try:
                n = int(load_settings()['concurrency'])
                if db.count_active() < n:
                    # B 站同时只跑 1 个：同 IP 并发高了容易被 412 风控，
                    # 它本身又是直连 30MB/s，串行反而整体更快
                    skip = 'bilibili' if db.count_active_platform('bilibili') >= 1 else ''
                    row = db.claim_next(skip)
                    if row:
                        t = threading.Thread(target=self._run, args=(row['id'],),
                                             name=f'task-{row["id"]}', daemon=True)
                        self._threads[row['id']] = t
                        t.start()
                        continue
            except Exception:
                log('scheduler error', traceback.format_exc(limit=2))
            time.sleep(1.0)

    def _janitor(self):
        while not self._stop:
            try:
                now = int(time.time())
                for r in db.find('delete_at>0 AND delete_at<=?', (now,)):
                    p = Path(r['file']) if r['file'] else None
                    if p and p.exists():
                        try:
                            p.unlink()
                        except Exception as e:
                            log('删除失败', p, e)
                    db.update(r['id'], state='removed', delete_at=0, file='')
                    log('已取回并删除', r['id'])
            except Exception:
                log('janitor error', traceback.format_exc(limit=2))
            time.sleep(3)

    def cancel_task(self, tid: int):
        self.cancel.add(tid)

    # ---------- 参数 ----------
    def _opts(self, tid: int, url: str, quality: str, all_parts: bool = False,
              part_index: int = 0, name_hint: str = '', referer: str = '') -> dict:
        s = load_settings()
        plat = platform_of(url)
        height, suffix, is_audio = plan(plat, quality)
        if plat == 'twitter' and part_index:
            # 一条推文多段媒体：靠 playlist_items 精确挑第 N 段，文件名加后缀区分，
            # 不然后面几段的文件名会和第一段撞在一起（推文标题是同一句话）
            suffix += f'_p{part_index}'
        if is_audio:
            fmt, sort = 'ba[ext=m4a]/ba/b', None
        elif plat == 'twitter':
            # 见 plan()：竖屏视频的 height 是长边，加 height 过滤会把 1080P 滤成 480P，
            # 这里直接用 b（音视频合成的最高码率档），不叠任何高度限制
            fmt = 'b'
            sort = None
        elif plat == 'bilibili':
            # 用 format 过滤硬压高度，而不是只靠排序：万一是大会员账号，
            # API 会吐出 4K/HDR，排序压不住。这里直接把 >1080 的全滤掉。
            fmt = f'bv*[height<={height}]+ba/b[height<={height}]/b'
            sort = [f'res:{height}', 'vcodec:avc1', 'acodec:m4a']
        else:
            fmt = 'bv*+ba/b'
            sort = [f'res:{height}', 'vcodec:avc1', 'acodec:m4a']

        clients = [c.strip() for c in str(s['player_client']).split(',') if c.strip()] or ['web_embedded']
        opts: dict = {
            'quiet': True,
            'noprogress': True,
            'no_warnings': False,
            'logger': _Logger(tid),
            'progress_hooks': [lambda d: self._hook(tid, d)],
            'postprocessor_hooks': [lambda d: self._pp_hook(tid, d)],
            'format': fmt,
            'outtmpl': {'default': str(DL_DIR / f'%(title)s [%(id)s]{suffix}.%(ext)s')},
            'merge_output_format': 'mp4',
            'continuedl': True,
            'retries': 12,
            'fragment_retries': 12,
            # 走代理时偶发 SSL 中断，解析阶段也多给几次（默认 3 次不够稳）
            'extractor_retries': 3,
            'concurrent_fragment_downloads': 4,
            'socket_timeout': 30,
            'ffmpeg_location': FFMPEG_DIR,
            'js_runtimes': {'node': {'path': NODE_BIN}},
            'extractor_args': {
                'youtube': {'player_client': clients},
                'youtubepot-bgutilhttp': {'base_url': [POT_URL]},
            },
            # B 站分P/合集/番剧：默认只下当前这一 P
            'overwrites': False,
            'postprocessor_args': {'ffmpeg': ['-loglevel', 'error']},
        }
        if plat == 'bilibili':
            # 坑：番剧季地址（/bangumi/play/ssXXXX）不吃 noplaylist，
            # 光设 noplaylist=True 会把整季逐集全下（实测 ss26801 下了 6 集才被取消）。
            # 必须再用 playlist_items 截断到第 1 项。
            opts['noplaylist'] = not all_parts
            if not all_parts:
                opts['playlist_items'] = '1'
        elif plat == 'twitter':
            if TW_STATUS_RE.search(url):
                # 多段媒体在提交时已经拆成独立任务，所以一个任务永远只允许落一个文件。
                # 探测失败（网络抖动）时也不会一口气吐出好几个文件把取回逻辑搞乱。
                opts['noplaylist'] = False
                opts['playlist_items'] = str(part_index) if part_index else '1'
                # 推文正文当标题：中文 280 字约 840 字节，直接超过文件名 255 字节上限，
                # 会报 [Errno 36] File name too long。截断到 40 字 + 推文 ID 兜底，
                # windowsfilenames 顺带把 : / \ 之类字符换掉，回传到 Windows 也不会出问题
                opts['outtmpl'] = {'default': str(
                    DL_DIR / f'%(title).40s [%(id)s]{suffix}.%(ext)s')}
            else:
                # 页面里抓到的 CDN 直链（video.twimg.com/.../1080x1920/xxx.mp4）：
                # 它本身就是一个完整文件，没有 playlist 概念，硬设 playlist_items
                # 会让 generic 提取器找"第 1 项"而失败
                opts['noplaylist'] = True
                # CDN 上的文件名是随机的，用前端带过来的提示名，列表里才认得出是什么
                if name_hint:
                    opts['outtmpl'] = {'default': str(
                        DL_DIR / f'{safe_name(name_hint)}{suffix}.%(ext)s')}
            opts['windowsfilenames'] = True
        else:
            opts['noplaylist'] = in_playlist(url)
        if referer:
            # X 的 CDN 实测不带 Referer 也放行（curl 直接 200），带上更稳
            opts['http_headers'] = {'Referer': referer}
        if sort:
            opts['format_sort'] = sort
        # 默认用镜像里自带的 ejs（yt-dlp-ejs 0.8.0），比每次去 GitHub 拉快一倍；
        # 万一 YouTube 改了挑战，可在 compose 里设 EJS_REMOTE=1 强制拉最新版
        if plat != 'bilibili' and os.environ.get('EJS_REMOTE', '').lower() in ('1', 'true', 'yes'):
            opts['remote_components'] = ['ejs:github']
        # 代理：B 站直连（实测 30MB/s，绕代理反而绕出国再回来），其余走 mihomo
        if plat != 'bilibili' and s.get('proxy'):
            opts['proxy'] = s['proxy']
        # 登录态分平台，各用各的（X 单独一份，避免被 YouTube 的定期刷新冲掉）
        ck = {'bilibili': BILI_COOKIE_FILE, 'twitter': TW_COOKIE_FILE}.get(plat, COOKIE_FILE)
        if ck.exists() and ck.stat().st_size > 200:
            opts['cookiefile'] = str(ck)
        if is_audio:
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '0',
            }]
        return opts

    def _hook(self, tid: int, d: dict):
        if tid in self.cancel:
            raise Canceled('用户取消')
        st = d.get('status')
        if st == 'downloading':
            fn = d.get('filename')
            if fn:
                self.files.setdefault(tid, set()).add(fn)
            now = time.time()
            if now - self._flush.get(tid, 0) < 0.4:
                return
            self._flush[tid] = now
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            got = d.get('downloaded_bytes') or 0
            info = d.get('info_dict') or {}
            title = info.get('title') or ''
            if info.get('id'):
                self.vid[tid] = info['id']
            kw = dict(state='downloading', done_bytes=got, total_bytes=total,
                      speed=d.get('speed') or 0, eta=d.get('eta'))
            if total:
                kw['pct'] = round(got / total * 100, 1)
            if title:
                kw['title'] = title
            db.update(tid, **kw)
        elif st == 'finished':
            # 合流后的最终文件记下来，取消时才能精确删掉（番剧会下完好几集才被取消）
            fin = (d.get('info_dict') or {}).get('filepath') or d.get('filename')
            if fin:
                self.files.setdefault(tid, set()).add(fin)
            db.update(tid, state='merging', pct=100.0, speed=0, eta=0)
        elif st == 'error':
            db.update(tid, state='failed', speed=0)

    def _pp_hook(self, tid: int, d: dict):
        if tid in self.cancel:
            raise Canceled('用户取消')
        if d.get('status') in ('started', 'processing'):
            db.update(tid, state='merging', speed=0, eta=0)

    # ---------- 执行 ----------
    def _run(self, tid: int):
        from yt_dlp import YoutubeDL
        row = db.get(tid)
        if not row:
            return
        url, quality = row['url'], row['quality']
        all_parts = bool(row['all_parts']) if 'all_parts' in row.keys() else False
        part_index = int(row['part_index'] or 0) if 'part_index' in row.keys() else 0
        name_hint = (row['name_hint'] or '') if 'name_hint' in row.keys() else ''
        referer = (row['referer'] or '') if 'referer' in row.keys() else ''
        plat = platform_of(url)
        s = load_settings()
        free_gb = shutil.disk_usage(DL_DIR).free / 1024 ** 3
        if free_gb < float(s['min_free_gb']):
            db.update(tid, state='failed', finished_at=int(time.time()),
                      error=f'磁盘只剩 {free_gb:.1f} GB，低于阈值 {s["min_free_gb"]} GB，请先清理')
            return
        db.update(tid, state='extracting', error='', pct=0, done_bytes=0, total_bytes=0,
                  speed=0, eta=None, file='', size=0)
        self.last_log[tid] = '解析视频信息...'
        log(f'#{tid} {plat} {quality} all_parts={all_parts} {url[:70]}')
        try:
            with YoutubeDL(self._opts(tid, url, quality, all_parts, part_index,
                                      name_hint, referer)) as ydl:
                info = ydl.extract_info(url, download=True)
            if tid in self.cancel:
                raise Canceled('用户取消')
            if (info or {}).get('id'):
                self.vid[tid] = info['id']
            path = self._find_output(info, tid)
            if path is None:
                raise RuntimeError('下载结束但没找到输出文件')
            size = path.stat().st_size
            title = (info or {}).get('title') or path.stem
            if plat == 'twitter':
                # 直链下载时 info['title'] 只是 CDN 的随机文件名，用前端给的提示名
                if name_hint:
                    title = safe_name(name_hint)
                # 多段媒体的每一段标题都是同一句推文正文，拼上去只会重复，
                # 用段号区分，列表里才看得出先后
                if part_index:
                    title = f'{title} · 第 {part_index} 段'
            else:
                # 番剧 ss 地址返回的是整季的 playlist，info['title'] 只有季名（"鬼灭之刃"），
                # 拼上实际下载那一集的标题才看得懂
                ents = (info or {}).get('entries') or []
                e0 = next((e for e in ents if e), None)
                if e0 and e0.get('title'):
                    title = f'{title} - {e0["title"]}'
            db.update(tid, state='done', pct=100.0, speed=0, eta=0,
                      file=str(path), size=size, total_bytes=size, done_bytes=size,
                      title=title, finished_at=int(time.time()), error='',
                      retrieved_at=0, delete_at=0)
            self.last_log[tid] = f'完成：{path.name}'
            log(f'#{tid} done {path.name} {size/1048576:.1f}MB')
        except Canceled:
            self._clean_partials(tid)
            row = db.get(tid)
            if row and row['state'] != 'removed':
                db.update(tid, state='canceled', speed=0, eta=None,
                          finished_at=int(time.time()))
            self.last_log[tid] = '已取消'
            log(f'#{tid} canceled')
        except Exception as e:  # noqa: BLE001
            msg = human_err(str(e))
            db.update(tid, state='failed', speed=0, eta=None,
                      error=msg, finished_at=int(time.time()))
            self.last_log[tid] = msg
            log(f'#{tid} failed: {msg}')
        finally:
            self.cancel.discard(tid)
            self._flush.pop(tid, None)
            self.vid.pop(tid, None)
            self.files.pop(tid, None)
            self._threads.pop(tid, None)

    def _clean_partials(self, tid: int):
        """取消后清掉这个任务留下的文件，避免孤儿分片和半截视频占空间。

        番剧/合集这一类，取消前可能已经整集下完好几个文件，
        所以除了按 id 找 .part，还要把本任务确实写过的文件删掉
        （只删自己写过的，绝不按"目录里最新的"来删——会误伤并行任务）。
        """
        seen = self.files.pop(tid, set())
        referenced = {r['file'] for r in db.list(500) if r['file']}
        for f in seen:
            p = Path(f)
            stem = p.with_suffix('')
            cands = {p, Path(str(p) + '.part')}
            for ext in ('.mp4', '.mkv', '.webm', '.m4a', '.mp3'):
                cands.add(stem.parent / (stem.name + ext))
                cands.add(stem.parent / (Path(stem.name).with_suffix('').name + ext))
            for cand in cands:
                if not cand.exists() or str(cand) in referenced:
                    continue
                try:
                    cand.unlink()
                    log(f'#{tid} 清理残留 {cand.name}')
                except Exception:
                    pass
        vid = self.vid.get(tid)
        if not vid:
            return
        for pat in (f'*[{vid}]*.part', f'*[{vid}]*.part-Frag*', f'*[{vid}]*.ytdl'):
            for p in DL_DIR.glob(pat):
                if str(p) in referenced:
                    continue
                try:
                    p.unlink()
                except Exception:
                    pass

    def _find_output(self, info, tid: int):
        # 合集展开理论上不会走到这里（提交时就按 P 拆成多条任务了），
        # 万一 yt-dlp 还是返回了 playlist，就取最后落盘的那个文件
        if (info or {}).get('_type') == 'playlist' or (info or {}).get('entries'):
            # 先看条目自己报的落盘路径：X 的多段媒体就是一个只含 1 条的 playlist，
            # 按「目录里最新的文件」猜，并行下载时会认成别的任务刚下完的文件
            for e in (info or {}).get('entries') or []:
                if not e:
                    continue
                p = e.get('filepath')
                if p and Path(p).exists():
                    return Path(p)
                for d in e.get('requested_downloads') or []:
                    p = d.get('filepath')
                    if p and Path(p).exists():
                        return Path(p)
            cands = [p for p in DL_DIR.iterdir()
                     if p.is_file() and p.suffix not in ('.part', '.ytdl')]
            return max(cands, key=lambda p: p.stat().st_mtime) if cands else None
        for key in ('filepath',):
            p = (info or {}).get(key)
            if p and Path(p).exists():
                return Path(p)
        for d in (info or {}).get('requested_downloads') or []:
            p = d.get('filepath')
            if p and Path(p).exists():
                return Path(p)
        vid = (info or {}).get('id')
        if vid:
            cands = [p for p in DL_DIR.glob(f'*[{vid}]*')
                     if p.is_file() and p.suffix not in ('.part', '.ytdl')]
            if cands:
                return max(cands, key=lambda p: p.stat().st_mtime)
        return None


class _Logger:
    def __init__(self, tid: int):
        self.tid = tid

    def debug(self, msg):
        if msg.startswith('[debug] '):
            return
        self._keep(msg)

    def info(self, msg):
        self._keep(msg)

    def warning(self, msg):
        self._keep('警告：' + msg)

    def error(self, msg):
        self._keep('错误：' + msg)

    def _keep(self, msg):
        m = str(msg).strip()
        if m:
            engine.last_log[self.tid] = m[:180]


engine = Engine()


# ────────────────────────────── 取回（Range 覆盖统计） ──────────────────────────────

_cover_lock = threading.Lock()
_coverage: dict[int, list[tuple[int, int]]] = {}


def record_range(tid: int, size: int, start: int, end: int):
    """记录客户端实际收完的字节区间；覆盖全文件即视为已取回。"""
    if size <= 0:
        return
    with _cover_lock:
        spans = _coverage.setdefault(tid, [])
        spans.append((start, end))
        spans.sort()
        merged: list[tuple[int, int]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        _coverage[tid] = merged
        covered = sum(e - s + 1 for s, e in merged)
    if covered >= size:
        row = db.get(tid)
        if row and not row['retrieved_at']:
            grace = int(load_settings().get('delete_grace_sec') or 60)
            now = int(time.time())
            db.update(tid, retrieved_at=now, delete_at=now + grace)
            log(f'#{tid} 已取回，{grace}s 后删除')


def clear_coverage(tid: int):
    with _cover_lock:
        _coverage.pop(tid, None)


# ────────────────────────────── Web ──────────────────────────────

app = FastAPI(title='N100 离线下载站', docs_url=None, redoc_url=None)

# 油猴脚本在 bilibili / youtube 页面上直接调这里的接口，必须放行跨域。
# 只放行这两家的域名（以及本机），别把整个局域网敞开给任意网站。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r'https?://([a-z0-9-]+\.)*(bilibili\.com|bilibili\.tv|youtube\.com|youtu\.be|localhost|127\.0\.0\.1)(:\d+)?',
    allow_methods=['GET', 'POST', 'DELETE', 'HEAD', 'OPTIONS'],
    allow_headers=['content-type'],
    max_age=600,
)

_SECRET = hashlib.sha256((APP_PASSWORD + '::n100-ytdl').encode()).hexdigest()


def _token() -> str:
    return hmac.new(_SECRET.encode(), b'v1', hashlib.sha256).hexdigest()


def require_auth(request: Request):
    if not APP_PASSWORD:
        return
    if request.cookies.get('ytdl_sid') == _token():
        return
    raise HTTPException(status_code=401, detail='需要登录')


_pot_state = {'ok': False, 'checked': 0.0}


def _pot_probe_loop():
    while True:
        try:
            with urllib.request.urlopen(POT_URL.rstrip('/') + '/ping', timeout=3) as r:
                _pot_state['ok'] = r.status == 200
        except Exception:
            _pot_state['ok'] = False
        _pot_state['checked'] = time.time()
        time.sleep(20 if _pot_state['ok'] else 4)


def snapshot(light: bool = False) -> dict:
    rows = [dict(r) for r in db.list()]
    s = load_settings()
    du = shutil.disk_usage(DL_DIR)
    now = int(time.time())
    for r in rows:
        if r['state'] == 'done' and not r['delete_at']:
            r['left'] = 0
        elif r['delete_at']:
            r['left'] = max(0, r['delete_at'] - now)
        if not r['title']:
            r['title'] = r['url']
        r['log'] = engine.last_log.get(r['id'], '')
        ext = Path(r['file']).suffix.lower() if r['file'] else ''
        r['ext'] = ext
        r['kind'] = 'audio' if ext in _AUDIO_EXT else 'video'
    return {
        'now': now,
        'tasks': rows,
        'settings': {k: s[k] for k in ('concurrency', 'quality', 'proxy',
                                       'player_client', 'min_free_gb', 'delete_grace_sec')},
        'qualities': [{'key': k, 'label': v[0]} for k, v in QUALITY.items()],
        'bili_qualities': [
            {'key': 'best', 'label': '最高（1080P，不含大会员）'},
            {'key': '720', 'label': '720P'},
            {'key': 'audio', 'label': '仅音频（MP3）'},
        ],
        'bili_cookies': {
            'exists': BILI_COOKIE_FILE.exists(),
            'size': BILI_COOKIE_FILE.stat().st_size if BILI_COOKIE_FILE.exists() else 0,
            'mtime': int(BILI_COOKIE_FILE.stat().st_mtime) if BILI_COOKIE_FILE.exists() else 0,
        },
        'tw_cookies': {
            'exists': TW_COOKIE_FILE.exists(),
            'size': TW_COOKIE_FILE.stat().st_size if TW_COOKIE_FILE.exists() else 0,
            'mtime': int(TW_COOKIE_FILE.stat().st_mtime) if TW_COOKIE_FILE.exists() else 0,
        },
        'disk': {'free': du.free, 'total': du.total},
        'cookies': {
            'exists': COOKIE_FILE.exists(),
            'size': COOKIE_FILE.stat().st_size if COOKIE_FILE.exists() else 0,
            'mtime': int(COOKIE_FILE.stat().st_mtime) if COOKIE_FILE.exists() else 0,
        },
        'pot': _pot_state['ok'],
        'node': Path(NODE_BIN).exists(),
        'auth': bool(APP_PASSWORD),
    }


@app.on_event('startup')
def _on_start():
    engine.start()
    threading.Thread(target=_pot_probe_loop, daemon=True).start()
    log(f'data={DATA_DIR} node={NODE_BIN} ffmpeg={FFMPEG_BIN} pot={POT_URL} proxy={DEFAULT_PROXY}')


@app.get('/')
def index():
    return FileResponse('/app/static/index.html', media_type='text/html; charset=utf-8')


@app.get('/healthz')
def healthz():
    return {'ok': True}


@app.post('/api/login')
async def login(body: dict):
    pwd = str(body.get('password') or '')
    if APP_PASSWORD and not hmac.compare_digest(pwd, APP_PASSWORD):
        raise HTTPException(status_code=401, detail='口令不对')
    resp = JSONResponse({'ok': True})
    resp.set_cookie('ytdl_sid', _token(), max_age=30 * 86400,
                    httponly=True, samesite='lax', path='/')
    return resp


@app.post('/api/logout')
async def logout():
    resp = JSONResponse({'ok': True})
    resp.delete_cookie('ytdl_sid', path='/')
    return resp


@app.get('/api/state')
def api_state(request: Request):
    require_auth(request)
    return snapshot()


@app.get('/api/events')
async def api_events(request: Request):
    require_auth(request)

    async def gen():
        last = ''
        while True:
            if await request.is_disconnected():
                break
            try:
                payload = json.dumps(snapshot(), ensure_ascii=False)
            except Exception as e:  # noqa: BLE001
                payload = json.dumps({'error': str(e)}, ensure_ascii=False)
            if payload != last:
                last = payload
                yield f'data: {payload}\n\n'
            await asyncio.sleep(0.8)

    return StreamingResponse(gen(), media_type='text/event-stream',
                             headers={'cache-control': 'no-cache',
                                      'x-accel-buffering': 'no',
                                      'connection': 'keep-alive'})


MAX_PARTS = 60


def expand_parts(url: str) -> list[str]:
    """把 B 站分P/合集展开成一条条独立链接。

    用 extract_flat 只拉列表不拉每条详情，正常 3-8 秒。展开成独立任务的好处是
    每一 P 有自己的进度、预览、取回、删除，而不是一个任务吐一堆文件。
    """
    from yt_dlp import YoutubeDL
    opts = {
        'quiet': True, 'no_warnings': True, 'skip_download': True,
        'extract_flat': 'in_playlist', 'noplaylist': False,
        'socket_timeout': 20, 'playlistend': MAX_PARTS,
        'ignoreerrors': True, 'extractor_args': {},
    }
    if BILI_COOKIE_FILE.exists() and BILI_COOKIE_FILE.stat().st_size > 200:
        opts['cookiefile'] = str(BILI_COOKIE_FILE)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = (info or {}).get('entries') or []
    out = []
    for e in entries:
        if not e:
            continue
        u = e.get('webpage_url') or e.get('url')
        if u and u.startswith('http'):
            out.append(u)
        elif e.get('id'):
            out.append(f'https://www.bilibili.com/video/{e["id"]}')
    return out


TW_STATUS_RE = re.compile(r'/status(?:es)?/(\d+)')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


# 探测结果里带一句"人话原因"，提交失败时直接回给前端，
# 不要只回一个空列表让油猴脚本自己猜（那会变成"没有返回任务号"这种鬼话）
TW_LOCKED = ('这条推文匿名读不到（可能已删除 / 私密账号 / 含敏感内容需登录）；'
             '要下这类内容，得先在 N100 上传 X 的 cookies')
TW_NOMEDIA = '这条推文里没有视频（只有文字或纯图片）'


def twitter_probe_fast(url: str) -> tuple[int, str]:
    """快速探测：走 X 的 syndication 接口，约 1-2 秒（yt-dlp 要 4 秒以上）。

    返回 (段数, 原因)，段数含义：
      >0 = 媒体段数；0 = 确实没有视频；-1 = 没探出来（网络抖动，交给 yt-dlp）；
      -2 = 接口明确说这条读不到（tombstone：删除/私密/敏感内容）。
    """
    m = TW_STATUS_RE.search(url)
    if not m:
        return -1, ''      # t.co 短链之类，交给 yt-dlp 去跟跳转
    s = load_settings()
    px = s.get('proxy') or ''
    api = ('https://cdn.syndication.twimg.com/tweet-result?id=%s&lang=en&token=a'
           % m.group(1))
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler(
            {'http': px, 'https': px} if px else {}))
        req = urllib.request.Request(api, headers={'User-Agent': UA})
        with op.open(req, timeout=12) as r:
            data = json.loads(r.read().decode('utf-8', 'replace'))
    except Exception as e:  # noqa: BLE001
        log(f'twitter 快速探测失败 {url[:50]}: {str(e)[:100]}')
        return -1, ''
    # tombstone = 推文存在但不给匿名看（删了 / 保护 / 敏感内容）。这时 mediaDetails
    # 也是空的，如果当成"没有视频"就会把用户的正常请求悄悄跳掉。
    if (not data.get('user')) and (data.get('tombstone') or
                                   str(data.get('__typename') or '').endswith('Tombstone')):
        log(f'twitter 推文不可匿名读取 {url[:60]}')
        return -2, TW_LOCKED
    media = data.get('mediaDetails') or []
    vids = [x for x in media if (x.get('type') or '') in ('video', 'animated_gif')]
    if vids:
        return len(vids), ''
    if data.get('video'):
        return 1, ''
    return 0, TW_NOMEDIA


def twitter_media_count(url: str) -> tuple[int, str]:
    """用 yt-dlp 再探一遍（慢，约 4 秒）。返回 (段数, 原因)，含义同 twitter_probe_fast。

    0 = 这条推文没有视频；-1 = 没探出来（网络抖动、被风控），调用方按单条处理，
    让下载阶段去报真实错误，不要因为探测失败就挡住用户提交。
    """
    from yt_dlp import YoutubeDL
    s = load_settings()
    opts = {
        'quiet': True, 'no_warnings': True, 'skip_download': True,
        'noplaylist': False, 'extract_flat': 'in_playlist',
        'socket_timeout': 20, 'playlistend': MAX_PARTS,
    }
    if s.get('proxy'):
        opts['proxy'] = s['proxy']
    if TW_COOKIE_FILE.exists() and TW_COOKIE_FILE.stat().st_size > 200:
        opts['cookiefile'] = str(TW_COOKIE_FILE)
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        low = str(e).lower()
        if 'no video could be found' in low:
            return 0, TW_NOMEDIA
        if not TW_COOKIE_FILE.exists() and any(k in low for k in (
                'nsfw', 'not authorized', 'age-restricted', 'protected',
                'authorization', 'failed to parse json')):
            return -2, TW_LOCKED
        log(f'twitter 探测失败 {url[:60]}: {str(e)[:120]}')
        return -1, human_err(str(e))
    entries = [e for e in ((info or {}).get('entries') or []) if e]
    return (len(entries) if entries else 1), ''


@app.post('/api/tasks')
def api_add(request: Request, body: dict):
    require_auth(request)
    raw = body.get('urls') or ''
    if isinstance(raw, list):
        raw = '\n'.join(str(x) for x in raw)
    quality = str(body.get('quality') or load_settings()['quality'])
    if quality not in QUALITY:
        quality = 'best'
    all_parts = bool(body.get('all_parts'))
    mode = 'direct' if str(body.get('mode') or '') == 'direct' else 'queue'
    urls = [u.strip() for u in re.split(r'[\s,;]+', str(raw)) if u.strip()]
    urls = [u for u in urls if u.startswith(('http://', 'https://'))]
    if not urls:
        raise HTTPException(status_code=400, detail='没有识别到链接')
    # 前端可以按顺序给每条链接带一个提示（来自油猴脚本抓到的推文信息）：
    #   [{name: 'NASA - xxx [2122404409292337592]', referer: 'https://x.com/NASA/status/...'}]
    hints = body.get('hints') if isinstance(body.get('hints'), list) else []
    ids = []
    warnings = []
    skipped = []
    for idx, u in enumerate(urls):
        hint = hints[idx] if idx < len(hints) and isinstance(hints[idx], dict) else {}
        hint_name = str(hint.get('name') or '')
        hint_ref = str(hint.get('referer') or '')
        plat = platform_of(u)
        # targets 里每项是 (链接, 该链接下的第几段媒体)，0 表示不分段
        targets = [(u, 0)]
        # B 站：勾了「整个合集」→ 先展开成一条条 P，每个 P 独立成任务
        if all_parts and plat == 'bilibili':
            try:
                parts = expand_parts(u)
                if len(parts) > 1:
                    targets = [(p, 0) for p in parts[:MAX_PARTS]]
                    if len(parts) > MAX_PARTS:
                        warnings.append(f'合集共 {len(parts)} P，只排了前 {MAX_PARTS} P')
            except Exception as e:  # noqa: BLE001
                warnings.append(f'合集展开失败，按单个视频处理：{str(e)[:80]}')
        # X：一条推文可能带多段视频/动图，默认全部展开成独立任务。
        # 现有取回逻辑按单文件记字节区间，一个任务吐多个文件会把删除判定搞乱
        elif plat == 'twitter' and not is_tw_cdn(u):
            # CDN 直链（video.twimg.com/.../vid/avc1/1080x1920/xx.mp4）是油猴脚本
            # 在页面里抓到的真实媒体地址，本身就是一份完整文件，不用探测也不用登录态
            try:
                n, why = twitter_probe_fast(u)          # ~1-2 秒
                # 快速接口不带登录态；库里存了 X cookies 的话，yt-dlp 也许还能读到
                if n == -2 and TW_COOKIE_FILE.exists() and TW_COOKIE_FILE.stat().st_size > 200:
                    n = -1
                if n < 0 and n != -2:
                    n, why2 = twitter_media_count(u)    # 退回 yt-dlp，~4 秒
                    why = why2 or why
            except Exception as e:  # noqa: BLE001
                n, why = -1, ''
                warnings.append(f'推文探测失败，按单个视频处理：{str(e)[:80]}')
            if n == 0:
                skipped.append({'url': u, 'reason': why or TW_NOMEDIA})
                continue
            if n == -2:
                # 明确读不到：别建必失败的任务，直接把原因回给前端
                skipped.append({'url': u, 'reason': why or TW_LOCKED})
                continue
            if n > 1:
                cap = min(n, MAX_PARTS)
                targets = [(u, i) for i in range(1, cap + 1)]
                if n > cap:
                    warnings.append(f'这条推文有 {n} 段媒体，只排了前 {cap} 段')
        for t, pi in targets:
            # 多段媒体的提示名加段号，否则两段的文件名一模一样，第二个会覆盖/撞名
            nm = hint_name
            if nm and len(targets) > 1 and pi:
                nm = f'{nm}_p{pi}'
            # 同一链接同一画质同一段已下完就不重复排
            dup = db.find("url=? AND quality=? AND part_index=? AND state IN "
                          "('done','downloading','queued','extracting','merging')",
                          (t, quality, pi))
            # 已取回、正在倒计时删除的旧任务不能复用——文件马上就没了，
            # 前端拿到 id 会去拉一个 404。这种直接建新任务重下一份。
            if dup and not (dup[0]['state'] == 'done' and dup[0]['delete_at']):
                ids.append({'id': dup[0]['id'], 'dup': True, 'state': dup[0]['state']})
                continue
            ids.append({'id': db.add(t, quality, plat, mode=mode, part_index=pi,
                                     name_hint=nm, referer=hint_ref),
                        'dup': False, 'state': 'queued'})
    return {'ok': True, 'added': ids, 'warnings': warnings, 'skipped': skipped}


@app.post('/api/tasks/{tid}/cancel')
def api_cancel(tid: int, request: Request):
    require_auth(request)
    row = db.get(tid)
    if not row:
        raise HTTPException(404, '任务不存在')
    if row['state'] in ACTIVE_STATES:
        engine.cancel_task(tid)
    elif row['state'] == 'queued':
        db.update(tid, state='canceled', finished_at=int(time.time()))
    return {'ok': True}


@app.post('/api/tasks/{tid}/retry')
def api_retry(tid: int, request: Request):
    require_auth(request)
    row = db.get(tid)
    if not row:
        raise HTTPException(404, '任务不存在')
    if row['state'] in ACTIVE_STATES:
        raise HTTPException(409, '正在下载中')
    db.update(tid, state='queued', pct=0, done_bytes=0, total_bytes=0, speed=0,
              eta=None, error='', retrieved_at=0, delete_at=0, finished_at=0)
    return {'ok': True}


@app.post('/api/tasks/{tid}/keep')
def api_keep(tid: int, request: Request):
    """撤销"取回后删除"。"""
    require_auth(request)
    db.update(tid, delete_at=0, retrieved_at=0)
    clear_coverage(tid)
    return {'ok': True}


@app.post('/api/tasks/clear')
def api_clear(request: Request, body: dict | None = None):
    """清空已结束的下载记录（done / failed / canceled / removed）。

    默认连 N100 上还没取回的成品文件一起删（否则磁盘上会攒一堆没人认领的孤儿文件）；
    传 keep_files=true 则只删记录、留文件。排队中和正在跑的任务绝不动。

    body.dry=true 只统计不执行 —— 前端不传，留给手工核一下要删多少。
    """
    require_auth(request)
    body = body or {}
    keep_files = bool(body.get('keep_files'))
    dry = bool(body.get('dry'))

    cleared = 0
    freed = 0
    kept = 0
    failed: list[str] = []
    for row in db.list(limit=100000):
        if row['state'] == 'queued' or row['state'] in ACTIVE_STATES:
            continue
        tid = int(row['id'])
        p = Path(row['file']) if row['file'] else None
        if p and p.exists():
            if keep_files:
                # 记录删了文件还在 → 磁盘上多一个没人认领的文件，明确算进 kept 让前端提示
                kept += 1
            else:
                try:
                    size = p.stat().st_size
                    if not dry:
                        p.unlink()
                    freed += size
                except Exception as e:  # noqa: BLE001
                    failed.append(f'#{tid} 删文件失败：{e}')
                    continue
        cleared += 1
        if not dry:
            db.remove(tid)
            clear_coverage(tid)
    if not dry:
        log(f'清空记录：{cleared} 条，释放 {freed} 字节'
            + (f'，保留 {kept} 个文件' if kept else '')
            + (f'，{len(failed)} 条失败' if failed else ''))
    return {'ok': True, 'cleared': cleared, 'freed': freed,
            'kept': kept, 'failed': failed, 'dry': dry}


@app.delete('/api/tasks/{tid}')
def api_delete(tid: int, request: Request):
    require_auth(request)
    row = db.get(tid)
    if not row:
        raise HTTPException(404, '任务不存在')
    if row['state'] in ACTIVE_STATES:
        engine.cancel_task(tid)
    p = Path(row['file']) if row['file'] else None
    if p and p.exists():
        try:
            p.unlink()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f'删除文件失败：{e}')
    db.update(tid, state='removed', file='', delete_at=0)
    clear_coverage(tid)
    return {'ok': True}


_MEDIA_MIME = {
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm',
    '.mkv': 'video/x-matroska', '.mov': 'video/quicktime', '.avi': 'video/x-msvideo',
    '.ts': 'video/mp2t', '.flv': 'video/x-flv',
    '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.aac': 'audio/aac',
    '.opus': 'audio/ogg', '.ogg': 'audio/ogg', '.oga': 'audio/ogg',
    '.flac': 'audio/flac', '.wav': 'audio/wav',
}

_AUDIO_EXT = {'.mp3', '.m4a', '.aac', '.opus', '.ogg', '.oga', '.flac', '.wav'}


def _media_type(path: Path) -> str:
    return (_MEDIA_MIME.get(path.suffix.lower())
            or mimetypes.guess_type(path.name)[0]
            or 'application/octet-stream')


def _task_file(tid: int):
    row = db.get(tid)
    if not row or not row['file']:
        raise HTTPException(404, '文件不存在')
    path = Path(row['file'])
    if not path.is_file():
        raise HTTPException(404, '文件已被清理')
    return row, path


def _serve_media(tid: int, path: Path, request: Request, *, inline: bool, track: bool):
    """Range 流式响应。inline=True 走网页预览，不参与"取回"统计。

    关键：预览绝不能记 record_range —— 浏览器播一遍就是"覆盖全文件"，
    会被当成已取回，60 秒后把文件删掉。
    """
    size = path.stat().st_size
    start, end, status = 0, size - 1, 200
    rng = request.headers.get('range') or ''
    m = re.match(r'bytes=(\d*)-(\d*)$', rng.strip())
    if m:
        a, b = m.group(1), m.group(2)
        if a == '' and b:
            start = max(0, size - int(b))
            end = size - 1
        else:
            start = int(a or 0)
            end = int(b) if b else size - 1
        if start >= size or start > end:
            return JSONResponse({'detail': '区间越界'}, status_code=416,
                                headers={'content-range': f'bytes */{size}'})
        end = min(end, size - 1)
        status = 206

    headers = {
        'accept-ranges': 'bytes',
        'content-length': str(end - start + 1),
        'content-type': _media_type(path),
        'cache-control': 'no-store',
    }
    if inline:
        headers['content-disposition'] = 'inline'
    else:
        headers['content-disposition'] = f"attachment; filename*=UTF-8''{quote(path.name)}"
    if status == 206:
        headers['content-range'] = f'bytes {start}-{end}/{size}'

    # HEAD：只回头，不传字节（部分播放器/Safari 会先探一下）
    if request.method == 'HEAD':
        return Response(status_code=status, headers=headers)

    def stream():
        remain = end - start + 1
        with path.open('rb') as f:
            f.seek(start)
            while remain > 0:
                chunk = f.read(min(1 << 20, remain))
                if not chunk:
                    break
                remain -= len(chunk)
                yield chunk
        if track and remain == 0:
            record_range(tid, size, start, end)

    return StreamingResponse(stream(), status_code=status, headers=headers)


@app.head('/api/file/{tid}')
@app.get('/api/file/{tid}')
def api_file(tid: int, request: Request):
    """下载取回：attachment + 统计覆盖度，满 100% 触发"取回后删除"。"""
    require_auth(request)
    _row, path = _task_file(tid)
    return _serve_media(tid, path, request, inline=False, track=True)


@app.head('/api/preview/{tid}')
@app.get('/api/preview/{tid}')
def api_preview(tid: int, request: Request):
    """网页内直接预览：inline、不统计取回，看多少遍都不会删文件。"""
    require_auth(request)
    _row, path = _task_file(tid)
    return _serve_media(tid, path, request, inline=True, track=False)


@app.get('/watch/{tid}')
def watch_page(tid: int):
    """独立预览页（新窗口打开），页面自己去 /api/task/{id} 取标题。"""
    return FileResponse('/app/static/watch.html', media_type='text/html; charset=utf-8')


@app.get('/n100.user.js')
def userscript():
    """油猴脚本本体：装过一次以后改脚本只要刷这个地址就能更新。"""
    return FileResponse('/app/static/n100-download.user.js',
                        media_type='text/javascript; charset=utf-8')


# 油猴脚本的自检回传：脚本加载/注入失败时把现场情况发回来，方便远程定位
_US_PINGS: deque = deque(maxlen=30)


@app.post('/api/userscript/ping')
async def userscript_ping(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    hits = body.get('hits')
    rec = {
        'at': int(time.time()),
        'v': str(body.get('v') or '')[:20],
        'site': str(body.get('site') or '')[:20],
        'path': str(body.get('path') or '')[:160],
        'injected': bool(body.get('injected')),
        'parent': str(body.get('parent') or '')[:100],
        'hits': [str(x)[:80] for x in hits][:10] if isinstance(hits, list) else [],
        'err': str(body.get('err') or '')[:200],
        'ua': request.headers.get('user-agent', '')[:140],
    }
    _US_PINGS.append(rec)
    print('[ytdl] 油猴回传 ' + json.dumps(rec, ensure_ascii=False), flush=True)
    return {'ok': True}


@app.get('/api/userscript/pings')
def userscript_pings(request: Request):
    require_auth(request)
    return {'pings': list(_US_PINGS)}


@app.get('/api/task/{tid}')
def api_task(tid: int, request: Request):
    """单条任务详情，给独立预览页用（不必为了一部片子拉整个 /api/state）。"""
    require_auth(request)
    row = db.get(tid)
    if not row:
        raise HTTPException(404, '任务不存在')
    r = dict(row)
    if not r['title']:
        r['title'] = r['url']
    ext = Path(r['file']).suffix.lower() if r['file'] else ''
    r['ext'] = ext
    r['kind'] = 'audio' if ext in _AUDIO_EXT else 'video'
    now = int(time.time())
    r['left'] = max(0, r['delete_at'] - now) if r['delete_at'] else 0
    r['log'] = engine.last_log.get(tid, '')
    return r


# ─── 登录态（cookies）上传 ─────────────────────────────────────────────
# 为什么要支持"粘贴"：
#   Chrome 的 Cookies 数据库（%LOCALAPPDATA%\Google\Chrome\User Data\Default\
#   Network\Cookies）里 encrypted_value 是 DPAPI + AES-GCM 加密的，密钥绑当前
#   Windows 账户；而且 Chrome 跑着的时候数据库被独占锁住，复制都复制不出来。
#   更关键的是 N100 是另一台机器，本来就没有你浏览器的数据。
#   所以只能"从浏览器里导出"，而不是"让 N100 去读"。
#   导出有两条路：装扩展存成 txt（最省事），或者 F12 复制 Cookie 请求头粘进来。

COOKIE_MAGIC = (
    '# Netscape HTTP Cookie File\n'
    '# https://curl.se/docs/http-cookies.html\n'
    '# 由 N100 下载站生成\n\n'
)

# 每个平台要写到哪些域。YouTube 的 SID 系列在 .youtube.com 和 .google.com 上都认，
# 两边都写一份——只写一边会出现"传了却还是未登录"
COOKIE_DOMAINS = {
    'youtube': ('.youtube.com', '.google.com'),
    'bilibili': ('.bilibili.com',),
    'twitter': ('.x.com', '.twitter.com'),
}

# 各平台必须有、且能证明"已登录"的票据。命中任意一个就算数（不同账号/时期会有差异）
COOKIE_KEYS = {
    'youtube': ('SID', 'HSID', 'SSID', 'APISID', 'SAPISID',
                '__Secure-1PSID', '__Secure-3PSID', '__Secure-1PAPISID',
                '__Secure-3PAPISID', 'LOGIN_INFO'),
    'bilibili': ('SESSDATA', 'bili_jct', 'DedeUserID'),
    'twitter': ('auth_token', 'ct0'),
}

COOKIE_LABEL = {'youtube': 'YouTube', 'bilibili': 'B 站', 'twitter': 'X'}


def _parse_cookie_pairs(txt: str) -> list[tuple[str, str]]:
    """把 "a=1; b=2"（或每行一对）拆成 [(a,1), (b,2)]。

    容忍 F12 里连 `Cookie:` 前缀一起复制、带引号、多行粘贴等手抖情况。
    """
    s = re.sub(r'(?im)^\s*cookie\s*:\s*', '', txt)
    out = []
    seen = set()
    for chunk in re.split(r'[;\n\r]+', s):
        chunk = chunk.strip().strip('"').strip("'")
        if not chunk or '=' not in chunk:
            continue
        k, v = chunk.split('=', 1)
        k, v = k.strip(), v.strip()
        if not k or k.startswith('#') or k in seen:
            continue
        seen.add(k)
        out.append((k, v))
    return out


def cookie_text_to_netscape(txt: str, target: str) -> tuple[str, int]:
    """把浏览器里复制出来的 Cookie 串转成 Netscape 格式，返回 (内容, 条数)。"""
    raw = txt.replace('\r\n', '\n').replace('\r', '\n').strip()
    if not raw:
        raise HTTPException(400, '粘贴的内容是空的')

    # 已经是 Netscape 格式（7 列 tab 分隔）→ 原样规范化，别改动用户导出的文件
    lines = [ln for ln in raw.split('\n') if ln.strip()]
    if any(len(ln.split('\t')) >= 7 for ln in lines):
        head = [ln for ln in lines if ln.startswith('#')]
        body = [ln.rstrip() for ln in lines if not ln.startswith('#')]
        if not any('Netscape HTTP Cookie File' in h for h in head):
            head = COOKIE_MAGIC.strip().split('\n') + head
        return '\n'.join(head) + '\n\n' + '\n'.join(body) + '\n', len(body)

    pairs = _parse_cookie_pairs(raw)
    if not pairs:
        raise HTTPException(
            400, '没解析出 cookie。格式应该是 name=value; name2=value2 这样')

    have = {k for k, _ in pairs}
    keys = COOKIE_KEYS.get(target, ())
    if keys and not any(k in have for k in keys):
        raise HTTPException(
            400,
            '没找到 %s 的登录票据（%s 都没出现）。'
            '通常是没登录，或者复制的是 document.cookie —— 那样读不到 HttpOnly '
            '的关键票据，必须从 F12 的 Network → Request Headers 里复制。'
            % (COOKIE_LABEL.get(target, target), ' / '.join(keys[:3])))

    exp = int(time.time()) + 400 * 86400
    rows = []
    for dom in COOKIE_DOMAINS.get(target, ()):
        for k, v in pairs:
            # __Host- 开头的按规范不允许带 domain，带上反而会让整份文件被丢弃
            if k.startswith('__Host-'):
                continue
            rows.append('\t'.join([dom, 'TRUE', '/', 'TRUE', str(exp), k, v]))
    return COOKIE_MAGIC + '\n'.join(rows) + '\n', len(rows)


def _cookie_dest_and_check(txt: str, target: str) -> Path:
    """按平台校验票据并给出落盘路径。文件和粘贴两种来源共用。"""
    if target == 'bilibili':
        if 'SESSDATA' not in txt:
            raise HTTPException(400, '缺 SESSDATA —— 导出/复制时没有处于 B 站登录状态')
        return BILI_COOKIE_FILE
    if target == 'twitter':
        if not ('x.com' in txt or 'twitter.com' in txt
                or 'auth_token' in txt or 'ct0' in txt):
            raise HTTPException(400, '这不像 X 的 cookies（没有 x.com / twitter.com 记录）')
        if 'auth_token' not in txt and 'ct0' not in txt:
            raise HTTPException(400, '缺 auth_token / ct0 —— 导出时没有处于 X 登录状态')
        return TW_COOKIE_FILE
    if 'youtube.com' not in txt and 'SID' not in txt:
        raise HTTPException(400, '这不像 YouTube 的 cookies（没有 youtube.com 记录，也没有 SID）')
    return COOKIE_FILE


@app.post('/api/cookies')
async def api_cookies(request: Request,
                      file: Optional[UploadFile] = File(None),
                      text: Optional[str] = Form(None),
                      target: str = 'youtube'):
    """上传登录态，两种方式二选一（target=youtube | bilibili | twitter，分开存）：

      - file：Netscape 格式的 cookies.txt（浏览器扩展导出）
      - text：F12 里复制出来的 Cookie 请求头（name=value; name2=value2）

    N100 读不到你浏览器里的 cookie（Chrome 用 DPAPI 加密 + 数据库独占锁，
    何况 N100 是另一台机器），所以只能这样"搬"过来。
    """
    require_auth(request)
    if target not in COOKIE_DOMAINS:
        raise HTTPException(400, 'target 只能是 youtube / bilibili / twitter')

    if text and text.strip():
        txt, n = cookie_text_to_netscape(text, target)
        dest = _cookie_dest_and_check(txt, target)
        how = 'paste'
        data = txt.encode('utf-8')
    elif file is not None:
        data = await file.read()
        txt = data.decode('utf-8', 'replace')
        if not txt.strip():
            raise HTTPException(400, '这个文件是空的')
        dest = _cookie_dest_and_check(txt, target)
        how = 'file'
        n = sum(1 for ln in txt.splitlines() if ln.strip() and not ln.startswith('#'))
    else:
        raise HTTPException(400, '要么上传 cookies.txt，要么粘贴 Cookie 字符串')

    dest.write_bytes(data)
    dest.chmod(0o600)
    log(f'登录态已更新 target={target} via={how} lines={n}')
    return {'ok': True, 'lines': n, 'target': target, 'via': how}


@app.delete('/api/cookies')
async def api_cookies_clear(request: Request, target: str = 'youtube'):
    """删掉某个平台的登录态文件（换账号、传错了、想回匿名状态）。"""
    require_auth(request)
    dest = {'youtube': COOKIE_FILE, 'bilibili': BILI_COOKIE_FILE,
            'twitter': TW_COOKIE_FILE}.get(target)
    if dest is None:
        raise HTTPException(400, 'target 只能是 youtube / bilibili / twitter')
    existed = dest.exists()
    dest.unlink(missing_ok=True)
    log(f'登录态已清除 target={target} existed={existed}')
    return {'ok': True, 'target': target, 'cleared': existed}


@app.post('/api/settings')
async def api_settings(request: Request, body: dict):
    require_auth(request)
    return {'ok': True, 'settings': save_settings(body)}


@app.post('/api/diag')
async def api_diag(request: Request):
    """自检：代理是否通 YouTube、出口 IP、node/ffmpeg/PO token 状态。"""
    require_auth(request)
    s = load_settings()
    out = {
        'node': {'path': NODE_BIN, 'exists': Path(NODE_BIN).exists()},
        'ffmpeg': {'path': FFMPEG_BIN, 'exists': Path(FFMPEG_BIN).exists()},
        'pot': _pot_state['ok'],
        'cookies': COOKIE_FILE.exists(),
        'proxy': s.get('proxy') or '(直连)',
    }

    def probe():
        px = s.get('proxy') or ''
        handlers = [urllib.request.ProxyHandler({'http': px, 'https': px} if px else {})]
        opener = urllib.request.build_opener(*handlers)
        t0 = time.time()
        try:
            req = urllib.request.Request('https://www.youtube.com/generate_204',
                                         headers={'User-Agent': 'Mozilla/5.0'})
            with opener.open(req, timeout=12) as r:
                out['youtube'] = {'status': r.status, 'ms': int((time.time() - t0) * 1000)}
        except Exception as e:  # noqa: BLE001
            out['youtube'] = {'status': 0, 'error': str(e)[:120]}
        try:
            with opener.open('https://ipinfo.io/json', timeout=12) as r:
                j = json.loads(r.read().decode())
                out['exit'] = f"{j.get('ip')} {j.get('city')}/{j.get('country')} {j.get('org')}"
        except Exception as e:  # noqa: BLE001
            out['exit'] = f'取不到：{str(e)[:80]}'

    await asyncio.to_thread(probe)
    return out


@app.exception_handler(HTTPException)
async def _http_err(request: Request, exc: HTTPException):
    return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
