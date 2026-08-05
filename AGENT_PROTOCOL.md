# Paperdaily Agent 协议（Protocol v1）

> **Status**: ①②④⑦章随 0.8.0 定稿（2026-07-26）；③⑤章随 A1 定稿（2026-07-26，0.8.x 第二批，端点上线前字段名以 OpenAPI 为准）；④⑦章随 0.8.5 修订（2026-07-27 回传面安全评审：evidence 条数上限 + 共享改管理员前置审核）；③章随 0.8.6 补 `GET /me/saves`（收藏库读回）；⑥⑧持续演进。
> **定位**: 本地 agent（用户侧 Claude + skills）↔ paperdaily 服务端的**对外契约单一呈现层**。本文是契约的权威呈现；`quickstart-zh.md` 是示例层，与本文冲突时以本文为准。服务端行为以运行版本的 OpenAPI 为准。
> **心智模型**: 车间与总控室——agent（车间，多实例）做认知与授权全文；服务端（总控室，单实例）做记忆、图谱、路由。分析真相在服务端，原文真相在本地。

## 1. 身份与授权

- 凭证：`Authorization: Bearer pd_live_…`，在 web `/account` → API Keys 签发；明文仅显示一次。
- scopes（最小权限原则——skill 只申请用得到的）：

| scope | 授予 | 典型消费者 |
|---|---|---|
| `read:digest` | 日报读 | thin CLI |
| `read:paper` | 论文/检索/bundle 读 | 所有 skill |
| `synth:ask` | /ask 服务端合成 | 需综述时 |
| `write:profile` | 画像写 | 显式管理画像时 |
| `read:contrib` / `write:contrib` | 阅读行为事实 + **收藏库读回**（`GET /me/saves`，0.8.6 新增） | reader 类客户端 / 需要「用户收藏了什么」的 skill |
| `read:reading`（0.8.0 新增） | 深读 session 回读 / 任务收件箱 | deep-research 类 |
| `write:reading`（0.8.0 新增） | 深读产物上传 / 任务完结 | deep-research 类，**用户显式勾选** |

- agent 不得代签/代管 key（需要 cookie session）；key 缺 scope 时**静默降级只读并提示**，不报错、不绕行。

## 2. 检索协议（分层；agent 不走 /ask）——0.8.0 定稿

**规范**：agent 检索一律走结构化分层端点；`/ask` 仅当需要**服务端合成**（如领域综述 overview）。理由：agent 自身是 LLM，/ask 的 NL 翻译 + 散文合成两段对 agent 是浪费（慢/贵/有损/多幻觉面）。

| 层 | 端点 | 语义 | scope / 计费 |
|---|---|---|---|
| L0 | `GET /papers/resolve?id=<任意标识符>` | W-id / DOI（裸 / `doi:` / `https://doi.org/…`）/ arXiv id（裸 / `arXiv:` / `abs/` URL，版本号剥离）→ `PaperDetail` + `resolved_by: 'id'\|'doi'\|'arxiv'`；未收录 404（body 提示转 search）。不解析标题——标题归 search | `read:paper`，0 分，限流 |
| L1/L2 | `GET /papers/search?q=&mode=auto&limit=&subfield_id=&field_id=&year_from=&year_to=` | 瀑布（`mode=auto`）：① q 形如标识符 → 内部 resolve 单条命中 `match_layer='id'`；② 标题词法（`paper_search` 投影）`match_layer='title'`；③ 语义向量兜底 `match_layer='semantic'`。响应 `{items:[{id,title,publication_date,match_layer,score,…}], layer_used, took_ms}`；`mode=title\|semantic` 可钉住单层；`limit ≤50`；**q= 无需 facet 过滤**（区别于 legacy `/papers?q=`） | `read:paper`，0 分，限流 |
| L2' | `GET /papers/{id}/similar` | SPECTER2 相似（BGE 兜底） | `read:paper`，0 分 |
| L3 | `GET /papers?topic_ids=…` / `/authors/*` / `/taxonomy/*` | 图谱/分面浏览 | `read:paper`，0 分 |
| L4 | `POST /ask` | 服务端 LLM 合成（散文 + cited_paper_ids） | `synth:ask`，10 分 |

**精准指向用 resolve（单篇或空），模糊匹配用 search（层标注排序列表）**。search 未命中时逐层降级，agent 不应自行对 /papers 全列表做客户端过滤模拟搜索。legacy `/papers?q=` 仍在（0.8.0 起投影就绪时同样放开强制 facet 过滤），但 agent 一律用 `/papers/search`。

## 3. 上下文协议（context bundle）——A1 定稿

- `GET /papers/{id}/context-bundle`（`read:paper`，纯读 0 分）：服务端预计算上下文打包，单往返替代 5-6 次零散拉取。响应五段（段的语义定稿如下，具体字段名以 OpenAPI 为准）：

