# N100 离线下载站

网页版的 YouTube / B 站下载器，跑在 N100 上。**你电脑关机也能继续下**，下好的文件先存 N100，
等你电脑开机后从网页点"下载取回"，文件收完自动从 N100 删掉。

> 本文档里的 `192.168.1.50` 只是**示例地址**，替换成你自己 N100 的局域网 IP 即可。
> 服务默认监听 8300 端口，没有内置任何账号/密钥，cookie 需要你自己上传（见下文）。

- 访问地址：`http://192.168.1.50:8300`
- 访问口令：**无**（`APP_PASSWORD` 为空即免登录，局域网内谁都能访问）
- 数据目录：`/srv/ytdl/data`（`cookies.txt` / `bilibili_cookies.txt` / `tasks.db` / `downloads/`）

## 支持平台

| 平台 | 最高清晰度 | 走什么线路 | 登录态 |
|---|---|---|---|
| YouTube | 4K（视源而定） | mihomo 代理 | `cookies.txt` |
| B 站 | **1080P**（qn=80，不含大会员档） | **直连**（实测 30–40 MB/s） | `bilibili_cookies.txt`，可选 |
| X（原 Twitter） | **源端最高**（实测竖屏 1080×1920 / 10 Mbps 档） | mihomo 代理（直连 x.com 是 000） | `twitter_cookies.txt`，公开推文不需要 |

B 站说明：
- 大会员独享的 1080P 高码率 / 1080P60 / 4K / HDR / 杜比 / 8K 一律不取，`format` 里直接按 `height<=1080` 硬过滤，
  即使换成大会员账号也不会越界
- 同分辨率强制 H.264：B 站自研编码在同分辨率下码率只有 H.264 的 1/4（1080P 实测 604k vs 2629k），压过头了
- **分P / 合集**默认只下当前这一 P；勾「整个合集」会在提交时展开成多条独立任务（每 P 独立进度/预览/取回/删除）
- **番剧**（`/bangumi/play/ssXXXX`）默认只下第 1 集；免费集能下到 1080P，大会员集会在页面标失败原因
- B 站任务同时只跑 1 个（同 IP 并发高了容易 412），不占 YouTube 的并发槽位

X 说明：
- **不做高度过滤**：竖屏视频的 `height` 是长边（1080×1920 记作 height=1920），按 `height<=1080` 过滤会把 1080P
  直接降到 480×852（实测踩过），所以 X 一律取 `b`（音视频合成的最高档）。可选画质只有「最高」和「仅音频」两档
- **一条推文多段媒体**（X 只允许 1 个视频，但可以带多个 GIF）会在提交时探测并**拆成多条独立任务**，
  每段有自己的进度 / 预览 / 取回 / 删除。探测走 X 的轻量 JSON 接口（~1.3 秒），失败才退回 yt-dlp（~4 秒）
- 纯文字 / 纯图片推文会直接提示「这条推文里没有视频，已跳过」，不会排一个注定失败的任务
- 文件名取推文正文前 40 字 + 推文 ID：正文最多 280 字，原样当文件名会超 255 字节报 `File name too long`
- 公开推文匿名就能取；敏感内容 / 私密账号 / 被风控时，匿名接口只回一个 `TweetTombstone` 空壳
  （`mediaDetails` 是 `null`，和"真的没视频"长得一模一样）。这种情况现在有两条路：
  1. **油猴脚本**（推荐）：页面里已经带着登录态，脚本直接把真实媒体直链交给 N100，完全绕开 X 接口，见下节
  2. 设置里上传 `twitter_cookies.txt`（`target=twitter`），让 yt-dlp 带登录态去读
- 直链下载（`video.twimg.com/.../vid/avc1/1080x1920/xx.mp4?tag=16`）实测**不带 cookies、不带 Referer 也是 200**，
  所以这条路对敏感推文同样有效；`platform_of()` 把 `video.twimg.com` 也认成 `twitter`，画质策略照旧（不过滤高度）


## 结构

```
/srv/ytdl/
├── docker-compose.yml
├── Dockerfile
├── server.py          FastAPI：队列 + SQLite + SSE + Range 下载
├── static/index.html  主页面（单文件，原生 JS，手机可用）
├── static/watch.html  独立预览页（新窗口打开，带"保存到本机/删除"）
├── static/n100-download.user.js  油猴脚本（B站/YouTube 页内下载按钮）
└── data/
    ├── cookies.txt            YouTube 登录态（网页"设置"里可更新）
    ├── bilibili_cookies.txt   B 站登录态（SESSDATA，可选）
    ├── tasks.db               任务表（重启不丢）
    ├── settings.json          并发/代理/画质等
    └── downloads/             下载的视频
```

