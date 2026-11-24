# Comment receipts and bounded responses

`send_video_comment` persists a server-accepted rpid/oid receipt separately from
the legacy dispatch attempt. Accepted never means publicly visible. A separate
worker reads exactly one tracked root per ten minutes, at most 20 first-page
children; it never adopts manual comments or discovers unrelated threads.
Roots rotate fairly even when a read fails and expire from polling after seven
days. A read failure leaves previous visibility unchanged.

Latest read diagnostics are stored separately in `comment_read_checks`. When
anonymous reading returns an exact match, no logged-in thread read is made.
Only explicit missing-comment business code 12006 or a returned root that fails
exact matching permits one extra read of the same oid/rpid using the publishing
account. This yields observations `account_only_readable` (account matched,
anonymous did not) or `both_not_read` (both returned missing/mismatching data),
without attributing a moderation cause. HTTP errors, risk-control business codes,
timeouts, invalid responses and authentication failures yield `check_blocked`;
they never mean a comment is hidden and never erase last conclusive visibility.
Export fields `read_status`, `anonymous_state`, `anonymous_code`, `account_state`,
`account_code`, `read_checked_at` report the latest check even when visibility
retains an older observation. No raw exception text or user identity is exported.

The `public_confirmed` internal state means the exact comment was readable in an anonymous detail request; it does not prove discovery in normal comment lists. Confirmation requires an anonymous GET and exact rpid, oid, root, parent,
author UID and message matching. The UID comes from the currently authenticated
account, not the logical `account_id`. Only direct responses to a tracked own
root are eligible. Nested replies to other people, own messages, known bot names,
and messages older than a day are excluded. One attempt per root/person ever,
four per New York day, one-hour cooldown. Durable intents prevent retries after
crashes, including model failures and uncertain sends. Model judgement may remain
silent; bot-name filtering cannot detect every automated account.

`comment_replies_enabled` and `comments_enabled` must both allow posting, the
watcher must not be stopping or watching the priority live stream, and the daily
reply ledger must allow an atomic reservation. Root, target and authenticated UID
are checked again immediately before POST. Model calls use the existing persona,
configured decider and `comment-reply` cost purpose; generated output is <=120
characters. This is a bounded first-page source: later pages and responses to the
bot's nested replies are deliberately not monitored.

Platform response codes, a fixed allowlist of public success/review toasts and
small numeric review states are retained; raw response bodies are not. The
review number is reported as a platform state, never interpreted as visibility.
The activity export includes `video_comment_attempts` receipt visibility and
`comment_reply_attempts` with incoming text, answer, IDs and delivery state. Public
exports remove credential-like strings. After 30 days, incoming/answer/receipt
text is cleared while identity tombstones and cost links remain.

## Interface evidence (2026-11-06)

Primary working source reviewed:
https://github.com/inorilzy/bilibili-api/blob/main/bilibili_api/comment.py
(`Comment.get_sub_comments`, `send_comment`); API semantics also documented at
https://github.com/Chilfish/bilibili-API/blob/master/docs/comment/action.md .
This is a community website API, not an official creator API guarantee.

Live, read-only checks from the deployed host succeeded without any POST:

- Anonymous `GET /x/v2/reply/reply?type=1&oid=2&root=319418677841&ps=20&pn=1`
  returned code 0, the requested root, and zero children.
- Anonymous GET with root 495059 returned code 0 and real children. The first
  eight had `oid=2`, `root=495059`, `parent=495059`, integer `ctime` and `rpid`,
  confirming the direct-child relationship used by the policy.
- Authenticated `/x/web-interface/nav` confirmed the current account UID. No
  cookie, credential, user content or raw upstream payload was exported.

No forced public send was performed. Historical comments without recorded rpid
receipts remain unverified and cannot become response targets. Natural future
sends must produce receipts before their threads can enter this workflow.

## 生产历史回查（纽约时间 2026-11-06）

本次在迁移后的生产 watcher 启用了每月60、每日硬限6记账美元。按30天月，日均2美元中直播保障0.99、评论回复保障及单日上限0.15，视频可用0.86。直播理解的最短间隔60秒，弹幕是否发出仍由新鲜范围内的内容、判断和发送冷却共同决定。调用未结费用按请求的纽约发生日期计入当日预算，跨日及跨月未结责任继续纳入月上限。

首批恢复补丁曾因启动依赖不完整退出，已修正，并增加通过服务入口启动并进入观看循环的回归测试。修正后实际自主选择 `BV1onxb6rEeE`，第一段写入音画观察，最长覆盖到28.5秒。提交和回查功能验收使用测试数据及真实只读接口，没有为验收发送公开测试消息。

旧库13次受理按纽约日分别为11月1日1次、2日至4日各4次。账户登录有效、等级3。对以下三个稿件用当前账号按时间翻页至空页，另查置顶并比对作者与完整正文。均无匹配。三稿均显示输入可用、非只读、web_selection=false、无折叠；缺失的精选或审核字段无法作为结论。

| 稿件 | 普通顶层数 | 同稿件可匿名读取的对照楼层 |
| --- | ---: | ---: |
| BV1b3nK6QEuE | 17 | 322951959008 |
| BV112so6eEpJ | 15 | 322632959280 |
| BV1NPxh6wEor | 19 | 322809757632 |

对照楼层经匿名详情 GET 返回匹配 rpid 和状态0。三条历史受理记录在此次普通列表中缺失，旧程序未保存 rpid 或审核提示，无法追溯具体处置。对照 [biliup 的视频评论与回复实现](https://github.com/biliup/biliup/blob/master/crates/biliup/src/uploader/bilibili.rs) 和 [bilibili-api 的顶层及楼中楼发送实现](https://github.com/inorilzy/bilibili-api/blob/main/bilibili_api/comment.py)，双方都用稿件 aid、账号凭据和 CSRF 向网站评论接口提交，定向回复另指定根楼层与父楼层。项目源码佐证请求结构，公开展示须从读端核验。