| 段 | 内容 |
|---|---|
| 引文邻域 | 双向 top-5（引用 / 被引，复用 citations 既有查询）+ cites_context 立场句 |
| 版本链 | paper_groups 版本链（如 SSRN-WP ↔ 正式发表版） |
| topic 链 | Domain → Field → Subfield → Topic 四层归属 |
| signals | novelty 分、被引数、subfield 最近周 pulse 摘要 |
| you | **你的**历史关联：claims / notes / 综述矩阵 / 合集 / 深读 session 命中 |

- `GET /api/v1/me/saves?limit=`（`read:contrib`，纯读 0 分，0.8.6 新增）：**用户收藏库**——`{items:[{paper_id, title, publication_date, doi, venue_name, venue_badges, tldr_zh}], total}`，与 web 库页同一份查询同一套富化字段（agent 拿到即用，不必逐篇再打 `/papers/{id}`）。`limit` 默认 100、上限 500；按 `publication_date` 倒序（**不是收藏时间**——AGE 的 `SAVED` 边无时间戳）。
  > **要「用户收藏了什么」只能读这个端点，不要拿 `GET /me/feedback` 反推。** feedback 是行为流水，只记录经反馈路径产生的动作；用户在库页直接点收藏的论文根本不进流水，反推出的清单会系统性偏少（实地案例：库里 20 篇、反推只得 6 篇）。这个端点 0.8.6 之前不存在，是当时唯一能凑合的替代——现在不是了。
- `you.open_questions_total` = **用户级** open 问题总数（问题收件箱整体水位，不随当前论文变化——不是"与本篇相关的问题数"）。
- 规范：深读一篇论文前**应**拉取 bundle 注入精读上下文（"在知识全景中读"——邻域立场、版本沿革、领域脉搏与你自己的历史论断一并进 prompt）；bundle 即拉即用不缓存跨日（内容随库演进）；404 = 论文未收录，按业务降级为直接精读，不视为故障。

## 4. 回传协议（深读产物上传）——0.8.0 定稿

- `POST /api/v1/me/reading-sessions`（`write:reading`）。payload：`{title, provenance{kind, skill_ver, model, taxonomy, worklist_fingerprint}, papers:[{paper_id, depth:'fulltext'|'abstract-only', note_md}], claims:[{claim, status:'supported'|'weak'|'contested'|'gap', evidence:[{paper_id, location, quote}], note}], report_md?, overview_md?, task_id?}`（`task_id` 为 A1 新增可选项：任务完结回链，语义见 §5）。
- **幂等**：`worklist_fingerprint`（worklist.jsonl 按行 strip 后 `join('\n')` 的 sha256）唯一键；重复上传返回既有 session——**200 + `deduplicated: true`**（首传 201）。
- **禁止件（硬性）**：PDF/任何二进制/base64 内嵌 —— 服务端拒收；原文永留本地（版权红线）。引文以短句为限。
- **上限（定稿）**：papers ≤100 / 单篇 note_md ≤64KB / claims ≤200 / **单条 claim 的 evidence ≤20 条**（v0.8.5 收紧，见下）/ report_md ≤2MB / 单条 evidence.quote ≤500 字符；整个请求体 ≤12MiB；超限 413/422。
  > **v0.8.5 破坏性收紧（唯一一处）**：`claims[].evidence` 此前无条数上限。这是 payload 里唯一无界的列表，配合当时只看 `Content-Length` 的请求体门（chunked 请求可绕过，且服务端在鉴权前就把 body 读进内存）构成鉴权前的内存耗尽面。超过 20 条现在 422。真实深读的页码锚点式证据远低于此，实践中不会碰到；碰到了就是该拆成多条 claim。
- **共享不是上传的一部分**：上传只入库，`visibility` 恒为 `private`。对外可见是**用户**在 web 端的独立动作，且从 v0.8.5 起需**管理员审核放行**（见 §7.5）。agent 不得代替用户申请公开。
- **回读**：`GET /api/v1/me/reading-sessions`、`GET /api/v1/me/reading-sessions/{id}`（`read:reading`）；web 回看 `https://www.paperdaily.org/workbench?tab=reading&s=<id>`。
- **质量语义**：服务端不做真伪裁决；不过质量门（notes 字段完整度、claims evidence 非空率）的上传**可存但标 unverified、不计积分**。参考客户端 gate 见 `skills/paperdaily-deep-research/scripts/upload_session.py`（evidence 非空率 ≥80% 等）。
- 上传是**用户显式同意后的独立动作**——skill 不得静默上传（见 SKILL_SPEC 同意点条款）。

## 5. 任务协议（agent 收件箱）——A1 定稿

