---
name: paperdaily-deep-research
description: End-to-end deep literature research on top of the paperdaily platform. Use when the user wants to go from "a research field/topic/question or a single seed paper" to "a deep, citation-grounded literature review" — query paperdaily for recommended papers and an LLM field overview, fetch the full-text PDFs locally (OA waterfall + optional institutional access), run an agent-team deep-read and cross-paper synthesis, and (optionally, with explicit user consent) upload the analysis back to the paperdaily workbench as a reading session. Triggers: "深度调研某领域", "帮我把这个方向的论文下下来精读", "文献综述 with full texts", "paperdaily deep research", "深读这篇论文" / "--paper <id>". Requires a paperdaily API key (pd_live_…).
---

# paperdaily-deep-research

> **声明块（SKILL_SPEC §1）**
> - **skill_ver**: `0.3.2`
> - **协议版本**: AGENT_PROTOCOL v1（`docs/api/AGENT_PROTOCOL.md`；检索走其 §2
>   分层端点，收件箱走其 §5 任务协议）
> - **所需 scopes**: `read:digest, read:paper, synth:ask`；阶段 0（可选收件箱）
>   另需 `read:reading`（缺失时跳过该阶段不报错）；领取任务（claim）与阶段 4
>   （可选回传）另需 `write:reading`（用户显式勾选，缺失时只读降级不报错）

从「一个研究领域 / 一篇种子论文」到「一份可溯源的深度文献综述」的三阶段
流水线，外加可选的阶段 0 收件箱与阶段 4 回传。阶段间用落盘工件衔接
（phase gate），可以从任意阶段进入——用户手头已有 worklist 就直接进阶段 2，
已有 PDF 就直接进阶段 3。

```
阶段 0  收件箱（可选） pd_inbox.sh         → 用户在 web 排的任务 → 种子论文
阶段 1  检索+综述     pd_worklist.sh      → worklist.jsonl + overview.md
阶段 2  全文获取      fetch_fulltext.py   → pdfs/*.pdf + fetch_report.jsonl
阶段 3  深度分析      agent team          → notes/*.md + synthesis/* + report.md
阶段 4  回传（可选）  upload_session.py   → paperdaily 工作台 reading session
                                            （--task-id 完结阶段 0 领取的任务）
```

统一工作目录约定（阶段 1 默认创建）：

```
pd-research/<slug>/
├── worklist.jsonl        # 推荐论文清单（id/title/doi/arxiv_id/oa_url）
├── worklist.meta.json    # 检索目标 provenance 侧车（阶段 4 读取）
├── overview.md           # 阶段 1 的 LLM 领域综述
├── pdfs/                 # 阶段 2 下载的全文（永留本地，绝不上传）
├── fetch_report.jsonl    # 每篇的获取账本（命中层/失败原因）
├── notes/<paper_id>.md   # 阶段 3 逐篇精读笔记
├── synthesis/            # 四件套 + claims.jsonl
└── report.md             # 最终深度综述报告
```

## 检索规范（AGENT_PROTOCOL §2 分层）

检索一律走结构化分层端点，`/ask` 只在生成 overview.md 那一次使用：

- **精准指向** → `GET /papers/resolve?id=`：任意标识符（W-id / DOI /
  arXiv id / URL 形态）→ 单篇或 404，响应带 `resolved_by`；
- **模糊匹配** → `GET /papers/search?q=&mode=auto`：标识符→标题→语义
  三层瀑布，结果带 `match_layer` 与 `score`（q= 无需 facet 过滤）；
- **拉池/浏览** → `GET /papers?subfield_id=…` 等 facet 列表与
  `GET /papers/{id}/similar` 相似扩张；
- **不要**用 `/ask` 做检索，也不要拉全列表在客户端过滤模拟搜索。

## Pre-flight（每个环境做一次）

需要 `~/.paperdaily-cli/env`：

```sh
mkdir -p ~/.paperdaily-cli && chmod 700 ~/.paperdaily-cli
cat > ~/.paperdaily-cli/env <<'EOF'
export PD_BASE="https://www.paperdaily.org/api/v1"
export PD_KEY="pd_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
EOF
chmod 600 ~/.paperdaily-cli/env
```

Key 在 web UI 签发：登录 → Settings → API keys → Issue key，scopes 至少
`read:digest,read:paper,synth:ask`；打算用阶段 4 回传的加勾 `write:reading`
（也可事后补签）。文件缺失时**停下来告诉用户先签 key**，不要试图代签
（那需要 cookie session）。

冒烟：

