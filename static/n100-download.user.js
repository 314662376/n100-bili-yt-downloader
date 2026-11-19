// ==UserScript==
// @name         N100 下载（B站 / YouTube / X）
// @namespace    n100-ytdl
// @version      1.4.0
// @description  在 B 站 / YouTube / X 的视频页加一个下载按钮，交给自建的 N100 下载站。YouTube 和 X 默认加入队列，B 站默认直接下到本机。X 走页面内网络层捕获，敏感推文也能下。
// @author       local
// @downloadURL  http://192.168.50.2:8300/n100.user.js
// @updateURL    http://192.168.50.2:8300/n100.user.js
// @match        https://www.youtube.com/*
// @match        https://m.youtube.com/*
// @match        https://www.bilibili.com/video/*
// @match        https://www.bilibili.com/list/*
// @match        https://www.bilibili.com/bangumi/play/*
// @match        https://www.bilibili.com/medialist/play/*
// @match        https://www.bilibili.com/cheese/play/*
// @match        https://x.com/*
// @match        https://twitter.com/*
// @match        https://mobile.twitter.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
// @connect      localhost
// @connect      *
// @run-at       document-start
// @noframes
// ==/UserScript==

/*
 * 用法：点视频边上的「下载」按钮
 *   YouTube  → 加入 N100 队列（存 N100，之后从网页取回）
 *   X        → 加入 N100 队列；推文里有多段视频时全部排队，每段一条任务
 *              （下载的是页面里抓到的 video.twimg.com 直链，敏感推文/私密账号也能下）
 *   B 站     → 直接下到本机（N100 优先下载，下完自动推给浏览器，N100 上倒计时删除）
 * 地址/画质可在油猴菜单里改。N100 那边只要是「免口令」模式就能用。
 */