本机（Windows）还有一个配套脚本 `_bili_cookie.py`：从 Edge 导出 B 站登录态，
自动处理"库被浏览器锁住"的情况，导出后 scp 到 N100 即可。

两个容器：

| 容器 | 作用 |
|---|---|
| `ytdl-app` | 主服务，8300 端口；镜像内自带 ffmpeg + node 22 + yt-dlp |
| `ytdl-pot` | bgutil PO token 服务端（4416），防止机房 IP 被 YouTube 判机器人 |

代理走 N100 上的 mihomo：容器里用 `host.docker.internal:7890` 访问宿主机的 `127.0.0.1:7890`。

## 常用命令

```bash
cd /srv/ytdl

docker compose ps                  # 状态
docker compose logs -f app         # 实时日志
docker compose restart app         # 重启
docker compose up -d --build       # 改完代码重新构建
docker compose down                # 停止（data 不会被删）
```

改访问口令：编辑 `docker-compose.yml` 里的 `APP_PASSWORD`，然后 `docker compose up -d`。

## 用法

1. 浏览器打开 `http://192.168.1.50:8300`（不需要口令）
2. 文本框里一行一个链接，可整段粘贴。**YouTube 和 B 站可以混着贴**，每条前面会标出平台徽标
3. 画质下拉会跟着链接自动切：贴的全是 B 站链接时只给「最高（1080P）/ 720P / 仅音频」，
   混合或 YouTube 链接时给「最高≤1080p / 4K / 720p / 仅音频」（B 站的那几条会自动压到 1080P）
4. B 站分P/番剧想整季下，勾「整个合集」——提交时会展开成一条条 P，各自独立
5. 提交有两个按钮，按需要选：
   - **加入队列**：N100 后台排队下载，存到 N100，之后随时点「取回」（电脑关机也能下）
   - **直接下到本机**：插到队列最前面优先下，下完**自动推到这台电脑**（浏览器下载目录），
     N100 上收完倒计时即删，不长期占空间。列表里带 `直下` 绿色徽标
   > 勾了「整个合集」再点直下，会连续触发多个文件下载，Chrome 会问一次"是否允许下载多个文件"，点允许即可
6. 下完在列表里点**"预览"**会**新开一个浏览器窗口**（`/watch/<任务ID>`）播放，不占本地磁盘、
   也不会触发删除；窗口里带「保存到本机」「删除」按钮
7. 想留到自己电脑上点"取回"，浏览器会存到本地下载目录
8. 文件被浏览器**完整**收完后开始倒计时（默认 60 秒），到点自动删；期间可点"撤销"
9. 不想留就点"删除"，直接删 N100 上的文件，不下载

> 中断的下载会保留 `.part` 分片，重试时从断点续传。
> 任务列表按**新→旧**排，最新提交的在最上面（后端 `ORDER BY id DESC`，前端每次刷新按顺序插位，不会追到末尾）。

## 油猴脚本（B 站 / YouTube / X 视频页直接下载）

脚本文件：`static/n100-download.user.js`，站点上也能直接下：**`http://192.168.1.50:8300/n100.user.js`**

装法：先装 Tampermonkey（或 Violentmonkey）→ 打开上面那个地址 → 油猴会弹安装页 → 安装。
脚本更新也一样，重开一次地址即可覆盖升级。

脚本里**没有写死任何 IP**：默认连接地址取自"你安装脚本的那个来源地址"，所以从哪台 N100 装的就连哪台。
换地址后可以在油猴菜单里改，或者干脆重新从新地址装一遍。
（个别管理器拿不到安装来源时会兜底问一次地址，填一次以后就记住了。）

行为：

| 站点 | 按钮位置 | 默认模式 |
|---|---|---|
| YouTube（watch / shorts） | **播放器正下方**（首屏可见）；找不到时退到点赞-分享那排，再不行退右下角悬浮 | **加入队列**（存 N100，之后从网页取回） |
| B 站（video / 番剧 / 合集） | 视频下方点赞-投币-收藏那一排，最后 | **直接下到本机**（下完自动推给浏览器，N100 上倒计时删） |
| X（`/用户名/status/ID`） | 推文下方「回复·转推·喜欢·收藏·分享」那一排的最后 | **加入队列**；优先用页面里抓到的媒体直链（敏感推文也能下），多段媒体会全部排队 |