```sh
source ~/.paperdaily-cli/env
curl -sS -o /dev/null -w "%{http_code}\n" "$PD_BASE/openapi.json"   # 期望 200
```

## 阶段 0（可选）— 收件箱

用户可能已经在 paperdaily web 上给自己的 agent 排了任务（如论文详情页
「转给我的 agent 深读」）。起手时若 key 带 `read:reading`，先拉一眼收件箱
（端点契约见 AGENT_PROTOCOL §5）：

```sh
scripts/pd_inbox.sh              # 表格列出 pending 任务（id/kind/论文数/created_at/note；
                                 #   answer_question 任务 NOTE 列显示 question:<question_id>）
scripts/pd_inbox.sh --json       # 机器可读 {items:[…]}——payload 原样透传
                                 #   （deep_read 读 paper_ids；answer_question 读 seed_paper_ids）
scripts/pd_inbox.sh --claim <id> # 领取（pending → claimed）
```

规程：

1. 列出任务后**交给用户选**——做哪个、还是不做直接进阶段 1，都由用户定。
   key 缺 `read:reading` 时脚本打印补签指引后 exit 0（不是报错），直接跳过
   本阶段即可；404（服务端版本低于 0.8.1 或未启用收件箱）同样跳过。
2. 选中的 `deep_read` 任务 → 其 `payload.paper_ids` 作阶段 1 的 `--paper`
   种子：单篇直接 `pd_worklist.sh --paper <id>`；多篇取**第一篇**做种子跑
   完后，把其余 id 逐篇 `GET /papers/{id}` 取 detail 并入 `worklist.jsonl`
   （`source` 标 `"seed"`，与首行同格式），保证任务点名的每一篇都在清单里。
3. 选中的 `answer_question` 任务（A2 起，payload =
   `{question_id, seed_paper_ids[]}`）→ `question_id` 对应的**问题文本就是
   本轮的研究目标叙述**，先取原文再开工：

   ```sh
   curl -sS -H "Authorization: Bearer $PD_KEY" \
     "$PD_BASE/me/questions/<question_id>"   # read:reading；响应 .question 即原文
   ```

   （端点不可用——404/服务端版本低于 0.8.2——时再请用户在 web 工作台「问题」
   页签给出原文作降级。）`seed_paper_ids` 取**首篇**作 `--paper` 种子，其余 id
   按上一条同款并入 `worklist.jsonl`（`source` 标 `"seed"`）。后续阶段与
   deep_read 无异。
4. **领取（`--claim`）放在用户确认要做之后**，不要一列出来就抢占——
   claimed 是向服务端声明「这个任务我做了」；409 = 已被领取/已完结，
   属正常语义不重试，换一个或直接进阶段 1。
5. 做完走阶段 4 上传时带 `--task-id <id>`，上传成功即自动完结该任务
   （响应 `task_linked: true`，session 回链到任务）——两种 kind 都照此
   完结，`answer_question` 无特殊上传形态。

## 阶段 1 — 检索 + 领域综述 + 推荐清单

```sh
scripts/pd_worklist.sh "Artificial Intelligence" --limit 30 --similar 2
scripts/pd_worklist.sh 1702 --year-from 2025 --out ./pd-research/nlp/
scripts/pd_worklist.sh T10270 --limit 20 --no-synth        # Topic id 直连，跳过综述
scripts/pd_worklist.sh "graph neural fraud detection"      # 非 taxonomy 词 → /papers/search
scripts/pd_worklist.sh --paper arxiv:2605.10419            # 单篇种子模式
```

第一个参数按形状解析：`T\d+`=Topic id、1-2 位数字=Field id、4 位数字=
Subfield id、其余按名字精确→子串匹配；全部 miss 时自动降级为
`GET /papers/search?q=&mode=auto` 自由词句检索（0.8.0+ 服务端）。做什么：

1. 主列表：taxonomy 命中走 `GET /papers`（facet 拉池 + `year_from` +
   `has_extraction=true`）；自由词句走 `GET /papers/search?q=&mode=auto`
   （stderr 会打出命中层 `layer_used`）；
2. 每篇 `GET /papers/{id}/similar` 做相邻扩展，合并去重成几十篇推荐池；
3. 每篇 detail 抽 `doi / arxiv_id / oa_url` 写 `worklist.jsonl`——这三个
   字段就是阶段 2 的全部输入；另写 `worklist.meta.json` 侧车记检索目标；
4. 一次 `POST /ask`（用 `load_extractions` 锚定清单内论文）生成
   `overview.md`——这是本 skill 唯一一次 `/ask` 调用。