(function () {
  'use strict';

  const VER = '1.3.0';
  const T0 = Date.now();
  // 默认连哪个 N100：从「你安装这个脚本的来源地址」反推。
  // 从 http://192.168.1.50:8300/n100.user.js 装的就连 192.168.1.50:8300，
  // 所以代码里不需要写死任何 IP；换了设备/地址也能用起来。
  const DEFAULT_BASE = (function () {
    try {
      const u = (typeof GM_info !== 'undefined' && GM_info.script && GM_info.script.downloadURL) || '';
      if (/^https?:\/\//i.test(u)) return new URL(u).origin;
    } catch (e) { /* 管理器没给就算了 */ }
    return 'http://localhost:8300';
  })();
  const K_BASE = 'n100_base';
  const K_QUALITY = 'n100_yt_quality';
  const QUALITIES = ['best', '4k', '720', 'audio'];
  const K_OFF = 'n100_off';

  // 兼容没有 GM_getValue 的管理器（个别浏览器/插件只给 GM_xmlhttpRequest）
  const store = {
    get(k, d) {
      try {
        if (typeof GM_getValue === 'function') return GM_getValue(k, d);
        const v = localStorage.getItem('n100:' + k);
        return v === null ? d : v;
      } catch (e) { return d; }
    },
    set(k, v) {
      try {
        if (typeof GM_setValue === 'function') GM_setValue(k, v);
        else localStorage.setItem('n100:' + k, String(v));
      } catch (e) { /* 存不下就算了 */ }
    },
  };

  // 地址优先级：油猴菜单里手动配过 > 从安装来源推导 > 首次点击时问一次
  let ASKED_BASE = false;
  function getBase() {
    const saved = String(store.get(K_BASE, '') || '').replace(/\/+$/, '');
    if (saved) return saved;
    if (DEFAULT_BASE && !/^https?:\/\/localhost:8300$/i.test(DEFAULT_BASE)) return DEFAULT_BASE;
    if (!ASKED_BASE) {
      ASKED_BASE = true;
      try {
        const v = prompt('第一次使用：填一下 N100 下载站的地址（例如 http://192.168.1.50:8300）', '');
        if (v && /^https?:\/\//i.test(v.trim())) {
          const x = v.trim().replace(/\/+$/, '');
          store.set(K_BASE, x);
          return x;
        }
      } catch (e) { /* 没有 prompt 的环境 */ }
    }
    return DEFAULT_BASE;
  }

  // 只读不弹窗：菜单标题这种「页面一加载就要显示」的地方用它
  function peekBase() {
    return String(store.get(K_BASE, '') || '').replace(/\/+$/, '') || DEFAULT_BASE;
  }
  const getQuality = () => {
    const q = String(store.get(K_QUALITY, 'best') || 'best');
    return QUALITIES.includes(q) ? q : 'best';
  };

  /* ───────────── 与 N100 通信 ───────────── */

  function req(path, opt = {}) {
    const url = getBase() + path;
    const method = opt.method || 'GET';
    const body = opt.body ? JSON.stringify(opt.body) : null;
    const parse = (status, text) => {
      let data = null;
      try { data = JSON.parse(text); } catch (e) { data = null; }
      if (status >= 200 && status < 300) return data;
      const msg = (data && data.detail) ? data.detail : ('HTTP ' + status);
      throw new Error(status === 401 ? 'N100 设了访问口令，油猴脚本用不了' : msg);
    };

    if (typeof GM_xmlhttpRequest === 'function') {
      return new Promise((resolve, reject) => {
        GM_xmlhttpRequest({
          method, url,
          headers: body ? { 'content-type': 'application/json' } : undefined,
          data: body || undefined,
          timeout: 20000,
          onload: r => { try { resolve(parse(r.status, r.responseText)); } catch (e) { reject(e); } },
          onerror: () => reject(new Error('连不上 N100（' + getBase() + '）')),
          ontimeout: () => reject(new Error('N100 响应超时')),
        });
      });
    }
    return fetch(url, {
      method,
      headers: body ? { 'content-type': 'application/json' } : undefined,
      body: body || undefined,
    }).then(async r => parse(r.status, await r.text()))
      .catch(e => { throw (e instanceof TypeError ? new Error('连不上 N100（' + getBase() + '）') : e); });
  }

  /* ───────────── 小 UI ───────────── */

  // 注意：YouTube / B站 都开了 Trusted Types，innerHTML 赋值会被浏览器直接拦下并抛异常，
  // 所以这里所有节点一律用 DOM API 构建，绝不拼字符串。
  const SVGNS = 'http://www.w3.org/2000/svg';

  function n(tag, props, kids) {
    const e = document.createElement(tag);
    if (props) {
      for (const k in props) {
        const v = props[k];
        if (v == null) continue;
        if (k === 'text') e.textContent = v;
        else if (k === 'style') e.style.cssText = v;
        else if (k === 'on') e.onclick = v;
        else e.setAttribute(k, v);
      }
    }
    (kids || []).forEach(c => { if (c != null) e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return e;
  }

  function icon() {
    const s = document.createElementNS(SVGNS, 'svg');
    s.setAttribute('viewBox', '0 0 24 24');
    s.setAttribute('width', '15');
    s.setAttribute('height', '15');
    s.setAttribute('fill', 'none');
    s.setAttribute('stroke', 'currentColor');
    s.setAttribute('stroke-width', '2');
    s.setAttribute('stroke-linecap', 'round');
    s.setAttribute('stroke-linejoin', 'round');
    s.style.flex = '0 0 auto';
    ['M12 3v12', 'M7 11l5 5 5-5', 'M4 20h16'].forEach(d => {
      const p = document.createElementNS(SVGNS, 'path');
      p.setAttribute('d', d);
      s.appendChild(p);
    });
    return s;
  }

  let toastEl = null, toastTimer = 0;
  function toast(msg, bad) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.id = 'n100-toast';
      toastEl.style.cssText = 'position:fixed;left:50%;top:18px;transform:translateX(-50%);z-index:2147483647;' +
        'padding:8px 16px;border-radius:999px;font:13px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;' +
        'box-shadow:0 6px 24px rgba(0,0,0,.35);pointer-events:none;max-width:80vw;transition:opacity .2s';
      document.body.appendChild(toastEl);
    }
    toastEl.style.background = bad ? '#f0736f' : '#2a2d32';
    toastEl.style.color = '#fff';
    toastEl.style.opacity = '1';
    toastEl.textContent = msg;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.style.opacity = '0'; }, 2600);
  }

  /* 右下角常驻状态条 */
  let pill = null;
  function pillBox() {
    if (pill) return pill;
    pill = document.createElement('div');
    pill.id = 'n100-pill';
    pill.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483646;display:none;' +
      'align-items:center;gap:10px;padding:9px 13px;border-radius:12px;background:#1c1e21;color:#e9eaec;' +
      'border:1px solid #2b2e33;box-shadow:0 8px 28px rgba(0,0,0,.45);' +
      'font:12px/1.4 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;max-width:360px';
    const body = document.createElement('div');
    body.setAttribute('data-body', '');
    body.style.cssText = 'min-width:0';
    const x = n('span', {
      'data-x': '',
      style: 'cursor:pointer;color:#6f7681;font-size:15px;line-height:1;padding:0 2px',
      text: '×',
      on: () => { pill.style.display = 'none'; },
    });
    pill.appendChild(body);
    pill.appendChild(x);
    document.body.appendChild(pill);
    return pill;
  }

  const bText = text => n('b', { text });
  const aLink = (href, text, blank) =>
    n('a', {
      href, text,
      style: 'color:#6f9cf7;margin-left:6px',
      ...(blank ? { target: '_blank', rel: 'noopener' } : {}),
    });

  /** showPill('文本', node, '文本'…) —— 只接受纯文本和节点，不用 innerHTML */
  function showPill() {
    const p = pillBox();
    const body = p.querySelector('[data-body]');
    body.textContent = '';
    for (const k of arguments) {
      if (k == null) continue;
      body.appendChild(typeof k === 'string' ? document.createTextNode(k) : k);
    }
    p.style.display = 'flex';
  }
  function hidePill() { if (pill) pill.style.display = 'none'; }

  /* ───────────── 识别当前页面 ───────────── */

  function site() {
    const h = location.hostname;
    if (/(^|\.)youtube\.com$/.test(h)) return 'youtube';
    if (/(^|\.)bilibili\.com$/.test(h)) return 'bilibili';
    if (/(^|\.)(x\.com|twitter\.com)$/.test(h)) return 'twitter';
    return '';
  }

  function videoUrl() {
    const s = site();
    const u = new URL(location.href);
    if (s === 'youtube') {
      if (/^\/(watch|live)\b/.test(u.pathname) && u.searchParams.get('v')) {
        return 'https://www.youtube.com/watch?v=' + u.searchParams.get('v');
      }
      const m = u.pathname.match(/^\/shorts\/([\w-]+)/);
      if (m) return 'https://www.youtube.com/shorts/' + m[1];
      return '';
    }
    if (s === 'bilibili') {
      // 只保留分P参数，去掉 spm_id_from / vd_source 之类跟踪串
      const p = u.searchParams.get('p');
      return u.origin + u.pathname + (p ? '?p=' + p : '');
    }
    if (s === 'twitter') {
      // 只认状态页（/<user>/status/<id> 或 /i/web/status/<id>），时间线、主页、搜索页都不挂按钮
      const m = u.pathname.match(/^\/(?:[^/]+|i\/web)\/status\/(\d+)/);
      return m ? 'https://x.com/i/status/' + m[1] : '';
    }
    return '';
  }

  /* ───────────── 按钮 ───────────── */

  const BTN_ID = 'n100-dl-btn';
  let btn = null;

  function makeButton(host) {
    const b = document.createElement('button');
    b.id = BTN_ID;
    b.type = 'button';
    b.title = site() === 'bilibili'
      ? '下载这个视频到本机（N100 下载，下完自动推送）'
      : site() === 'twitter'
      ? '把这条推文里的视频加入 N100 队列（存 N100，之后取回）'
      : '加入 N100 下载队列（存 N100，之后取回）';
    b.appendChild(icon());
    b.appendChild(n('span', { text: '下载' }));

    const LIT = 'linear-gradient(180deg,#5c9dff,#2f6fe4)';
    const LIT_HI = 'linear-gradient(180deg,#7ab2ff,#3b7ff0)';

    if (host === 'floating') {
      b.style.cssText = 'position:fixed;right:16px;bottom:76px;z-index:2147483645;display:flex;' +
        'align-items:center;gap:6px;padding:9px 15px;border-radius:999px;cursor:pointer;' +
        (site() === 'youtube' || site() === 'twitter'
          ? 'border:0;background:' + LIT + ';color:#fff;' +
            'font:600 13px/1 Roboto,Arial,sans-serif;box-shadow:0 6px 22px rgba(47,111,228,.5)'
          : 'border:1px solid #2b2e33;background:#1c1e21;color:#e9eaec;' +
            'font:500 13px/1 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;' +
            'box-shadow:0 8px 28px rgba(0,0,0,.45)');
    } else if (host === 'below') {
      // 直接贴在播放器下方——不管页面怎么改版，这个位置总在首屏里看得见
      b.style.cssText = 'display:inline-flex;align-items:center;gap:6px;height:34px;padding:0 15px;' +
        'margin:0 0 10px 12px;border-radius:17px;cursor:pointer;font:600 13px/1 Roboto,Arial,sans-serif;' +
        'border:0;color:#fff;letter-spacing:.2px;background:' + LIT + ';' +
        'box-shadow:0 2px 10px rgba(47,111,228,.45)';
    } else if (site() === 'twitter') {
      // X 的操作栏（回复/转推/喜欢/查看/书签/分享）是纯图标行，插一个 32px 的挠腮
      b.style.cssText = 'display:inline-flex;align-items:center;gap:5px;height:32px;padding:0 14px;' +
        'margin-left:8px;border-radius:999px;cursor:pointer;flex:0 0 auto;box-sizing:border-box;' +
        'font:600 13px/1 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;' +
        'border:0;color:#fff;background:' + LIT + ';box-shadow:0 2px 10px rgba(47,111,228,.45)';
    } else if (site() === 'youtube') {
      b.style.cssText = 'display:inline-flex;align-items:center;gap:6px;height:36px;padding:0 16px;' +
        'margin-right:8px;border-radius:18px;cursor:pointer;font:600 14px/1 Roboto,Arial,sans-serif;' +
        'border:0;color:#fff;letter-spacing:.2px;background:' + LIT + ';' +
        'box-shadow:0 2px 10px rgba(47,111,228,.45)';
    } else {
      // B 站工具栏是「图标在上、数字在下」的竖排 item。
      // 尺寸刻意压小并锁高，避免把 toolbar 撑高、连带触发播放器 resize（起播黑屏的一个诱因）
      b.style.cssText = 'display:inline-flex;align-items:center;gap:5px;height:24px;box-sizing:border-box;' +
        'padding:0 10px;margin-left:12px;align-self:center;border-radius:6px;cursor:pointer;' +
        'white-space:nowrap;line-height:1;' +
        'font:500 12px/1 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;' +
        'border:1px solid var(--line_regular,#e3e5e7);background:var(--bg1,#fff);' +
        'color:var(--text2,#61666d)';
    }
    b.onclick = submit;

    // 亮色按钮：悬停变亮、按下微沉（B 站保持原样的低调样式）
    if (site() !== 'bilibili') {
      b.onmouseenter = () => { if (b.disabled) return;
        b.style.background = LIT_HI;
        b.style.boxShadow = '0 5px 18px rgba(59,127,240,.58)'; };
      b.onmouseleave = () => { if (b.disabled) return;
        b.style.background = LIT;
        b.style.boxShadow = '0 2px 10px rgba(47,111,228,.45)'; };
      b.onmousedown = () => { if (!b.disabled) b.style.transform = 'translateY(1px)'; };
      b.onmouseup = () => { b.style.transform = ''; };
    }
    return b;
  }

  const YT_HOSTS = [
    { sel: '#below', where: 'prepend' },
    { sel: 'ytd-watch-metadata #top-level-buttons-computed', where: 'append' },
    { sel: '#top-level-buttons-computed', where: 'append' },
    { sel: 'ytd-watch-metadata #actions-inner', where: 'append' },
    { sel: 'ytd-watch-metadata #actions', where: 'append' },
    { sel: '#menu-container', where: 'append' },
    { sel: 'ytd-watch-metadata #above-the-fold', where: 'append' },
  ];
  const BILI_HOSTS = [
    { sel: '#arc_toolbar_report .video-toolbar-left', where: 'append' },
    { sel: '#arc_toolbar_report .video-toolbar-left-main', where: 'append' },
    { sel: '#arc_toolbar_report', where: 'append' },
    { sel: '.video-toolbar-left', where: 'append' },
    { sel: '.toolbar-left-item-wrap', where: 'append' },
    { sel: '.video-tool-container', where: 'append' },
  ];
  // X：主推文是页面里第一个 article，操作栏是带「喜欢」按钮的那个 [role=group]。
  // 用 :has 精确定位，退一步只要 group，再退一步直接挂到 article 里，
  // 全都匹配不到就落到右下角悬浮按钮（X 改版很勤，必须有兜底）
  const X_HOSTS = [
    { sel: 'article[data-testid="tweet"] [role="group"]:has([data-testid="like"])', where: 'append' },
    { sel: 'article[data-testid="tweet"] [role="group"]', where: 'append' },
    { sel: 'article[data-testid="tweet"]', where: 'append' },
  ];

  /* ───────────── X：页面内网络层捕获（不受敏感推文限制） ─────────────
   *
   * 为什么必须有这一块：
   *   N100 走的是 X 的匿名接口（cdn.syndication.twimg.com）。遇到敏感内容 / 私密账号 /
   *   已删除的推文，X 只回一个 tombstone 空壳，mediaDetails 是 null —— 只能得出"没有视频"。
   *   但页面本身带着登录态，X 会把真实的 media 列表发给它，里面有 video.twimg.com 的直链：
   *     .../amplify_video/<id>/vid/avc1/1080x1920/xxxx.mp4?tag=16   （渐进式，画+音都在里面）
   *   直链是公开 CDN，不需要登录态、不需要任何 X 接口。所以：页面里抓到 → 直接交给 N100。
   *
   * 实现要点：
   *   1. document-start 就 patch 页面的 fetch / XHR（要 unsafeWindow，不然改不到页面的）；
   *   2. 只认 GraphQL / timeline / tweet-result 的 JSON，且必须含 video_info 才解析；
   *   3. 只收 /<videoid>/pl/<token>.m3u8（master）和 /vid/avc1/WxH/xx.mp4（完整文件）；
   *      pl/avc1/WxH/ 这种子清单是纯视频轨，单下必无声，直接丢；
   *   4. 按「推文 ID → 媒体列表」存起来，点下载时用当前状态 ID 取，取不到再从 DOM 反查。
   */

  const XMED = new Map();     // tweetId -> {items:[{urls:[...], poster, dur}], handle, text, at}
  const XRAW = new Map();     // videoId -> [{url, kind, w, h, br}]（网络层只要到 mp4 时兜底用）
  const TW_VIDEO_PATH = /\/(ext_tw_video|amplify_video|tweet_video)\/(\d+)\//;
  const TW_SUB_PL = /\/pl\/(?:avc1|mp4a|h264|s0|subs)\//i;   // 子清单目录，不是完整视频

  function curStatusId() {
    const m = String(location.pathname || '').match(/\/status(?:es)?\/(\d+)/);
    return m ? m[1] : '';
  }

  function twVidOf(url) {
    const m = String(url).match(TW_VIDEO_PATH);
    return m ? m[2] : '';
  }

  function dimsOfUrl(url) {
    const m = String(url).match(/\/(\d{2,5})x(\d{2,5})\//);
    return m ? [Number(m[1]), Number(m[2])] : [0, 0];
  }

  // 把一条媒体候选归类：完整 mp4 / master m3u8 / 其它（丢掉）
  function classifyMediaUrl(u, bitrate) {
    const path = String(u).split('?')[0];
    if (/\.mp4$/i.test(path)) {
      // /<id>/<0>/<0>/<WxH>/xx.mp4 这种是 HLS 的 init 段，几百字节，不是视频
      if (/\/\d{1,7}\/\d{1,7}\//.test(path)) return null;
      const d = dimsOfUrl(path);
      return { url: u, kind: 'mp4', w: d[0], h: d[1], br: Number(bitrate) || 0 };
    }
    if (/\.m3u8$/i.test(path)) {
      if (TW_SUB_PL.test(path)) return null;        // 纯视频子清单 → 下了没声音
      if (!/\/pl\/[^/]+\.m3u8$/i.test(path)) return null;
      return { url: u, kind: 'hls', w: 0, h: 0, br: Number(bitrate) || 0 };
    }
    return null;
  }

  function setXMed(id, items, handle, text) {
    const old = XMED.get(id);
    let merged = items;
    if (old) {
      // 同一条推文会从时间线、详情页、hover 卡片反复出现，按媒体下标合并取更全的那份
      merged = old.items.slice();
      items.forEach((it, i) => {
        const cur = merged[i];
        if (!cur || it.urls.length > cur.urls.length) { merged[i] = it; return; }
        const seen = new Set(cur.urls.map(x => x.url));
        it.urls.forEach(x => { if (!seen.has(x.url)) { cur.urls.push(x); seen.add(x.url); } });
        if (!cur.poster && it.poster) cur.poster = it.poster;
      });
    }
    XMED.set(id, {
      items: merged,
      handle: handle || (old && old.handle) || '',
      text: text || (old && old.text) || '',
      at: Date.now(),
    });
    // 长会话里时间线能刷出上百条推文，留最近 200 条足够，别把内存吃满
    if (XMED.size > 200) XMED.delete(XMED.keys().next().value);
  }

  function ingestTweet(t) {
    if (!t || typeof t !== 'object') return;
    const lg = t.legacy || t;
    if (!lg || typeof lg !== 'object') return;
    const media = (lg.extended_entities && lg.extended_entities.media) || lg.media;
    if (!Array.isArray(media) || !media.length) return;
    const id = String(lg.id_str || t.rest_id || '');
    if (!id) return;
    const items = [];
    for (const m of media) {
      if (!m) continue;
      const poster = m.media_url_https || m.media_url || '';
      if ((m.type || 'photo') === 'photo') { items.push({ urls: [], poster }); continue; }
      const vi = m.video_info || m.videoInfo || {};
      const vars = Array.isArray(vi.variants) ? vi.variants : [];
      const urls = [];
      for (const v of vars) {
        if (!v || !v.url) continue;
        const c = classifyMediaUrl(v.url, v.bitrate);
        if (c) urls.push(c);
      }
      if (!urls.length) continue;
      items.push({
        urls,
        poster,
        dur: vi.duration_millis ? Math.round(vi.duration_millis / 1000) : 0,
      });
    }
    if (!items.some(x => x.urls.length)) return;
    const ur = t.core && t.core.user_results && (t.core.user_results.result || t.core.user_results);
    const u2 = (ur && (ur.legacy || ur)) || {};
    setXMed(id, items, u2.screen_name || '', String(lg.full_text || '').slice(0, 60));
  }

  // 递归找推文对象。只在 JSON 里含 video_info 时才走到这里，量不大
  function walkTweetJson(root) {
    if (!root || typeof root !== 'object') return;
    const stack = [root];
    let n = 0;
    while (stack.length && n < 60000) {
      const o = stack.pop();
      n++;
      if (!o || typeof o !== 'object') continue;
      if (Array.isArray(o)) {
        for (let i = o.length - 1; i >= 0; i--) stack.push(o[i]);
        continue;
      }
      try {
        const lg = o.legacy;
        if (lg && typeof lg === 'object' && (lg.extended_entities || lg.media)) {
          ingestTweet(o);
          continue;      // 已经是推文对象，不再往下钻（避免把引用推文重复算一遍）
        }
        const tr = o.tweet_results && o.tweet_results.result;
        if (tr) ingestTweet(tr);
        const keys = Object.keys(o);
        for (let i = 0; i < keys.length; i++) {
          const v = o[keys[i]];
          if (v && typeof v === 'object') stack.push(v);
        }
      } catch (e) { /* 单个节点异常不影响其它 */ }
    }
  }

  function safeJson(text) {
    try { return JSON.parse(text); } catch (e) { return null; }
  }

  // 网络层顺手记一下 mp4 直链（页面上真的去拉 progressive mp4 时才有）
  function noteRawMedia(url) {
    const vid = twVidOf(url);
    if (!vid) return;
    const c = classifyMediaUrl(url, 0);
    if (!c || c.kind !== 'mp4') return;
    const arr = XRAW.get(vid) || [];
    if (!arr.some(x => x.url === c.url)) {
      arr.push(c);
      XRAW.set(vid, arr);
    }
    if (XRAW.size > 300) XRAW.delete(XRAW.keys().next().value);
  }

  function wantJson(url) {
    return /\/i\/api\/graphql\//i.test(url) || /\/\d+\.\d+\/timeline\//i.test(url)
      || /tweet-result/i.test(url);
  }

  let X_HOOK_IN_PAGE = false;

  function installXHooks() {
    let W;
    try {
      if (typeof unsafeWindow !== 'undefined' && unsafeWindow) { W = unsafeWindow; X_HOOK_IN_PAGE = true; }
      else W = window;
    } catch (e) { W = window; }
    if (!W || W.__n100Hooked) return;
    W.__n100Hooked = 1;

    // fetch：X 的 GraphQL 全走这里
    try {
      const of = W.fetch;
      if (typeof of === 'function' && !of.__n100) {
        const wrapped = function (input) {
          let url = '';
          try { url = typeof input === 'string' ? input : ((input && input.url) || ''); } catch (e) { /* 忽略 */ }
          const p = of.apply(this, arguments);
          try {
            if (url && wantJson(url)) {
              p.then(r => {
                try {
                  if (!r || !r.ok) return;
                  const ct = (r.headers.get('content-type') || '').toLowerCase();
                  if (ct.indexOf('json') < 0) return;
                  // clone 一份读，不能影响页面自己的消费
                  r.clone().text().then(t => {
                    if (t && t.indexOf('video_info') > -1) walkTweetJson(safeJson(t));
                  }, () => { /* 读失败无所谓 */ });
                } catch (e) { /* 忽略 */ }
              }, () => { /* 请求失败无所谓 */ });
            } else if (url && url.indexOf('video.twimg.com') > -1) {
              noteRawMedia(url);
            }
          } catch (e) { /* 忽略 */ }
          return p;
        };
        wrapped.__n100 = 1;
        W.fetch = wrapped;
      }
    } catch (e) { /* 忽略 */ }

    // XHR：老路径 / 上传类请求还会走它，兜个底
    try {
      const XO = W.XMLHttpRequest && W.XMLHttpRequest.prototype;
      if (XO && !XO.__n100) {
        const oOpen = XO.open, oSend = XO.send;
        XO.open = function (m, u) {
          try { this.__n100url = typeof u === 'string' ? u : ''; } catch (e) { /* 忽略 */ }
          return oOpen.apply(this, arguments);
        };
        XO.send = function () {
          try {
            const u = this.__n100url || '';
            if (u && wantJson(u)) {
              this.addEventListener('load', function () {
                try {
                  const rt = this.responseType;
                  if (!rt || rt === 'text') {
                    const t = this.responseText || '';
                    if (t.indexOf('video_info') > -1) walkTweetJson(safeJson(t));
                  }
                } catch (e) { /* 忽略 */ }
              });
            } else if (u && u.indexOf('video.twimg.com') > -1) {
              noteRawMedia(u);
            }
          } catch (e) { /* 忽略 */ }
          return oSend.apply(this, arguments);
        };
        XO.__n100 = 1;
      }
    } catch (e) { /* 忽略 */ }
  }

  // 页面 DOM 兜底：气泡里 <video poster="...amplify_video_thumb/<视频ID>/...">
  // 封面路径里的数字就是视频 ID，用它反查网络层抓到的直链
  function xMedFromDom(tid) {
    let root = null;
    try {
      root = document.querySelector('article:has(a[href*="/status/' + tid + '"])')
          || document.querySelector('article[data-testid="tweet"]');
    } catch (e) { root = null; }
    if (!root) return null;
    const vids = [];
    try {
      root.querySelectorAll('video').forEach(v => {
        const p = v.getAttribute('poster') || '';
        const m = p.match(/\/(?:amplify_video_thumb|ext_tw_video_thumb|tweet_video_thumb)\/(\d+)\//);
        if (m && vids.indexOf(m[1]) < 0) vids.push(m[1]);
      });
    } catch (e) { /* 忽略 */ }
    const items = vids.map(vid => ({ urls: (XRAW.get(vid) || []).slice(), poster: '' }));
    return items.some(x => x.urls.length) ? { items, handle: '', text: '' } : null;
  }

  // 同一段媒体里挑最优：优先渐进式 mp4（音画一体），再按分辨率、码率
  function pickBestVariant(list) {
    const mp4 = (list || []).filter(x => x.kind === 'mp4');
    if (mp4.length) {
      mp4.sort((a, b) => (b.w * b.h - a.w * a.h) || (b.br - a.br));
      return mp4[0];
    }
    const hls = (list || []).filter(x => x.kind === 'hls');
    hls.sort((a, b) => b.br - a.br);
    return hls[0] || null;
  }

  // 当前这条推文 → 要交给 N100 的直链 + 文件名提示
  function xPackForPage() {
    const tid = curStatusId();
    if (!tid) return null;
    const med = XMED.get(tid) || xMedFromDom(tid);
    if (!med || !med.items) return null;
    const tag = ((med.handle ? '@' + med.handle + ' ' : '') + (med.text || '')).trim().slice(0, 60);
    const base = (tag ? tag + ' ' : '') + '[' + tid + ']';
    const urls = [];
    const hints = [];
    let p = 0;
    for (const it of med.items) {
      const best = pickBestVariant(it.urls);
      if (!best) continue;
      p++;
      urls.push(best.url);
      hints.push({
        name: med.items.length > 1 ? base + ' p' + p : base,
        referer: location.href,
      });
    }
    return urls.length ? { urls, hints, n: p } : null;
  }

  let lastHref = location.href;   // 初始就对齐，否则首次轮询会误判"换页"，把刚插的按钮拆了重建
  let navCount = 0;
  let quietUntil = 0;             // 这段时间内不动 DOM（B 站换 P 后视频正在起播）

  // 元素在页面上真的看得见吗（挂到 display:none 的容器里等于没挂）
  function onScreen(node) {
    if (!node || !node.isConnected) return false;
    const r = node.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    return node.offsetParent !== null || getComputedStyle(node).position === 'fixed';
  }

  function place() {
    if (!document.body) return;
    const s = site();
    if (!s || !videoUrl()) { removeBtn(); return; }
    // 油猴菜单里可以临时关掉按钮（排查页面卡顿时用）
    if (String(store.get(K_OFF, '0')) === '1') { removeBtn(); return; }

    const cur = document.getElementById(BTN_ID);
    // 已在页面上、父容器也还在 —— 就什么都不做。
    // 这里刻意不调 getBoundingClientRect / offsetParent：那是强制同步布局，
    // B 站页面 DOM 巨大，一秒一次就能和视频解码抢主线程，表现为起播黑屏卡一下。
    if (cur && cur.isConnected && cur.parentElement && cur.parentElement.isConnected) return;

    // B 站：视频还没出第一帧时先别动 DOM，等它起播完（最多等 8 秒）；
    // 换 P 之后的 3 秒也先让路。按钮晚几秒出现总比画面黑一下强。
    if (s === 'bilibili') {
      if (Date.now() < quietUntil) return;
      const v = document.querySelector('video');
      if (v && v.readyState < 2 && Date.now() - T0 < 8000) return;
    }

    const hosts = s === 'youtube' ? YT_HOSTS : s === 'twitter' ? X_HOSTS : BILI_HOSTS;
    let target = null, where = 'append', hit = '';
    for (const h of hosts) {
      // :has 这类选择器在旧内核里会直接抛 SyntaxError，别让它把整个 place() 打断
      let el = null;
      try { el = document.querySelector(h.sel); } catch (e) { el = null; }
      if (el) { target = el; where = h.where; hit = h.sel; break; }
    }

    if (target) {
      removeBtn();
      btn = makeButton(where === 'prepend' ? 'below' : 'inline');
      btn.dataset.host = hit;
      if (where === 'prepend') target.insertBefore(btn, target.firstChild);
      else target.appendChild(btn);
      verifyLater(btn, hit);
      return;
    }

    // 找不到站内工具栏（改版、Shorts、加载中）→ 右下角悬浮按钮
    removeBtn();
    btn = makeButton('floating');
    btn.dataset.host = 'floating';
    document.body.appendChild(btn);
  }

  // 插入后只量一次：容器若被隐藏（还没渲染好/被折叠），就换成悬浮按钮。
  // 放到 1.2s 后再做，避开视频起播那几秒；而且只做一次，不是每秒量。
  function verifyLater(el, hit) {
    setTimeout(() => {
      if (!el || !el.isConnected) return;
      if (onScreen(el)) return;
      LASTERR = '挂到 ' + hit + ' 后不可见，已退到悬浮按钮';
      if (el !== btn) return;
      removeBtn();
      btn = makeButton('floating');
      btn.dataset.host = 'floating';
      document.body.appendChild(btn);
    }, 1200);
  }

  function removeBtn() {
    const old = document.getElementById(BTN_ID);
    if (old) old.remove();
    btn = null;
  }

  /* 自检回传：把"脚本有没有跑、注到哪、报了什么错"发回 N100，方便远程定位 */
  let LASTERR = '';

  function safePlace() {
    try { place(); } catch (e) { LASTERR = String((e && e.message) || e).slice(0, 160); }
  }

  function ping() {
    try {
      const hosts = site() === 'youtube' ? YT_HOSTS : BILI_HOSTS;
      const hits = [];
      for (const h of hosts) {
        try { if (document.querySelector(h.sel)) hits.push(h.sel); } catch (e) { /* 忽略 */ }
      }
      const el = document.getElementById(BTN_ID);
      const p = el && el.parentElement;
      req('/api/userscript/ping', {
        method: 'POST',
        body: {
          v: VER, site: site(),
          path: (location.pathname + location.search).slice(0, 150),
          injected: !!el,
          parent: p ? String(p.id || p.className || p.tagName) : '',
          hits,
          err: LASTERR,
        },
      }).catch(() => { /* 回传失败不影响使用 */ });
    } catch (e) { /* 忽略 */ }
  }

  function tickNav() {
    if (location.href === lastHref) return;
    lastHref = location.href;
    navCount++;
    removeBtn();
    // 站内换 P / 换集：视频马上要起播，先安静 3 秒再插按钮，别和起播抢那一帧
    if (navCount > 1) quietUntil = Date.now() + 3000;
    setTimeout(safePlace, 400);
    setTimeout(safePlace, 1500);
    setTimeout(() => { if (videoUrl()) ping(); }, 2600);
  }

  /* ───────────── 提交与进度 ───────────── */

  const job = { tid: 0, mode: '', timer: 0, pulled: 0 };

  function stopPoll() { clearInterval(job.timer); job.timer = 0; }

  function pull(id) {
    const a = document.createElement('a');
    a.href = getBase() + '/api/file/' + id;
    a.setAttribute('download', '');
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 1000);
  }

  function fmtB(n) {
    if (!n) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (v < 10 ? v.toFixed(v < 1 ? 2 : 1) : Math.round(v)) + ' ' + u[i];
  }
  function fmtEta(s) {
    if (s == null || s < 0) return '';
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    const t = h ? h + ':' + String(m).padStart(2, '0') : String(m);
    return '剩 ' + t + ':' + String(x).padStart(2, '0');
  }

  function submit() {
    const url = videoUrl();
    if (!url) { toast('没识别到视频地址', true); return; }
    const s = site();
    const mode = s === 'bilibili' ? 'direct' : 'queue';

    if (btn) { btn.disabled = true; btn.style.opacity = '.65'; btn.querySelector('span').textContent = '提交中'; }
    showPill('正在提交给 N100…');

    // YouTube 用菜单里设的画质；B 站和 X 都由服务端按平台最优处理（X 只认最高码率）
    const quality = s === 'youtube' ? getQuality() : 'best';
    let payload = { urls: url, quality, mode };
    let viaDirect = '';
    if (s === 'twitter') {
      // X 优先走页面里抓到的媒体直链：敏感推文 / 私密账号 / 已删除的推文，
      // N100 匿名接口只能读到空壳，而直链是公开 CDN，谁都能下
      const pack = xPackForPage();
      if (pack) {
        payload = { urls: pack.urls.join('\n'), quality, mode, hints: pack.hints };
        viaDirect = pack.n > 1 ? `${pack.n} 段直链` : '直链';
      }
    }
    req('/api/tasks', { method: 'POST', body: payload })
      .then(r => {
        if (!r || !Array.isArray(r.added)) {
          throw new Error('N100 服务端没返回任务列表（服务可能是旧版本，重启一下 N100 上的下载服务）');
        }
        const it = r.added[0] || null;
        if (!it) {
          // 服务端明确跳过（推文里没视频 / 内容读不到 / 私密账号…）——把原因原样说出来，
          // 别再甩一句"没有返回任务号"，那是我们自己的锅
          const why = (r.skipped && r.skipped[0] && r.skipped[0].reason)
            || (r.warnings && r.warnings[0])
            || '没有可下载的内容';
          const skipErr = new Error(why);
          skipErr.skip = 1;
          throw skipErr;
        }
        job.tid = it.id; job.mode = mode; job.pulled = 0;

        // 链接之前下过、文件还在 N100 上
        if (it.dup && it.state === 'done') {
          if (mode === 'direct') { pull(it.id); showPill('已在 N100 上，正在存到本机…'); }
          else showPill('这个视频已经在 N100 上：',
            aLink(getBase() + '/watch/' + it.id, '打开预览/取回', true));
          return;
        }
        toast(mode === 'direct' ? '已提交 · 下完自动存到本机' : '已加入 N100 队列');
        if (viaDirect) toast('已从页面抓到' + viaDirect + '，直接交给 N100 下载（不经过 X 接口）');
        if (r.added.length > 1) {
          toast('这条推文有 ' + r.added.length + ' 段媒体，已全部排队');
        }
        if (r.warnings && r.warnings.length) toast(r.warnings[0], true);
        startPoll();
      })
      .catch(e => {
        showPill((e.skip ? 'N100：' : '提交失败：') + e.message);
        toast(e.message, true);
      })
      .finally(() => { if (btn) { btn.disabled = false; btn.style.opacity = ''; btn.querySelector('span').textContent = '下载'; } });
  }

  function startPoll() {
    stopPoll();
    pollOnce();
    job.timer = setInterval(pollOnce, 2000);
  }

  function pollOnce() {
    if (!job.tid) { stopPoll(); return; }
    req('/api/task/' + job.tid).then(t => {
      const pct = (t.pct || 0).toFixed(0);
      if (t.state === 'queued') {
        showPill(bText('N100'), ' 排队中…');
      } else if (t.state === 'extracting') {
        showPill(bText('N100'), ' 解析中…');
      } else if (t.state === 'downloading') {
        showPill(bText('N100'), ' 下载中 ' + pct + '% · ' + fmtB(t.done_bytes) + '/' + fmtB(t.total_bytes) +
          ' · ' + (t.speed ? fmtB(t.speed) + '/s' : '—') + ' · ' + fmtEta(t.eta));
      } else if (t.state === 'merging') {
        showPill(bText('N100'), ' 合并音视频中…');
      } else if (t.state === 'done') {
        stopPoll();
        if (job.mode === 'direct') {
          if (!job.pulled) { job.pulled = 1; pull(job.tid); toast('已下好，正在存到本机'); }
          const again = aLink('#', '点这里再存一次');
          again.onclick = ev => { ev.preventDefault(); pull(job.tid); };
          showPill(bText('N100'), ' 已下好 · 正在存到本机',
            n('span', { text: '（没反应就 ', style: 'color:#6f7681' }),
            again,
            n('span', { text: '）', style: 'color:#6f7681' }));
        } else {
          showPill(bText('N100'), ' 已下好（' + fmtB(t.size) + '）',
            aLink(getBase() + '/watch/' + job.tid, '预览/取回', true));
        }
      } else if (t.state === 'failed') {
        stopPoll();
        showPill(bText('N100'), ' 失败：' + (t.error || '原因不明'));
      } else if (t.state === 'canceled' || t.state === 'removed') {
        stopPoll();
        showPill(bText('N100'), ' 任务已' + (t.state === 'canceled' ? '取消' : '删除'));
      }
    }).catch(e => { stopPoll(); showPill('查询进度失败：' + e.message); });
  }

  /* ───────────── 菜单 ───────────── */

  if (typeof GM_registerMenuCommand === 'function') {
    GM_registerMenuCommand('设置 N100 地址（当前：' + peekBase() + '）', () => {
      const v = prompt('N100 下载站地址', peekBase());
      if (v && /^https?:\/\//.test(v.trim())) {
        store.set(K_BASE, v.trim().replace(/\/+$/, ''));
        toast('已保存：' + getBase());
      } else if (v) {
        toast('地址要以 http:// 开头', true);
      }
    });
    GM_registerMenuCommand('设置 YouTube 画质（当前：' + getQuality() + '）', () => {
      const v = prompt('YouTube 画质：best / 4k / 720 / audio', getQuality());
      if (v && QUALITIES.includes(v.trim())) {
        store.set(K_QUALITY, v.trim());
        toast('YouTube 画质：' + getQuality());
      } else if (v) {
        toast('只能填 best / 4k / 720 / audio', true);
      }
    });
    GM_registerMenuCommand('试一下能不能连上 N100', () => {
      req('/api/state').then(() => toast('连得上：' + getBase()))
        .catch(e => toast(e.message, true));
    });
    GM_registerMenuCommand('临时关闭下载按钮（排查页面卡顿 / 黑屏用）', () => {
      const off = String(store.get(K_OFF, '0')) === '1';
      store.set(K_OFF, off ? '0' : '1');
      if (off) { place(); toast('已恢复：按钮回来了（没出现就刷新一下）'); }
      else { removeBtn(); toast('已关闭：按钮已移除，刷新页面也不会再出现'); }
    });
    GM_registerMenuCommand('诊断当前页面（结果打到控制台）', () => {
      const hosts = site() === 'youtube' ? YT_HOSTS
                  : site() === 'twitter' ? X_HOSTS : BILI_HOSTS;
      const hit = hosts.filter(h => {
        try { return !!document.querySelector(h.sel); } catch (e) { return false; }
      }).map(h => h.sel);
      const el = document.getElementById(BTN_ID);
      const info = {
        版本: VER,
        站点: site() || '(不支持的站点)',
        识别的视频地址: videoUrl() || '(空)',
        按钮是否在页面上: !!el,
        按钮可见: el ? onScreen(el) : false,
        按钮宿主: el ? (el.dataset.host || '-') : '-',
        按钮位置: el && el.parentElement ? (el.parentElement.id || el.parentElement.className) : '-',
        命中的挂载点: hit.length ? hit : '(一个都没命中，应该已退到右下角悬浮按钮)',
        注入报错: LASTERR || '(无)',
        按钮开关: String(store.get(K_OFF, '0')) === '1' ? '已关闭' : '开启',
        X捕获: site() === 'twitter'
          ? `${XMED.size} 条推文已记录 / 当前推文${xPackForPage() ? '已抓到直链' : '还没抓到'} / ` +
            (X_HOOK_IN_PAGE ? '页面上下文（有效）' : '沙箱（捕获无效，会退回推文链接）')
          : '-',
        N100地址: getBase(),
      };
      console.log('[N100] 诊断', info);
      ping();
      toast('诊断已打到控制台，并回传给 N100 了');
    });
  }

  /* ───────────── 启动 ───────────── */

  // 挂钩必须抢在 X 自己的脚本之前（脚本是 document-start 注入的）：
  // 晚一步，X 的内部闭包早就把原生 fetch 拿走了，再改 window.fetch 也抓不到它的请求。
  // B 站 / YouTube 根本不需要这套，一步都不多做，别给它们添开销。
  if (site() === 'twitter') installXHooks();

  try { console.log('[N100] 下载脚本 v' + VER + ' 已加载：' + location.href); } catch (e) { /* 忽略 */ }

  function bootUi() {
    // 首次插入晚一点：等播放器自己初始化完，避开起播那几秒的重排
    setTimeout(safePlace, 600);
    if (videoUrl()) setTimeout(ping, 2400);
    // 2s 一轮，且后台标签页直接不跑；常规上每轮只做 getElementById + isConnected，不量几何
    setInterval(() => {
      if (document.hidden) return;
      tickNav();
      safePlace();
    }, 2000);
    // YouTube 的 SPA 事件，比轮询更快一点
    document.addEventListener('yt-navigate-finish', () => { lastHref = ''; tickNav(); });
    window.addEventListener('popstate', () => { lastHref = ''; tickNav(); });
    // 页面隐藏时省点开销，回来补一次
    document.addEventListener('visibilitychange', () => { if (!document.hidden) safePlace(); });
  }

  // document-start 时还没有 body，所有 DOM 相关的活都要等文档就绪再干
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootUi);
  } else {
    bootUi();
  }

  // 排查出口：X 页面上控制台里 `__N100_X__.xPackForPage()` 就能看到"这条推文抓到什么直链"
  try {
    if (typeof unsafeWindow !== 'undefined' && unsafeWindow) {
      unsafeWindow.__N100_X__ = {
        XMED, XRAW, xPackForPage, xMedFromDom, walkTweetJson, ingestTweet,
        classifyMediaUrl, pickBestVariant, curStatusId,
      };
    }
  } catch (e) { /* 忽略 */ }
})();