- 点一下就在右下角出现进度条：排队 → 解析 → 下载中（百分比 / 速度 / 剩余）→ 已下好
- B 站直下：下完自动触发浏览器保存；如果 Chrome 拦了“自动下载”，状态条里有「点这里再存一次」
- YouTube 队列：下完后状态条给「预览/取回」，直接跳 `/watch/<任务ID>`
- B 站分 P 页会带上 `?p=N`，下的是当前这一 P；不需要的 `spm_id_from` 之类跟踪串会自动去掉
- X 只在**推文详情页**挂按钮（时间线 / 主页 / 搜索页不挂，避免一次冒出几十个按钮），`/i/web/status/ID` 也能认

### X 为什么能下敏感推文（v1.4.0 新增的页面内捕获）

N100 走的是 X 的**匿名**接口，敏感内容 / 私密账号 / 已删除的推文只会回一个 `TweetTombstone` 空壳，
拿不到任何媒体（会提示"这条推文匿名读不到"）。这个脚本用的是另一条路——**猫抓式网络层捕获**：

1. 脚本改成 `document-start` 注入，并用 `unsafeWindow` 改写**页面自己**的 `fetch` / `XHR`
   （晚一步就没用：X 的闭包早把原生 `fetch` 拿走了）
2. 只挑 `graphql` / `timeline` / `tweet-result` 的 JSON，且必须含 `video_info` 才解析；
   从里面取 `video_info.variants`——这就是页面（带登录态）拿到的真实媒体列表
3. 只收两类 URL：
   - `.../vid/avc1/<W>x<H>/xxx.mp4?tag=16` —— **渐进式 mp4**，音画一体的完整文件（首选，按分辨率排序取最大）
   - `.../<id>/pl/<token>.m3u8` —— **master 清单**（只在没有 mp4 时用；`pl/avc1/720x1280/` 这种子清单是纯视频轨，
     下了必无声，直接丢掉）
4. 按「推文 ID → 媒体列表」缓存（最多 200 条），点下载时按当前 URL 里的状态 ID 取；
   万一 JSON 没抓到，再退回 DOM：用 `<video poster>` 路径里的视频 ID 反查网络层记下的直链
5. 抓到了就直接把直链 + 文件名提示（`@作者 正文前 60 字 [推文ID]`）POST 给 N100；
   **抓不到才退回原来的"提交推文链接"**（那条路才需要 cookies）

副作用：这条路完全不经过 X 的接口，所以没有匿名风控，也不用等 yt-dlp 解析，通常 1 秒内就进了队列。
代价是脚本必须拿到页面上下文（`@grant unsafeWindow`），并且必须在 `document-start` 运行。
排查：X 页面上控制台执行 `__N100_X__.xPackForPage()`，能看到当前推文抓到了哪些直链。
- 工具栏选择器被改版打掉时，会自动退成右下角的悬浮「下载」按钮，不会失效
- 按钮挂到容器里之后会检查**是否真的可见**，容器被隐藏/还没渲染好就改用悬浮按钮（否则会出现"脚本在跑但没有按钮"）
- 全部 DOM 节点用 DOM API 构建，**不用 `innerHTML`** —— YouTube / B 站都开了 Trusted Types，
  赋 `innerHTML` 会被浏览器直接拦掉并抛异常，按钮根本造不出来（v1.1.x 的坑）

油猴菜单里可以改：

- **N100 地址**（默认自动取"你安装脚本的那个地址"，不用手填；换了 IP / 端口在这里改）
- **YouTube 画质**（`best` ≤1080p / `4k` / `720` / `audio` 仅音频）
- **试一下能不能连上 N100**
- **临时关闭下载按钮**（排查页面卡顿 / 起播黑屏时做 A/B 对比用）
- **诊断当前页面**（把版本、识别到的视频地址、按钮挂在哪个容器、命中的选择器打到控制台）

看不到按钮时按顺序查：

1. 是在**装了油猴的那个浏览器**里打开的吗（内置预览窗口 / 无扩展的环境跑不了脚本）
2. 油猴扩展里「N100 下载」是否**已启用**；Chrome 需要在 `chrome://extensions` 打开右上角**开发者模式**，
   否则用户脚本不执行