**`--paper <id>` 单篇种子模式**：`GET /papers/resolve?id=` 解析种子
（W-id / DOI / arXiv id 均可；404 = 未收录，报错并提示改用
`/papers/search` 找它）→ `GET /papers/{id}/similar?k=<limit>` 扩张成
worklist，种子在首行（`source:"seed"`）；`--similar` 在此模式下不生效。
适合「就着这一篇往外读」的场景（如 web 详情页「转给我的 agent 深读」）。

成本：`/ask` 免费档 2/min，一次 ~3-12k input token；多领域连跑时除最重
要的一个外都加 `--no-synth`。`read:paper` 60/min，脚本内置节流。

**Phase gate → 阶段 2**：`worklist.jsonl` 存在、行数 ≥ 目标篇数的 8 成、
每行合法 JSON 且 `doi`/`arxiv_id`/`oa_url` 至少一项非 null 的行占多数。
不满足先修阶段 1，不要带病进入下一阶段。

## 阶段 2 — 全文 PDF 获取（瀑布）

```sh
export UNPAYWALL_EMAIL="you@example.org"          # OA 定位必需（免费；用途见下方「邮箱去哪」）
# export PD_FETCH_INSTITUTIONAL=1                 # 见下方合规边界
# export ELSEVIER_TDM_KEY=… WILEY_TDM_TOKEN=…     # 可选，机构有 TDM key 才配

python3 scripts/fetch_fulltext.py --worklist pd-research/<slug>/worklist.jsonl \
    --out pd-research/<slug>/pdfs/
```

瀑布逐层尝试、命中即停：arXiv 直连 → API 给的 `oa_url` → Unpaywall →
PMC/Europe PMC → 出版商 TDM API（有 key 才走）→ 机构订阅直连（opt-in）
→ Playwright 浏览器兜底（opt-in）→ 失败落账本。细节与排查见
[references/fulltext-sources.md](references/fulltext-sources.md)。

**邮箱去哪（隐私边界）**：`UNPAYWALL_EMAIL` 只发给要求联系邮箱的 polite-pool
API——`api.unpaywall.org` / `api.crossref.org` / NCBI-PMC / EBI（作查询参数）。
它**不进 User-Agent**，所以出版商站点、CDN、从页面 metadata 抠出的第三方地址
都看不到它；写进 `fetch_report.jsonl` 的 URL 也会把 `email`/`token`/`key` 类
参数掩码成 `<redacted>`。凭证（TDM key、平台 key）遇跨 origin 跳转会被剥掉并
在账本记 `cred_stripped_on_redirect`——所以跨域跳转后的 401 是**预期**，不是
出版商故障。

**合规边界（对用户说清楚再开）**：

- 默认只走开放获取渠道，任何网络环境下都合法。
- `PD_FETCH_INSTITUTIONAL=1` 启用出版商直连层——仅当用户机器在**自己
  机构有订阅授权的网络内**（如校园网 IP 授权）才是合法访问。开启前
  必须向用户确认这一点。
- 本 skill 不含、也不会添加 Sci-Hub 等绕过付费墙的渠道。
- 账本里 `denied` 意味着「请求了但被拒（大概率无订阅权限）」，不要
  当成技术故障反复重试。

**Phase gate → 阶段 3**：`fetch_report.jsonl` 中 `ok`+`already` 覆盖
worklist 的 6 成以上即可推进（付费墙论文拿不到是常态）；把 `failed`
清单和失败原因明确报给用户，问是否人工补齐（账本里有 doi.org 链接），
不要沉默丢弃。全文缺失的论文在阶段 3 降级为「仅摘要参与综合」，
且笔记与 claims 里必须标注 abstract-only。

## 阶段 3 — 深度文献分析（agent team）

进入前校验：`pdfs/` 有文件、每个 PDF 前 4 字节是 `%PDF`（阶段 2 的
脚本已保证）、worklist 与 PDF 能按 `<paper_id>.pdf` 对上。

### 3a 逐篇精读（fan-out，sonnet）

按 [references/reading-note-template.md](references/reading-note-template.md)
并行派子 agent，**每个子 agent 只读 1-2 篇 PDF**（防止上下文摊薄），
model 用 `sonnet`。每篇产出 `notes/<paper_id>.md`，铁律：

- 每个字段锚定 PDF 具体位置（页码 / Figure N / Table N），禁止「论文说」；
- 必须读全文，不许只读摘要下结论（abstract-only 的论文显式标注）；
- 笔记不达模板字段完整度的，重派该篇，不要在综合阶段补。