- 状态机：`pending → claimed → done | cancelled`。`done` 由上传完结回链触发；`cancelled` 由用户在 web 端撤销。
- **拉取**：`GET /api/v1/me/agent-tasks?status=pending`（`read:reading`）→ `{items:[{id, kind, payload, status, created_at}]}`——**`{items}` 包装**，与 reading-sessions 列表同形态，非裸数组；`payload` 形态随 kind（见下方 kind 语义行）。
- **问题原文**：`GET /api/v1/me/questions/{question_id}`（`read:reading`）→ `{id, question, origin, origin_session_id, seed_claim_ids, status, created_at, updated_at}`——`answer_question` 任务只带 `question_id`，agent 领单后从这里取研究目标原文；404 = 问题不存在或不属于你。
- **领取**：`POST /api/v1/me/agent-tasks/{id}/claim`（`write:reading`）→ 200 = 已置 claimed；**409 = 任务已被领取或已非 open 态**——benign 语义（§6 的 4xx 纪律），不重试，换任务或径直开工。claim 不幂等：重复 claim 得 409。
- **完结回链**：`POST /me/reading-sessions` payload 可带可选 `task_id`，响应新增 `task_linked: bool`。`true` = 任务标 done 并回链 `result_session_id`；`false` = task_id 不存在/不属于你/已完结——**session 本身照常入库**，回链失败不连坐上传。
- **幂等**：入队侧由服务端保证——同 kind + **同 payload** 的 open 态任务不重复入队（幂等键 = md5(payload)，口径顺序敏感：`deep_read` 的 paper_ids 顺序即语义，首篇=种子，同组乱序视为不同任务；`answer_question` 同理，键 = question_id + 同序 seed_paper_ids）；agent 对同一任务不重复加工（列表里见过、claim 过或已回链的不再做）。
- kind：`deep_read`（A1，`payload:{paper_ids[], note?}`——paper_ids 作深读种子，skill 侧第一篇当 `--paper` 种子、其余并入 worklist）、`answer_question`（**A2 起启用**，`payload:{question_id, seed_paper_ids[]}`——`question_id` 指向用户的一条 reading question，其问题文本即研究目标（问题原文经 `GET /api/v1/me/questions/{question_id}`（`read:reading`）获取，见上方端点行）；`seed_paper_ids` 首篇作种子、其余并入 worklist；任务可由**服务端问题值班自动生成**（question_leads 命中）或**用户在 web 从线索卡下单**）。
- 参考客户端：`skills/paperdaily-deep-research/scripts/pd_inbox.sh`（列取/领取，缺 scope 软降级）+ `upload_session.py --task-id`（完结回链）。

## 6. 行为规范

- **限流**：尊重 `429` + `Retry-After` 与 `X-RateLimit-*`；退避重试上限 3 次，不得轰炸。
- **错误语义**：`402` 积分耗尽（仅 /ask 类）——向用户呈现，不自动重试；`403` scope 不足——提示补签，不绕行；`404` 资源不存在——按业务降级；`422` 载荷非法——修正后重试一次。
- 网络瞬断按指数退避；连续失败向用户如实报告（诚实账本），不得盖章成功。

## 7. 合规红线

1. 抓取的 PDF **永不上传/转发**——只上传用户自著派生分析。
2. 机构直连/浏览器兜底获取全文是 **opt-in**（`PD_FETCH_INSTITUTIONAL`/`PD_FETCH_BROWSER`），仅在用户机构授权网络内合法；不含 Sci-Hub 类渠道。
3. 写操作（上传/画像回流）必须**显式征得用户同意**；回流个性化默认关。
4. `denied`（付费墙拒绝）是终态不是故障，不做技术性重试。
5. **用户回传一律不能直接对外**（v0.8.5，安全评审 2026-07-27 CEO 裁决）。上传落库 = 私有。用户把 session 切成「链接可见 / 社区可见」只是**提交审核**：服务端先跑引文门预筛（覆盖逐篇 note ∪ `report_md` ∪ `overview_md`，另加 evidence 引文总量；单块 ≤8000 字符、每份文档引文占比 ≤25%、evidence 合计 ≤8000 字符），过筛后置 `pending` 进管理员队列，**放行后才对外可读**——`unlisted-link` 与 `community` 一视同仁（能力链接不可枚举，但版权文本一样离开了本机）。被管理员下架的 session 用户不能自行重新公开。
   - 对 agent 的含义：**不要向用户承诺「上传后就能分享链接」**。上传成功 ≠ 可公开；如实说明「已入库，公开需平台审核」。
   - 引文门是**预筛不是判定**：它看 blockquote 标记与引号段，看不见无标记的逐字抄写。别把「过了门」当成「合规了」——合规责任仍在写笔记的那一步：转述优先，直接引用留短句 + 页码锚点。

## 8. 版本化

- 协议版本随本文头部演进（v1 起）；服务端版本经 `GET /api/version`。
- 兼容承诺：v1 端点 additive-only；破坏性变更先在已知问题清单立案，并在本文标注迁移窗口。
  - **已用掉的破坏性额度（v0.8.5）**：`claims[].evidence` 加 20 条上限（§4）。安全修复，不设迁移窗口；理由与影响面在 §4 的引注里写明。同批的其它变更都是加严服务端行为（共享需审核）或纯新增字段，不改请求形态。
- skill 兼容矩阵见 `SKILL_SPEC.md` 文末。
