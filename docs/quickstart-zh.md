# paperdaily API v1 — 快速上手

paperdaily 的 v1 API 让你用一把 bearer key 程序化地访问每日推荐、论文详情、综合查询，以及修改自己的兴趣画像。下面这份文档从"拿到第一把 key"到"接入 Claude agent"，按使用顺序展开。

## 概述

- **Base URL**: `https://www.paperdaily.org`
- **认证**: HTTP Header `Authorization: Bearer pd_live_…`
- **数据格式**: 请求 / 响应均为 JSON，邮件渲染端点返回 `text/html`
- **OpenAPI 规范**: <https://www.paperdaily.org/api/v1/openapi.json>
- **Swagger UI（在线调试）**: <https://www.paperdaily.org/api/v1/docs>

调用流程：

```
登录 paperdaily 网站
        ↓
在 /account 页面创建 API key（明文一次性显示）
        ↓
带 Authorization: Bearer pd_live_… 请求 /api/v1/*
```

## 认证

### 创建 key

1. 浏览器登录 [paperdaily](https://www.paperdaily.org)
2. 打开 [账号设置](https://www.paperdaily.org/account)
3. 找到 **API Keys** 卡片 → 点击 **+ 新建 key**
4. 填一个识别用的名称（可选，比如 `本地脚本` / `cron 机器`）
5. 勾选所需 [Scope 权限](#scope-权限)
6. 提交后弹出明文 `pd_live_…`，**只显示一次**

> **重要**：服务端只保存 sha256 哈希。明文丢了只能撤销旧 key 再新建。请立即复制到密码管理器或 `.env` 文件。

### 撤销 key

发现 key 泄漏或不再使用 → 立即撤销：

**账号设置 → API Keys → 对应行点「撤销」**。撤销立即生效，不可恢复（DB 行保留作审计）。

> key 的签发与撤销都只走网页端。**不要**把浏览器的登录 cookie 复制到
> 命令行去调管理接口——登录态权限高于 API key，一旦进了 shell history、
> 进程参数或 agent 日志就等于长期泄漏。命令行只该拿到 `pd_live_…` 这种
> 限定 scope、可单独吊销的凭证。

### 用 key 调用

把 key 放在环境变量里，避免泄漏到 shell history / 截图：

```bash
export PD_KEY='pd_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
curl -H "Authorization: Bearer $PD_KEY" \
  https://www.paperdaily.org/api/v1/digest/today
```

## 概念

### 论文 ID

ID 优先级：

1. **OpenAlex Work ID**（`W` 开头，例如 `W4404012345`）
2. **DOI**（`10.xxxx/...`，仅当 OpenAlex 没收录时）
3. **arXiv ID**（`2503.12345`，仅当 OpenAlex 和 DOI 都不可用时）

**调用时永远用 `/digest/*` 或 `/papers/*` 返回的原始 id**，不要自己改格式（去前缀、补前缀、改大小写都会破坏匹配）。

### Tier 分层

每日报告分四层，重要性递减：

| Tier | 字段名 | 期望使用方式 |
|---|---|---|
| Tier 1 | `tier1` | "必读" — 一般 1-3 篇，强匹配兴趣 + 高新颖度 |
| Tier 2 | `tier2` | "强相关" — 5-10 篇，兴趣命中或图路径推荐 |
| Tier 3 | `tier3` | "上下文" — 同主题但弱相关，了解领域用 |
| Tier 4 | `tier4` | "浏览" — 长尾，仅展开看 |

每个 tier 内是按重排序后的得分递减排列。

### why 字段（推荐理由）

Tier 内的每一篇都有一个 `why` 字符串，**模板化生成自图路径**（如"你关注的作者 X 引用了它"、"匹配你的话题 Y"）。这是确定性的事实溯源，不是 LLM 自由生成的 prose — 可以放心展示给用户。

## 调用示例

### 拉今日 digest

```bash
curl -H "Authorization: Bearer $PD_KEY" \
  https://www.paperdaily.org/api/v1/digest/today | jq
```

返回 `DigestReport`，包含 4 个 tier 列表 + summary + stats（每个 tier 的论文数）。

### 拉历史 digest

```bash
curl -H "Authorization: Bearer $PD_KEY" \
  https://www.paperdaily.org/api/v1/digest/2026-05-10 | jq
```

最远支持 90 天前。

### 拉今日邮件 HTML

如果你想拿到跟邮箱里 100% 一致的渲染 HTML：

```bash
curl -H "Authorization: Bearer $PD_KEY" \
  https://www.paperdaily.org/api/v1/digest/today/email.html \
  -o today.html && open today.html
```

历史日期同理：`/api/v1/digest/{date}/email.html`。

### 拉单篇论文详情

```bash
curl -H "Authorization: Bearer $PD_KEY" \
  https://www.paperdaily.org/api/v1/papers/W4404012345 | jq
```

返回元数据 + LLM 抽取（`tldr_zh`、关键 claims、方法、数据集等）+ PDF 状态 + 引用概览。重点字段：

| 字段 | 含义 |
|---|---|
| `tldr_zh` / `tldr_en` / `elevator_pitch` | 一句话摘要（中 / 英 / ≤25 字电梯版） |
| `key_claims` | 主要论点数组（每条带 claim / type / evidence） |
| `contributions` / `methods` / `datasets` / `limitations` / `open_questions` / `applicable_to` | LLM 抽取的结构化字段 |
| `primary_topic` | OpenAlex 4 层学科链（Domain → Field → Subfield → Topic） |
| **`has_pdf`** | PDF 是否已被 paperdaily 解析并缓存 |
| **`pdf_status`** | `parsed`／`no_oa_url`／`download_failed`／`mineru_failed`／`permanent_failure`／`null` |
| **`external_full_text_url`** | 外部原文链接（OA URL → arXiv abs → DOI 优先级） |
| **`citations_summary`** | `{n_out, n_in, top_referenced[5]}` 引用概览，无需再调 `/citations` |
| `has_extraction` / `has_embedding` | 数据完备性标记 |

### 用 `?include=` 一次拿更多数据

需要展开引用列表 / 完整作者 / 最近被引时，加 `?include=` 即可在同一次调用里一并拿到，避免多次往返：

```bash
curl -H "Authorization: Bearer $PD_KEY" \
  'https://www.paperdaily.org/api/v1/papers/W4404012345?include=references_full,cited_by_recent,authors_full' | jq
```

| include 值 | 增加字段 | 说明 |
|---|---|---|
| `references_full` | `references_full[]` | 本文引用的全部论文（不含 stub） |
| `cited_by_recent` | `cited_by_recent[]` | 最近 5 篇引用本文的论文 |
| `authors_full` | `authors_full[]` | 全体作者（含 ORCID、position） |

每个 include 增加一次 DB 查询，**把想要的一次列出**最划算。未列出的 include 默认不展开（响应里字段为 `null`）。

### 直接 DOI / arXiv id 反查

```bash
# DOI 反查（DOI 含 / 也能直接当 path）
curl -H "Authorization: Bearer $PD_KEY" \
  https://www.paperdaily.org/api/v1/papers/by-doi/10.1109/CVPR.2026.123

# arXiv id 反查
curl -H "Authorization: Bearer $PD_KEY" \
  https://www.paperdaily.org/api/v1/papers/by-arxiv/2410.12345
```

### 任意标识符直达（resolve，0.8.0）

> 本节端点已于 2026-07-26 对生产 v0.8.0 实测验证。

不确定手里的标识符是哪种格式时，交给 `resolve` 归一化：W-id、DOI
（裸串 / `doi:` 前缀 / `https://doi.org/…` URL）、arXiv id（裸号 /
`arXiv:` 前缀 / `abs/` URL，版本号自动剥离）都认。命中返回完整
`PaperDetail` + `resolved_by`（`id` / `doi` / `arxiv`，标注按哪条路径
命中）；404 = 未收录（响应提示改用 `/papers/search` 找）：

```bash
curl -H "Authorization: Bearer $PD_KEY" \
  "https://www.paperdaily.org/api/v1/papers/resolve?id=10.2139%2Fssrn.7123198" | jq
```

resolve 只解析标识符，不解析标题——标题模糊匹配用下面的 search。

### 统一分层检索（search，0.8.0）

> 本节端点已于 2026-07-26 对生产 v0.8.0 实测验证。

```bash
curl -H "Authorization: Bearer $PD_KEY" \
  "https://www.paperdaily.org/api/v1/papers/search?q=Harnessing+LLMs&subfield_id=1702&year_from=2026" | jq
```

返回：

```json
{
  "items": [
    {"id": "ssrn:7123198", "title": "Harnessing LLMs ...",
     "publication_date": "2026-07-15", "match_layer": "title", "score": 0.63}
  ],
  "layer_used": "title",
  "took_ms": 48
}
```

`mode=auto`（默认）按瀑布逐层尝试：① q 形如标识符 → 内部 resolve 单条
命中（`match_layer='id'`）；② 标题词法匹配（`match_layer='title'`）；
③ 语义向量兜底（`match_layer='semantic'`）。`match_layer` 标注每条结果
的命中层，`layer_used` 是本次查询实际走到的最深层。`mode=title` /
`mode=semantic` 可钉住单层；`limit` 上限 50。

**`q=` 在这里不要求 facet 过滤**——`subfield_id` / `field_id` /
`year_from` / `year_to` 都是可选的收窄条件，不是准入条件（区别于
legacy `/papers?q=` 的老 422 规则，见 FAQ）。scope `read:paper`，
0 积分。

### 拉相似论文

```bash
curl -H "Authorization: Bearer $PD_KEY" \
  "https://www.paperdaily.org/api/v1/papers/W4404012345/similar?k=20" | jq
```

基于 SPECTER2 / bge-m3 embedding 的 cosine 邻居，按相似度递减。

### 更新兴趣画像

关注一个 topic（明天的 digest 会权重更高）：

```bash
curl -X POST https://www.paperdaily.org/api/v1/me/topics \
  -H "Authorization: Bearer $PD_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"topic_id":"T11000","weight":1.5}]}'
```

记录论文反馈（推荐系统会用到）：

```bash
curl -X POST https://www.paperdaily.org/api/v1/me/feedback \
  -H "Authorization: Bearer $PD_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"paper_id":"W4404012345","action":"like","reason":"why_path"}'
```

`action` 取值：`like` / `dislike` / `save` / `skip` / `open`。

### 综合提问（synth:ask）

把自然语言问题交给 LLM，后端拼装 KG 工具调用并合成答案：

```bash
curl -X POST https://www.paperdaily.org/api/v1/ask \
  -H "Authorization: Bearer $PD_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"question":"我最近关注的因果推断里，下周需要重点读哪几篇？"}'
```

返回 `answer` 文本 + `cited_paper_ids` 引用列表 + `trace` 调试日志（每步 `name`/`args`/`result_summary`）。**单次响应 30-100 秒**，记得调大 client timeout。需要 `synth:ask` scope（默认不开，建表时手动勾）。

### 深读回传与回看（reading sessions，0.8.0）

deep-research skill 精读完一批论文后，可以（在你明确同意后）把**分析
产物**——逐篇笔记、四态论断账本、综述报告——上传成一个 reading
session，之后在网页工作台回看、装配综述矩阵。上传需要 `write:reading`
scope（手动勾），回读需要 `read:reading`。

> 本节端点已于 2026-07-26 对生产 v0.8.0 实测验证。

```bash
# 上传（payload 由 skill 的 upload_session.py 组装并先过本地质量门）
curl -X POST https://www.paperdaily.org/api/v1/me/reading-sessions \
  -H "Authorization: Bearer $PD_KEY" \
  -H 'Content-Type: application/json' \
  -d @session.json
# 首传 201 → {"id":"Xy3kP9qLmA2w", ...}
#（session id 是不透明随机串，token_urlsafe 风格、无固定前缀——永远用服务端返回的原值，不要自己拼）
# 同一 worklist 重复上传 → 200 + {"deduplicated": true}（幂等，不产生重复条目）

# 回看：列表返回 {"items":[...]} 包装形态，不是裸数组
curl -H "Authorization: Bearer $PD_KEY" \
  https://www.paperdaily.org/api/v1/me/reading-sessions | jq
curl -H "Authorization: Bearer $PD_KEY" \
  https://www.paperdaily.org/api/v1/me/reading-sessions/Xy3kP9qLmA2w | jq
```

payload 上限：papers ≤100、单篇笔记 ≤64KB、claims ≤200、report ≤2MB、
单条引文 quote ≤500 字符。**不收 PDF / 二进制 / base64**——上传的只有
你自己的派生分析，抓取的原文永留本地。网页回看入口：
`https://www.paperdaily.org/workbench?tab=reading&s=<id>`。POST 返回
403 说明 key 缺 `write:reading`，去 /account 重签一把勾上该 scope。

### 任务收件箱与上下文打包（agent tasks / context bundle，0.8.x A1）

> 本节端点**未经生产验证**（A1 后端并行实现中）——下方每条命令前的
> 以下端点自 0.8.1 起生产可用。契约细节见
> `docs/api/AGENT_PROTOCOL.md §3/§5`。

在网页上（如论文详情页「转给我的 agent 深读」）给自己的本地 agent 排
任务；agent 侧拉取（`read:reading`）、领取（`write:reading`）、做完上传
reading session 时带 `task_id` 自动完结：

```bash
拉 pending 任务（{items} 包装，同 reading-sessions 列表形态）
curl -H "Authorization: Bearer $PD_KEY" \
  "https://www.paperdaily.org/api/v1/me/agent-tasks?status=pending" | jq
# → {"items":[{"id":"…","kind":"deep_read",
#      "payload":{"paper_ids":["arxiv:2605.10419"],"note":"…"},
#      "status":"pending","created_at":"…"}]}

领取（200 = claimed；409 = 已被领取或已完结，属正常语义不要重试）
curl -X POST -H "Authorization: Bearer $PD_KEY" \
  https://www.paperdaily.org/api/v1/me/agent-tasks/<task_id>/claim

上传 reading session 时 payload 带 "task_id" 即自动完结该任务；
# 响应新增 task_linked: true/false——false = 任务不存在/不属于你/已完结，
# session 本身照常入库，不因回链失败而失败
curl -X POST https://www.paperdaily.org/api/v1/me/reading-sessions \
  -H "Authorization: Bearer $PD_KEY" -H 'Content-Type: application/json' \
  -d @session.json
```

深读一篇论文前，可以单往返拉它的预计算上下文包（`read:paper`，五段：
引文邻域双向 top-5 + 立场句 / 版本链 / topic 四层链 / signals（novelty、
被引数、subfield 周脉搏）/ 你的历史关联（矩阵、合集、深读 session 命中））：

```bash
curl -H "Authorization: Bearer $PD_KEY" \
  "https://www.paperdaily.org/api/v1/papers/W4404012345/context-bundle" | jq
```

deep-research skill 已把收件箱包成 `pd_inbox.sh`（列取 / `--claim` 领取，
key 缺 scope 时软降级不报错），`upload_session.py --task-id <id>` 负责
完结回链。

## Python 示例

### 最小客户端

```python
import os
import httpx

API = "https://www.paperdaily.org/api/v1"
KEY = os.environ["PD_KEY"]
http = httpx.Client(
    headers={"Authorization": f"Bearer {KEY}"},
    base_url=API,
    timeout=30,
)

def fetch_today() -> dict:
    return http.get("/digest/today").json()

def fetch_paper(pid: str) -> dict:
    return http.get(f"/papers/{pid}").json()

print(fetch_today()["stats"])
```

### 接 Claude agent

把今天 tier1 的论文做成 system context，让 agent 决定先读哪一篇：

```python
import os
import httpx
import anthropic

API = "https://www.paperdaily.org/api/v1"
KEY = os.environ["PD_KEY"]
http = httpx.Client(
    headers={"Authorization": f"Bearer {KEY}"},
    base_url=API,
    timeout=30,
)

digest = http.get("/digest/today").json()
papers = digest["tier1"][:5]
context = "\n\n".join(
    f"# {p['paper']['title']}\nTLDR: "
    f"{http.get(f'/papers/{p[\"paper\"][\"id\"]}').json().get('tldr_zh') or '—'}\n"
    f"Why: {p.get('why') or '—'}"
    for p in papers
)

client = anthropic.Anthropic()
msg = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=512,
    system=f"你是一个论文摘要助手。\n\n今日候选清单：\n{context}",
    messages=[{"role": "user", "content": "我应该先读哪一篇？为什么？"}],
)
print(msg.content[0].text)
```

### 写反馈 + 关注（用完即弃的脚本）

```python
import os
import httpx

API = "https://www.paperdaily.org/api/v1"
KEY = os.environ["PD_KEY"]
http = httpx.Client(
    headers={"Authorization": f"Bearer {KEY}"},
    base_url=API,
    timeout=30,
)

# 关注一组 topic
http.post("/me/topics", json={
    "items": [
        {"topic_id": "T11000", "weight": 1.5},
        {"topic_id": "T11042", "weight": 1.0},
    ],
})

# 批量打 like
for pid in ["W4404012345", "W4404012346", "W4404012347"]:
    http.post("/me/feedback", json={
        "paper_id": pid,
        "action": "like",
        "reason": "why_path",
    })
```

## Scope 权限

每把 key 都有一组 scope，调对应 scope 范围外的端点会返回 403。

| Scope | 用途 | 默认勾选 |
|---|---|---|
| `read:digest` | 拉日报（`/digest/*`） | ✅ |
| `read:paper` | 单篇详情 + 相似 + resolve/search 检索（`/papers/*`） | ✅ |
| `write:profile` | 改兴趣画像（`/me/topics`, `/me/authors`, `/me/feedback`） | ✅ |
| `synth:ask` | LLM 综合提问（`/ask`） | ❌ 需手动勾 |
| `read:reading` | 深读 session 回读（`GET /me/reading-sessions*`，0.8.0） | ✅ |
| `write:reading` | 深读产物上传（`POST /me/reading-sessions`，0.8.0） | ❌ 需手动勾 |

> `synth:ask` 默认不勾是因为每次调用都触发后端 LLM token 消耗；`write:reading` 默认不勾是因为它是写操作——agent 上传你的分析前必须经你显式同意，key 层面同样要求显式授权。如果你确认要用，可以单独建一把仅带对应 scope 的 key，便于追踪。

## 端点速查

### 读类（`read:digest`）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/digest/today` | 今日 4-tier 报告（JSON） |
| GET | `/digest/{date}` | 指定日期报告（≤90 天） |
| GET | `/digest/today/email.html` | 今日真实邮件 HTML |
| GET | `/digest/{date}/email.html` | 历史邮件 HTML |
| GET | `/digest/today/papers?tier=1` | 把某 tier 拍平成论文列表 |

### 读类（`read:paper`）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/papers/{id}` | 详情 + LLM 抽取 |
| GET | `/papers/{id}/similar?k=20` | 相似邻居（向量召回） |
| GET | `/papers/resolve?id=` | 任意标识符直达（0.8.0；`resolved_by` 标注，404=未收录） |
| GET | `/papers/search?q=&mode=auto` | 分层检索：标识符→标题→语义瀑布（0.8.0；`match_layer`/`layer_used`） |

### 深读回传（`read:reading` / `write:reading`，0.8.0）

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/me/reading-sessions` | 上传深读产物（幂等按 `worklist_fingerprint`，重复返回 200 + `deduplicated`） |
| GET | `/me/reading-sessions` | session 列表（`{"items":[...]}` 包装，非裸数组） |
| GET | `/me/reading-sessions/{id}` | 单个 session（notes/claims/report 全量） |

### 画像 + 反馈（`write:profile`）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/me/profile` | 当前画像完整快照 |
| GET | `/me/topics` | 关注的 topic 列表 |
| POST | `/me/topics` | 关注一批 topic |
| DELETE | `/me/topics/{topic_id}` | 取消关注 |
| GET | `/me/authors` | 关注的作者列表 |
| POST | `/me/authors` | 关注一批作者 |
| DELETE | `/me/authors/{author_id}` | 取消关注作者 |
| GET | `/me/feedback?limit=50` | 最近反馈记录 |
| POST | `/me/feedback` | 写一条反馈（like / dislike / save / skip / open） |
| GET | `/me/changes?limit=50` | 画像变更审计日志 |

### 综合提问（`synth:ask`）

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/ask` | 自然语言提问 → LLM + KG 工具 → 合成答案 + 引用 |

## Rate Limits

Free tier 限速，按 scope 分别计算：

| Scope | 每分钟 | 每天 |
|---|---|---|
| `read:digest` | 30 | 5,000 |
| `read:paper` | 60 | 10,000 |
| `write:profile` | 60 | 1,000 |
| `synth:ask` | 2 | 5 |
| `read:reading`（0.8.0） | 60 | 10,000 |
| `write:reading`（0.8.0） | 10 | 100 |

429 响应里会带：

- `Retry-After`：建议等待的秒数（数值或 HTTP-date）
- `X-RateLimit-Limit`：当前 scope 的窗口上限
- `X-RateLimit-Remaining`：剩余配额
- `X-RateLimit-Reset`：配额重置时刻（Unix 时间戳）

**请尊重 `Retry-After`**。持续硬撞 429 的 key 会被自动节流（短期 403，反复触发后人工 review）。

### Python 风格的 backoff 示例

```python
import time
import httpx

def call_with_retry(req, max_retries=4):
    for attempt in range(max_retries):
        r = req()
        if r.status_code != 429:
            return r
        wait = int(r.headers.get("Retry-After", "30"))
        time.sleep(wait)
    return r  # 最后一次的响应
```

## 错误码

| 状态码 | 含义 | 建议处理 |
|---|---|---|
| 200 / 201 / 204 | 成功 | — |
| 400 | 请求格式错误（如日期非 ISO） | 检查请求体；不要重试 |
| 401 | key 缺失 / 无效 / 已撤销 | 检查 Authorization header；去 /account 重发 key |
| 403 | key 有效但缺少所需 scope | 重建 key 时勾选对应 scope |
| 404 | 资源不存在（如未知 paper id） | 别造 id，用 `/digest` 返回的原值 |
| 422 | 请求体验证失败 | 看响应里的 pydantic 错误字段定位 |
| 429 | 超频率 | 按 `Retry-After` 等待 |
| 5xx | 服务器问题 | 退避重试（指数 backoff，最多 3-4 次） |

错误响应统一形如：

```json
{
  "detail": "key not found or already revoked"
}
```

或对于 422，FastAPI 标准格式：

```json
{
  "detail": [
    {"loc": ["body", "topic_id"], "msg": "field required", "type": "missing"}
  ]
}
```

## 常见问题

### key 写错位置怎么办？

key **只在 `Authorization` 头**里。Query string、cookie、body 都不识别。如果你不小心放进 URL 或 form data，会得到 401。

### 怎么测试 key 是否有效？

最便宜的探活：

```bash
curl -i -H "Authorization: Bearer $PD_KEY" \
  https://www.paperdaily.org/api/v1/digest/today | head -3
```

`HTTP/1.1 200 OK` 即正常；401 / 403 看响应 body 的 `detail`。

### 为什么我的 digest 是空的？

可能性：

1. 你刚注册，还没填兴趣画像 → 去 /profile 写一段
2. 今天的 pipeline 还没跑完（每天约 07:30 完成）→ 隔半小时再试
3. 你的兴趣太窄，今天没有匹配的新论文 → 看 `tier4`（浏览层），或调宽 topics

### `/v1/papers?q=` 报 422 是怎么回事？

0.8.0 之前，`q=`（标题模糊搜索）在不带任何主题 / 学科筛选时会被服务端拒绝（防全表 seq scan）。**0.8.0 起**：`q=` 走 `paper_search` 投影（带索引），投影可用时不带 facet 过滤也接受；422 只在投影关闭的降级路径（`PAPER_SEARCH_PROJECTION=off`）保留。

新代码建议直接用统一分层检索端点（q= 从不要求 facet，且带命中层标注）：

```bash
# 推荐（0.8.0+）
.../api/v1/papers/search?q=transformer&limit=20

# 仍可用：facet 收窄只是可选条件
.../api/v1/papers/search?q=transformer&subfield_id=1702&year_from=2025
```

### 论文没有 PDF 怎么读原文？

- 看 `pdf_status`：
  - `parsed` → 平台有缓存，直接用查看按钮打开
  - `no_oa_url` → 这篇论文没找到开放获取链接（多见于闭源期刊文章）
  - `download_failed` / `mineru_failed` → PDF 抓取或解析过程出问题，平台会自动重试
- 回退路径：用 `external_full_text_url` 字段，它按 OA URL → arXiv abs → DOI 顺序返回外部原文链接，永远不为 null（除非 paper 自身没有任何 doi / arxiv_id）。

### 如何拉取超过 90 天前的报告？

free tier 不支持。如果有研究需求，邮件联系站长。

### 邮件 HTML 渲染跟邮箱里不一样？

本地预览版可能省略个性化退订链接（该链接依赖服务端签名配置），其他内容一致。

## 更多资源

- **OpenAPI 完整规范**: <https://www.paperdaily.org/api/v1/openapi.json>
- **Swagger UI（在线调试）**: <https://www.paperdaily.org/api/v1/docs>
- **英文版 quickstart**: <https://www.paperdaily.org/docs/api/quickstart.html>
- **LLM agent 自发现**: <https://www.paperdaily.org/llms.txt>
- **GitHub Issues**（反馈 bug / feature request）: 联系站长

---

*最后更新：2026-07-26（0.8.0：新增 resolve / search 分层检索与 reading sessions 深读回传；0.8.x A1：新增 agent 任务收件箱与 context bundle 示例段，标注 `` 待生产验证）*