### 3b 跨篇综合（opus / 会话主模型）

全部笔记就绪后（phase gate：notes 数 = 全文数），按
[references/synthesis-templates.md](references/synthesis-templates.md)
产出 `synthesis/` 四件套 + claims 账本：

1. 方法对比表、2. 时间线、3. 流派/分类法、4. 矛盾点与研究空白清单；
5. `claims.jsonl` —— 每条跨篇论断挂证据（论文+页码）与四态
   （supported/weak/contested/gap），**无证据必须标 gap，禁止用模型
   常识补全**。

综合难度高，用 `opus`（或不指定 model 由主会话直接做）。最后按模板的
报告结构整合成 `report.md`，正文论断随文标 `[paper_id p.X]` 引用。

### 3c 追问模式（可选）

报告交付后用户继续提问时，采用 PaperQA2 范式而非直接全量重读：先在
notes/（必要时回到 PDF 原文）里检索相关段落 → 每段做「问题语境下的
打分摘要」→ 重排取最相关 → 生成带论文+页码引用的回答。找不到证据
就明说「所给论文中未见」。

## 阶段 4（可选）— 回传 paperdaily 工作台

把本次深读的**分析产物**（notes / claims / report / overview）上传成
paperdaily 的 reading session，之后可在
`https://www.paperdaily.org/workbench?tab=reading` 回看、装配综述矩阵。

**铁律：必须先征得用户一次明确同意才运行**——上传是写操作
（SKILL_SPEC §4 同意点），默认姿态是只读。向用户说清三点再问：

1. 上传的只有你自己的派生分析（笔记/论断账本/报告），**绝不含 pdfs/
   与任何二进制**——原文永留本地（版权红线，AGENT_PROTOCOL §7）；
2. 幂等：同一 worklist 重复上传返回既有 session（`deduplicated:true`），
   不会产生重复条目；
3. 需要 key 带 `write:reading` scope。

用户同意后：

```sh
python3 scripts/upload_session.py --dir pd-research/<slug>/            # 直接上传
python3 scripts/upload_session.py --dir pd-research/<slug>/ --dry-run  # 先看要传什么
python3 scripts/upload_session.py --dir pd-research/<slug>/ --task-id <id>
                                  # 阶段 0 领取过收件箱任务时带上，上传成功自动完结该任务
```

脚本自带回传前 phase gate（不过门打印缺项清单、退出非零、不上传）：
notes 存在且非空；`claims.jsonl` 每行 status ∈ supported/weak/contested/gap
且 evidence 非空率 ≥80%；`report.md` 存在；papers ≤100、单篇笔记 ≤64KB、
claims ≤200、report ≤2MB、单条 quote ≤500 字符。每篇 depth 按诚实账本
推断：note 头部标注 `abstract-only` 优先，否则看 `fetch_report.jsonl`
有无 `ok`/`already` 记录——只降级不冒充全文级。

结果处理：201 打印 session id + 工作台回看 URL；200 + `deduplicated`
提示已存在；带 `--task-id` 时看响应 `task_linked`——true 打印「已完结
收件箱任务 <id>」，false 说明任务不存在/不属于你/已完结（session 照常
入库，不连坐），如实转告用户；**403 = key 缺 `write:reading`**——引导
用户去 `/account` → API Keys 重签一把勾上该 scope 的 key（不要代签、
不要绕行），其余错误如实转告用户。

## 本 skill 不做什么

- 不改用户的 paperdaily 画像（follow/feedback 等 `write:profile` 操作）。
- 不管理 API key（签发/吊销走 web UI）。
- 不提供任何绕过付费墙的获取渠道。
- 不静默上传：阶段 4 只在用户明确同意后执行，且永不上传 PDF/二进制。

## Files

- `scripts/pd_inbox.sh` — 阶段 0：agent 任务收件箱（列取 pending / `--claim` 领取；缺 scope 软降级，自包含 bash+curl+jq）
- `scripts/pd_worklist.sh` — 阶段 1：taxonomy/查询/种子解析 + 推荐池 + 综述（自包含，bash+curl+jq）
- `scripts/fetch_fulltext.py` — 阶段 2：零依赖瀑布下载器（playwright 可选）
- `scripts/upload_session.py` — 阶段 4：phase gate + 回传（纯 stdlib，绝不上传 pdfs/）
- `references/reading-note-template.md` — 阶段 3a 精读模板与规程
- `references/synthesis-templates.md` — 阶段 3b 四件套 + claims schema + 报告结构
- `references/fulltext-sources.md` — 全文渠道手册：层级/配置/判据/排查/合规