3. 脚本装完/更新完要**刷新一次页面**
4. 还不行就 F12 打开 Console，看有没有 `[N100] 下载脚本 v1.3.0 已加载` —— 没有说明脚本压根没跑，
   有日志但没按钮，用油猴菜单的「诊断当前页面」（结果打控制台，同时回传 N100：
   `curl http://192.168.1.50:8300/api/userscript/pings`）
5. Console 里出现 `This document requires 'TrustedHTML' assignment` 说明装的还是 1.1.x 老版本，
   重新打开脚本地址覆盖安装即可

注意：

- 只在 N100 **免口令**模式下可用（跨域带不了登录 cookie）；设了口令会提示“用不了”
- 脚本用 `GM_xmlhttpRequest` 发请求，不依赖浏览器 CORS；同时服务端也放行了
  `bilibili.com` / `youtube.com` 及其子域的跨域请求（`CORSMiddleware`，其它站点仍被拒），
  所以换了浏览器/管理器、退化成 `fetch` 也能用
- 安装时 Tampermonkey 可能问是否允许访问 `192.168.1.50`，点允许

## 网页预览为什么不触发删除

预览和取回是**两个独立端点**：

| 端点 | 用途 | Content-Disposition | 记"取回" |
|---|---|---|---|
| `/watch/{id}` | 独立预览页（新窗口打开） | — | 不记 |
| `/api/preview/{id}` | 页面里 `<video>`/`<audio>` 的流 | `inline` | **不记** |
| `/api/file/{id}` | 下载到本地 | `attachment` | 记，覆盖 100% 才倒计时删除 |
| `/api/task/{id}` | 单条任务详情（预览页查标题/大小/倒计时） | — | 不记 |

如果预览走 `/api/file`，浏览器播一遍就等于"完整收完整个文件"，会被判定成已取回、60 秒后删掉——
所以必须分开。预览支持 Range，拖进度条、断线重连都正常。

想让手机直接跳到某个文件的预览页：`http://192.168.1.50:8300/?play=<任务ID>`（会自动跳到 `/watch/<任务ID>`）。

## 取回后自动删除的原理

后端边发边记账：把客户端实际收完的字节区间合并成覆盖度，**只有覆盖到 100% 才认定取回成功**。
浏览器中途取消、断网都不会误删。真删之前还有 60 秒缓冲，可以撤销。

## cookies 更新（大概率会用到）

**N100 读不到你浏览器里的 cookie**，这是浏览器的设计，不是配置问题：

- Chrome 从 v80 起把 cookie 的 `encrypted_value` 用 **DPAPI（绑 Windows 账户）+ AES-GCM** 加密，
  换台机器、换个用户就解不开
- Chrome 开着的时候 `Network\Cookies` 这个文件被独占锁住，连复制都可能失败
- 更根本的是：N100 是另一台 Linux 机器，本来就没有你浏览器的数据

所以只能"从浏览器导出 → 传到 N100"。两种方式，任选：

### 方式一：扩展导出（推荐，一次搞定）

1. Chrome 装扩展 **Get cookies.txt LOCALLY**（开源，纯本地处理，不上传任何数据）
2. 打开目标站点并确认真的是已登录状态 → 点扩展图标 → Export
3. N100 网页右上角「设置」→ 选文件 → 保存

### 方式二：F12 复制 Cookie 请求头（什么都不用装）

1. 目标站点按 **F12** → **Network** → 刷新页面 → 点任意一条请求
2. 在 **Request Headers** 里找到 `Cookie:` 那一行，复制整行（值很长，要复制全）
3. N100「设置」→ 展开对应平台的「没有扩展？直接粘贴 Cookie」→ 粘贴 → 保存

服务端会自动转成 Netscape 格式（YouTube 写 `.youtube.com` + `.google.com` 两份，
X 写 `.x.com` + `.twitter.com`），并校验关键票据在不在：

| 平台 | 必须出现（任一个即可） |
|---|---|
| YouTube | `SID` / `HSID` / `SSID` / `APISID` / `SAPISID` / `__Secure-*PSID` / `LOGIN_INFO` |
| B 站 | `SESSDATA` |
| X | `auth_token` 或 `ct0` |

⚠️ **别用控制台 `document.cookie`** —— HttpOnly 的那几个（正好是最关键的登录票据）JS 读不到，
粘进去会被服务端直接拒绝。

命令行也能传（注意用 `--form-string`，普通 `-F` 会把值里的 `;` 当参数分隔符）：

```bash
# 粘贴式
curl -X POST "http://192.168.1.50:8300/api/cookies?target=twitter" \
  --form-string "text=auth_token=xxx; ct0=yyy"
# 文件式
curl -X POST "http://192.168.1.50:8300/api/cookies?target=youtube" -F "file=@cookies.txt"
# 清除某个平台
curl -X DELETE "http://192.168.1.50:8300/api/cookies?target=twitter"
```

网页上每个平台旁边也有「清除」按钮，换账号时用得上。

也可以绕过网页直接推文件过去：

```bash
scp -i ~/.ssh/n100_ed25519 edge_cookies.txt root@192.168.1.50:/srv/ytdl/data/cookies.txt
docker compose restart app
```

YouTube 会因为"机房 IP + 无登录态"要求验证。当前 `data/cookies.txt` 是从本机 Edge 导出的，
一般能撑几周到一两个月。失效的表现是任务失败、提示"YouTube 要求验证"。

导出 cookies 的现成文件在本机：`C:\Users\Administrator\.workbuddy\binaries\ytdlp\edge_cookies.txt`
（超过 24h 的话先跑一次 `ytdl` 命令会自动刷新）。

### B 站 cookies（可选，但建议有）

B 站匿名也能下 1080P（实测），登录态主要解决两类问题：部分视频匿名只给 480P/720P、720P60 需要登录。
测试用水号**不是大会员**（`vipStatus=0`），所以有没有 cookie 上限都是 1080P。

导出（在 Windows 本机跑，会自动找有登录态的 profile）：

```bash
python n100-ytdl/_bili_cookie.py            # 生成 ~/.workbuddy/binaries/ytdlp/bilibili_cookies.txt
scp -i ~/.ssh/n100_ed25519 ~/.workbuddy/binaries/ytdlp/bilibili_cookies.txt \
  root@192.168.1.50:/srv/ytdl/data/bilibili_cookies.txt
```

- **必须和 YouTube 的 cookie 分文件**：YouTube 那份每 24h 自动刷新，混在一起会被覆盖
- 这个脚本的坑：Edge 开着时 Cookies 库被独占锁，sqlite 直接打开报
  `unable to open database file`（看着像"没登录"，其实是锁住了），所以它自动走 `robocopy /B` 备份模式读镜像
- 网页"设置"里也能直接上传，接口是 `POST /api/cookies?target=bilibili`
- `SESSDATA` 有效期约几个月，失效表现是 B 站任务报"需要登录/风控"

## 排错

- **页面打不开**：`docker compose ps` 看 `ytdl-app` 是否 Up；`ss -lntp | grep 8300`
- **任务全失败，提示要验证**：先点设置里的"自检"，看 YouTube 是否 200、出口 IP 是否变了
- **提示"该清晰度不可用"**：源端没有这个分辨率，换"最高画质"重试
- **磁盘满**：设置里调高"最低剩余"，或删旧文件；`df -h /srv`
- **代理换了端口**：设置里改"代理"地址，保存即生效，不用重启
- **player_client 失效**（YouTube 又改策略）：设置里把 `web_embedded` 换成 `tv` 或 `web_embedded,tv`
- **B 站报 412**：限流，降并发等几分钟；**报 -352/-404**：风控/地区限制/需要大会员
- **B 站下了整季/整个合集**：说明 `playlist_items` 没生效，检查 `server.py` 里 `_opts()` 的 bilibili 分支
- **JS 挑战解不出**（日志里出现 ejs / nsig 相关报错）：镜像里带的是本地 `yt-dlp-ejs 0.8.0`，比拉远程快一倍；
  如果 YouTube 改了挑战，在 `docker-compose.yml` 的 `app.environment` 里加一行 `EJS_REMOTE: "1"`，
  `docker compose up -d` 后会改用 GitHub 上的最新求解脚本（要求代理能通 GitHub）

## 备份与迁移

整个站点就是 `/srv/ytdl`，`docker compose down` 后打包目录拷到别的机器，
装好 docker 再 `docker compose up -d` 就能跑（镜像重新 build 一次）。
`data/downloads` 里是暂存文件，不必备份。
