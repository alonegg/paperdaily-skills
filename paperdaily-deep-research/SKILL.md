---
name: paperdaily-deep-research
description: Deep, citation-grounded literature research on the user's own paperdaily corpus — the skill that actually READS the papers instead of listing them. Pulls a candidate pool from paperdaily (taxonomy facets / layered search / seed-paper similarity), downloads the full-text PDFs locally through an open-access waterfall, fans out an agent team for page-anchored deep reads, synthesizes a method comparison + timeline + taxonomy + a four-state evidence ledger, and can upload the analysis back to the paperdaily workbench. Use it whenever the user wants a research direction 深度调研 / 精读 / 做成文献综述, hands over a seed paper ("深读这篇论文", "--paper <id>"), pastes a deep-read command copied from the paperdaily web UI, asks what their agent 收件箱 has queued, or wants a review where every claim carries a paper id and page number. For a quick lookup instead — "今天有什么新论文", a one-shot field digest, an author's recent work — use the lighter `paperdaily` skill: this one downloads PDFs and spawns reading sub-agents, so it is the right pick only when depth is the point. Credentials come from ~/.paperdaily-cli/env.
---

# paperdaily-deep-research

> **声明块（SKILL_SPEC §1）**
> - **skill_ver**: `0.5.0`
> - **协议版本**: AGENT_PROTOCOL v1（`docs/api/AGENT_PROTOCOL.md`；检索走其 §2
>   分层端点，收件箱走其 §5 任务协议）
> - **所需 scopes**: `read:digest, read:paper, synth:ask`；阶段 0（可选收件箱）
>   另需 `read:reading`（缺失时跳过该阶段不报错）；领取任务（claim）与阶段 4
>   （可选回传）另需 `write:reading`（用户显式勾选，缺失时只读降级不报错）

从「一个研究领域 / 一篇种子论文」到「一份可溯源的深度文献综述」的流水线：
主干是检索 → 收敛 → 取全文 → 深读综合，两端各挂一个可选阶段（收件箱、回传）。
阶段间用落盘工件衔接（phase gate），可以从任意阶段进入——用户手头已有清单就
直接进阶段 2，已有 PDF 就直接进阶段 3。

```
阶段 0   收件箱（可选） pd_inbox.sh         → 用户在 web 排的任务 → 种子论文
阶段 1   检索+综述     pd_worklist.sh      → worklist.jsonl + overview.md
阶段 1.5 清单收敛      与用户一起挑         → worklist.selected.jsonl
阶段 2   全文获取      fetch_fulltext.py   → pdfs/*.pdf + fetch_report.jsonl
                     pd_browser_fetch.py    （--triage 先分诊；穷尽则交回 needs_web_search）
阶段 3   深度分析      agent team          → notes/*.md + synthesis/* + report.md
阶段 4   回传（可选）  upload_session.py   → paperdaily 工作台 reading session
                                             （--task-id 完结阶段 0 领取的任务）
```

统一工作目录约定（阶段 1 默认创建）：

```
pd-research/<slug>/
├── worklist.jsonl          # 候选池（id/title/doi/arxiv_id/oa_url/pdf_url）
├── worklist.selected.jsonl # 阶段 1.5 选中真正深读的那批（阶段 2 的输入）
├── worklist.twins.jsonl    # 折叠掉的孪生行审计（kept/dropped/title）
├── worklist.meta.json      # 检索目标 provenance 侧车（阶段 4 读取）
├── decisions.md            # 你替用户做过的判断（规模/选谁/为什么），随时可查
├── overview.md             # 阶段 1 的 LLM 领域综述
├── pdfs/                   # 阶段 2 下载的全文（永留本地，绝不上传）
├── fetch_report.jsonl      # 每篇的获取账本（命中层/失败原因/carrier/version；主键字段名是 `id`，不是 `paper_id`）
├── triage.jsonl            # `--triage` 的可达性分诊（阶段 1.5 选语料用）
├── needs_web_search.jsonl  # 自动层穷尽后交回给你的检索清单（见阶段 2）
├── notes/<paper_id>.md     # 阶段 3 逐篇精读笔记
├── synthesis/              # 四件套 + claims.jsonl
└── report.md               # 最终深度综述报告
```

## 开工前先把规模定下来

这条流水线的成本几乎全部线性于**深读几篇**：每篇一次 PDF 下载、一个精读子
agent、一段进入综合的上下文。所以在跑阶段 1 之前先用一句话跟用户对齐，不要
听到「深度调研 X」就直接点火：

> 我打算先从 paperdaily 拉一个 ~40 篇的候选池（约 1-2 分钟），跟你一起挑出
> 8-15 篇做全文精读，再出综合报告。精读这段是大头。这个规模合适吗？

一个可用的档位表（**深读篇数**，候选池比它大 3-4 倍即可）：

| 用户想要的 | 深读篇数 | 典型说法 |
|---|---|---|
| 摸个底 / 决定值不值得投入 | 5-8 | 「这个方向大概什么情况」 |
| 正经的方向综述 | 10-15 | 「帮我把这个方向理清楚」 |
| 就着一篇往外读 | 种子 + 6-10 | 「深读这篇，顺带看看相关的」 |
| 写论文 related work | 15-25（分批跑） | 「我要写这一章」 |

超过 25 篇就分批：跑完一批出报告，再用同一 `<slug>` 目录追加下一批。一次派
30+ 个精读子 agent，综合阶段拿到的是一堆读不完的笔记，报告质量反而下降。

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
scripts/pd_worklist.sh "Artificial Intelligence"           # 默认 --limit 25 --similar 2，折叠后 ~35-50 篇候选池
scripts/pd_worklist.sh 1702 --year-from 2025 --out ./pd-research/nlp/
scripts/pd_worklist.sh T10270 --limit 20 --no-synth        # Topic id 直连，跳过综述
scripts/pd_worklist.sh "graph neural fraud detection"      # 非 taxonomy 词 → /papers/search
scripts/pd_worklist.sh --paper arxiv:2605.10419            # 单篇种子模式
scripts/pd_worklist.sh "graph neural fraud detection" --append   # 把第二批并进同一个池子
```

第一个参数按形状解析：`T\d+`=Topic id、1-2 位数字=Field id、4 位数字=
Subfield id、其余按名字精确→子串匹配；全部 miss 时自动降级为
`GET /papers/search?q=&mode=auto` 自由词句检索（0.8.0+ 服务端）。做什么：

1. 主列表：taxonomy 命中走 `GET /papers`（facet 拉池 + `year_from` +
   `has_extraction=true`）；自由词句走 `GET /papers/search?q=&mode=auto`
   （stderr 会打出命中层 `layer_used`）；
2. 每篇 `GET /papers/{id}/similar` 做相邻扩展，合并去重成几十篇推荐池；
3. 每篇 detail 抽 `doi / arxiv_id / oa_url / resolved_pdf_url` 写
   `worklist.jsonl`——这些字段就是阶段 2 的全部输入；另写
   `worklist.meta.json` 侧车记检索目标；
4. 一次 `POST /ask`（用 `load_extractions` 锚定清单内论文）生成
   `overview.md`——这是本 skill 唯一一次 `/ask` 调用。

**`--paper <id>` 单篇种子模式**：`GET /papers/resolve?id=` 解析种子
（W-id / DOI / arXiv id 均可；404 = 未收录，报错并提示改用
`/papers/search` 找它）→ `GET /papers/{id}/similar?k=<limit>` 扩张成
worklist，种子在首行（`source:"seed"`）；`--similar` 在此模式下不生效。
适合「就着这一篇往外读」的场景（如 web 详情页「转给我的 agent 深读」）。
**注意 off-by-one，且它与折叠叠加**：`--limit` 只管相似扩张那一段，原始池
上限是 `limit+1`（种子 + limit 篇邻居），折叠又会**再砍掉**其中的孪生行
（这个语料上常见 20-30%）。想要 N 篇就往多了要，从 `--limit $((N*3/2))`
起步，然后看 `worklist rows written`——那是折叠后的数。

## 孪生行折叠（0.4.2）

同一篇论文会以多个 id 进池（图谱里是多个顶点）。脚本在写 `worklist.jsonl`
前做两趟折叠，并把每一次合并记进 `worklist.twins.jsonl` + 打到 stderr：

| 形态 | 例子 | 谁抓的 |
|---|---|---|
| arXiv id ↔ DOI 是 `10.48550/arxiv.X` 的 W 行 | — | 第一趟（硬标识） |
| arXiv id ↔ doi/arxiv_id 全空但 `oa_url` 是 arxiv.org/pdf 链接的 W 行 | `arxiv:2606.17449` ↔ `W7165219597` | 第一趟（硬标识） |
| 同一篇挂两个**不同** DOI（会议 DOI / arXiv DOI / Underline DOI） | MIRAGE 挂 `10.48448/…` 与 `10.18653/…` | 第二趟（标题归一） |

**服务端 `paper_group_id` 是补充信号，不是替代品**（0.8.10+ 起 `GET /papers/{id}`
随详情返回 `paper_group_id` + `is_canonical`，不额外花请求）：

- 它能看到客户端看不到的东西——**标题被改写过的跨版本重复**（工作论文 → 正式发表
  常改标题），这正是上面两趟折叠抓不到的那一类；
- 但它覆盖率约 64%，且分组本身仍在修（2026-07-28 实测样本 1/5，客户端同批 5/5）。
  **所以只能用它来「合并」，绝不能用它来「判定两篇不同」**——两行 group 不同不代表
  它们不是一篇。
- 正确用法：把 `paper_group_id` 相同**并入**客户端两趟折叠的结果（取并集），
  `is_canonical=false` 的那份优先丢弃。**不要因为有了官方字段就删掉客户端逻辑**。

**客户端折叠能保证什么、不能保证什么**（0.4.1 在这里给过一个兑现不了的承诺，别再信那句）：

- **能保证**：上表三类不会再重复占用阅读名额。
- **不能保证**：标题被改写过的跨版本重复（工作论文 → 正式发表常改标题），以及
  平台侧 `paper_groups` 才知道的版本链。所以 `worklist rows written`
  是**已知去重后的下界**，不是「保证互不重复」的断言。
- **绝不误并**：两行都有 `arxiv_id` 且号码不同时，即使标题一模一样也不合并
  ——不同的 arXiv 投稿就是不同的工作。
- 阶段 1.5 挑选时**顺手扫一眼标题**，看到疑似同一篇的再手工去掉一份。

值得这么较真的原因见阶段 3b 的三闸门表：一篇论文被算成两篇会把 `weak` 假升成
`supported`。

**`--similar` 别低于 2**：这个语料里一篇论文的最近邻常常就是它自己的孪生顶点，
`--similar 1` 会静默零扩张（脚本 0.4.2 起会为此明确告警）。调大 k **不增加请求
数**（每篇仍是一次 `/similar` 调用，只是返回列表更长），所以想要更宽的池子直接
给 3-4。`--similar 0` 是合法的，意思是完全不做相似扩张。

**追加批次**：`--append` 会把已有的 `worklist.jsonl` 并进这一轮再统一折叠，
用于「跑完一批再补一批」。不加这个参数会**覆盖**原文件。

第一个参数是长尾 Topic 名（不在全库最大的 500 个 Topic 里）时，脚本不再默认
去逐个子领域穷举扫描——那是 ~250 个请求、几分钟、且会吃光一分钟配额；它直接
落到 `/papers/search` 自由词句检索，通常一样找得到。确实需要那个 Topic id 时
加 `--deep-topic-scan` 强制扫（会明确告诉你扫了多少、失败多少）。

**成本与节奏**：`/ask` 免费档 **2/min**，一次 ~3-12k input token；多领域连跑
时除最重要的一个外都加 `--no-synth`。`read:paper` 免费档 **60/min（固定窗）**，
脚本按 `--throttle`（默认 1.05s）自节流并在 429 时按服务端 `Retry-After` 退避
（最多 3 次）。因此阶段 1 的墙钟主要是节流本身：默认参数约 80 秒，`--limit 30
--similar 2` 要 2 分钟以上。**不要为了快把 throttle 调小**——0.15s 那档正是
上个版本静默丢论文的原因。

**看到 `!! WORKLIST INCOMPLETE` 必须处理**：它表示有论文因取详情失败被丢掉，
清单短是限流造成的，不是这个领域小。如实告诉用户丢了几篇，重跑或调大
`--throttle` 后再进下一阶段——**不要拿一份缺行的清单当作检索结果去做综述**。

**看到「stopped at the title layer」也要处理**：自由词句检索的服务端瀑布只要
标题层有命中就不再往语义层走，于是一个很大的方向可能只回来几行。这时池子薄
不代表语料薄。换更短/更宽的词重跑，或者拿最好的那篇当种子 `--paper <id>` 去
扩张（0.4.2 起脚本会主动提示）。

**Phase gate → 阶段 1.5**：`worklist.jsonl` 存在、每行合法 JSON 且
`doi`/`arxiv_id`/`oa_url` 至少一项非 null 的行占多数、**没有 INCOMPLETE 横幅**，
**且池子 ≥ 计划深读篇数的 2 倍**——最后这条是新加的：只看「行数 ≥ 目标的 8 成」
会让一个 7 行的池子为 6 篇的目标开绿灯，阶段 1.5 就没什么可收敛的了，收敛这一
步的价值全在于有得挑。不满足先修阶段 1，不要带病进入下一阶段。

## 阶段 1.5 — 清单收敛（候选池 → 真正深读的那批）

阶段 1 给的是**候选池**，不是阅读清单。相似扩张来的尾巴通常有相当比例是噪声，
而后面每一篇的代价是一次下载 + 一个精读子 agent + 一段综合上下文。所以在下载
之前先收敛——这是整条流水线上性价比最高的一个动作。

做法：

1. 读 `overview.md`（它已经点了「最值得作为起点的 2-3 篇」）+ `worklist.jsonl`
   的标题/年份/venue，给用户一份紧凑清单，标出你建议选的那些和一句话理由。
2. 选择标准按这个顺序：与用户真实问题的贴合度 > 覆盖不同流派/方法路线 >
   时间跨度（要有早期奠基和最新进展）> 引用量或 venue。**不要只按相似度取前 N**，
   那样选出来的是一堆彼此重复的论文，综合阶段会无话可说。
3. 让用户增删，然后落盘。**id 打错一个字符，`select` 只会让文件变短、不会报错**，
   所以这个配方自带核对——没匹配上的 id 会打出来：

   ```sh
   cd pd-research/<slug>/
   SEL='["arxiv:2409.10897","W4416083181"]'          # ← 选中的 id
   jq -c --argjson sel "$SEL" 'select(.id as $i | $sel | index($i))' \
     worklist.jsonl > worklist.selected.jsonl
   # 核对：期望数 vs 实得数，并列出没匹配上的 id
   jq -n --argjson sel "$SEL" --slurpfile got worklist.selected.jsonl '
     ($got | map(.id)) as $ids
     | {wanted: ($sel|length), got: ($ids|length),
        unmatched: ($sel - $ids)}'
   ```

   `unmatched` 非空就是写错了（或那篇本来就不在池子里），先修再往下走。

4. 阶段 2 用 `--worklist worklist.selected.jsonl` 跑（`worklist.jsonl` 原样保留：
   阶段 4 的幂等指纹按它算，代表这一轮检索的完整 provenance）。

用户明确说「全都要」时照办，但先把篇数和大致耗时说出来。

**把判断写进 `decisions.md`**：规模定在几篇、为什么选这些不选那些、遇到疑似
重复怎么处理的。这条流水线会跑很久，用户回来看结果时往往已经不记得当时同意
了什么；`report.md` 讲的是论文，`decisions.md` 讲的是你。

**注意 `overview.md` 是收敛之前生成的**——它描述的是整个候选池，不是最终选中
的那几篇。写 `report.md` 时不要把它当成对最终清单的综述直接引用；`/ask` 免费档
2/min，通常不值得为收敛后的清单再生成一次，但要在报告里说清这个时间差。

**worklist 行的规范形状**（手工加行时照这个填，别少字段——阶段 2 只认这三个
定位字段，缺了就只能落 L7 人工兜底）：

```json
{"id":"arxiv:2409.10897","title":"…","publication_date":"2024-09-17","venue":null,
 "doi":null,"arxiv_id":"2409.10897","oa_url":"https://arxiv.org/abs/2409.10897",
 "source":"seed"}
```

- `id` — 全流程主键，笔记正文的 `**paper_id**` 必须与它逐字一致；
- `doi` / `arxiv_id` / `oa_url` / `pdf_url` / `urls_extra` — 阶段 2 的**全部**
  输入，至少一项非 null；缺失字段写 JSON `null`，**不要写空字符串**；
  `pdf_url` 由阶段 1 从 v1 的 `resolved_pdf_url` 带出（老服务端为 null），
  `urls_extra` 是数组，由你跑完网页检索后回填（见阶段 2「检索回填回路」）；
- `arxiv_id` 是裸号（`2409.10897`，可带 `v2`），不带 `arxiv:` 前缀；
- `source` — `primary` / `similar` / `seed`，只用于阅读顺序，不影响取全文。

## 阶段 2 — 全文获取（分诊 → 瀑布 → 检索回填 → 浏览器）

**先分诊，再决定语料。** 直接对整份清单跑下载，是把「这批能不能拿到」的答案
推迟到选完语料之后——顺序反了。分诊只跑查询层、不下载任何 PDF：

```sh
export UNPAYWALL_EMAIL="you@example.org"          # 免费，只发给 polite-pool API（见下）

python3 scripts/fetch_fulltext.py --worklist pd-research/<slug>/worklist.jsonl \
    --out pd-research/<slug>/pdfs/ --triage
```

产出 `triage.jsonl`，每篇一个可达性标签：

| 标签 | 含义 | 下一步 |
|---|---|---|
| `direct-pdf` | arXiv 号，或已有 `urls_extra` | 直接下 |
| `oa-pdf` | Unpaywall 给了真正的 OA PDF | 直接下 |
| `title-search` | 找到了同一篇的**开放兄弟版本**（工作论文/会议稿） | 直接下，但注意版本差异 |
| `repo-landing` | 兄弟版本只有记录页 | 下载时多一跳，可能失败 |
| `browser-needed` | 只有出版商链接，没定位到开放副本 | 要么放弃，要么走浏览器层 |
| `paywalled-only` | 所有查询层都没有 | 每篇要花一次网页检索 |

拿这张表和用户一起按**相关性 × 可得性**定阶段 1.5 的语料。经验判据：
`paywalled-only` 占比超过一半时，与其硬啃这一批，不如回阶段 1 换检索面——
一次网页检索一篇的成本，乘以 10 篇就是整个流程最贵的一段。

然后正式跑：

```sh
# export PD_FETCH_INSTITUTIONAL=1                 # 见下方合规边界
# export PD_FETCH_CDP=1                           # 用你自己的 Chrome 会话，见下
# export ELSEVIER_TDM_KEY=… WILEY_TDM_TOKEN=… S2_API_KEY=…   # 可选

python3 scripts/fetch_fulltext.py --worklist pd-research/<slug>/worklist.selected.jsonl \
    --out pd-research/<slug>/pdfs/ --jobs 4
```

（跳过了阶段 1.5 就传 `worklist.jsonl`；但候选池超过 20 篇时先回去收敛。
账本 `fetch_report.jsonl` 的路径由 `--out` 的父目录决定，两种输入都落同一处。）

**墙钟**：每主机 1s 节流不变，`--jobs N` 只在**不同主机之间**并行（10 篇分散在
10 个站点时实测 87s → 35s）。单篇 PDF 常见 2-8 MB，跨境带宽差时仍可能十几分钟。
断了直接重跑：已下好的 PDF 判 `already` 跳过（幂等）。

### 检索回填回路（这一层承重，别跳）

瀑布的 L0-L4b 全部按 DOI / arXiv 号定位。对**闭源期刊的经管法**论文这在构造上
就是死路：DOI 只登记在付费的正刊版上，于是 Unpaywall、PMC、乃至这篇自己的
OpenAlex `best_oa_location` 全都如实回 `miss`。2026-08-02 那次 10 篇全灭的实测：
8 个 DOI 里 5 个完全没有 OA 位置，2 个指向 SSRN 落地页——**配上
`UNPAYWALL_EMAIL` 最多也只能救回其中一篇**。

但开放副本通常是存在的，只是以**另一个 work** 的身份：SSRN / NBER / 会议 /
作者主页的工作论文，有自己的 id、自己的（不同的）标题。学术聚合器不一定收录，
通用网页检索能找到——那次最终用的 6 篇里，`acfr.aut.ac.nz`（会议稿）、
`efmaefm.org`、`sfmitali.gitlab.io`（作者主页 JMP）都是这么来的，而一次标题检索
就能命中第一条。

脚本跑不了通用网页检索（DDG/Bing 对脚本客户端软封锁，keyless S2 首请求就 429），
**但你能**。所以穷尽后它把活交回来：

```sh
cat pd-research/<slug>/needs_web_search.jsonl
# {"id":"hhag020","title":"…","suggested_query":"\"…\" filetype:pdf", …}
```

1. 对每行跑**一次**网页检索（用 `suggested_query`）；
2. 把找到的 PDF 或记录页 URL 写回该论文的 worklist 行：
   `"urls_extra": ["https://…/paper.pdf"]`；
3. 重跑同一条命令——已下好的跳过，只补这几篇。

一篇一次检索，**不要为一篇论文连着搜三轮**；两次没有就标 `paywalled-only`
交给用户定夺。

⚠️ **命中的多半是另一个版本。** 账本会写 `version: "alternate"` 和
`matched_title`。页码、样本量、表号在版本之间不通用——精读笔记必须记清读的是
哪一版（见 reading-note-template.md「版本铁律」），拿工作论文的数字去描述正刊
结论是实打实的错误陈述。

### 浏览器层（用你自己的 Chrome 会话）

上面所有层说的都是普通 HTTP 客户端能看到的互联网：Cloudflare 挡的期刊回 403，
校园代理/SSO 后面的内容回登录页，靠 JS 拼下载链接的仓储回 HTML。**你的浏览器
这三样都能看**，因为 cookie 在它那儿。`pd_browser_fetch.py` 借的就是这个会话。

一次性设置：打开 `chrome://inspect/#remote-debugging` 把开关打开（不用重启
Chrome，也不用装任何东西）。**必须用你日常那个 profile**——新开的干净 profile
没有任何会话，那就白设了。然后：

```sh
python3 scripts/pd_browser_fetch.py --list            # 确认连得上
python3 scripts/pd_browser_fetch.py --url "https://doi.org/10.1093/rfs/hhag020" \
    --out pd-research/<slug>/pdfs/hhag020.pdf
# 或整批：--worklist … --out pdfs/   （产出 browser_fetch_report.jsonl）
# 或让瀑布自动用它：export PD_FETCH_CDP=1
```

碰到人机验证或登录墙时，脚本**停下来等你在浏览器里点完**（默认 180s，
`PD_CDP_WAIT_HUMAN` 可调），然后自己继续。agent 不解验证码，也解不了——
这一步只能是你。一个域名过一次，通常够覆盖整批。

**不要用截图点按钮的方式做这件事**：Chrome 能把 PDF 显示出来而字节不在 DOM 里，
点"下载"进的是浏览器自己的下载队列、调用方拿不到路径。2026-08-02 卡掉 22 分钟
就是卡在这里。真正出字节的只有两条机制，脚本两条都用：落地页上下文里的
同源 `fetch(credentials:'include')`，和 `Browser.setDownloadBehavior` + 下载完成
事件。浏览器路线**最多试 3 分钟**，不出字节就转公开仓储/作者稿/网页检索。

**中途怎么看进度**：`[i/n]` 进度行走 stderr（0.4.1 起逐行 flush）。但如果你把
输出重定向到文件，**最可靠的进度信号是 `fetch_report.jsonl`**——它每完成一篇就
落盘并 flush，`wc -l` 一下就知道走到第几篇了。别因为日志文件没动就判定卡死。

瀑布逐层尝试、命中即停：arXiv 直连 → **paperdaily 自己已解析出的 `pdf_url`** →
API 给的 `oa_url`（是裸 doi.org 的直接跳过，那不是 OA 链接）→
你回填的 `urls_extra` → Unpaywall → PMC/Europe PMC →
出版商 TDM API（有 key 才走）→ **标题检索开放兄弟版本** → 机构订阅直连（opt-in）
→ **你自己的 Chrome 会话**（opt-in）→ Playwright 兜底（opt-in）→
落 `needs_web_search.jsonl` 交回给你。细节与排查见
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
- `PD_FETCH_CDP=1` 用的是用户**自己的浏览器、自己的登录、自己的权限**——
  与上一条同一条线，只是更靠前一步。它不接触密码、不导出 cookie、
  **不解验证码**（检测到就停下来等人工）。
- 本 skill 不含、也不会添加 Sci-Hub 等绕过付费墙的渠道。
- 账本里 `denied` 意味着「请求了但被拒（大概率无订阅权限）」，不要
  当成技术故障反复重试。

**Phase gate → 阶段 3**：账本里判为全文（`carrier` 是 binary-pdf /
html-fulltext / parsed-fulltext 之一，或老账本的 `status` 是 ok/already）覆盖
**这一轮输入清单**的 6 成以上即可推进（付费墙论文拿不到是常态）；把失败
清单和原因明确报给用户，问是否人工补齐（账本里有 doi.org 链接），
不要沉默丢弃。全文缺失的论文在阶段 3 降级为「仅摘要参与综合」，
且笔记与 claims 里必须标注 abstract-only。

**「拿到全文」不等于「存下了 PDF 二进制」**：把公开 PDF 完整解析成保留页序的
Markdown 来精读，是合法的全文载体。这种情况在账本里写
`{"id":…, "status":"ok", "carrier":"parsed-fulltext", "url":…}`（可以单独写进
`fulltext_report.jsonl`，阶段 4 会一起读），不要另建一套账本，也不要因为
`pdfs/` 里没有文件就把它降级成 abstract-only。前提是**页序真的保留了**——
没有页标记的解析结果按 `anchor-degraded` 处理。

## 阶段 3 — 深度文献分析（agent team）

进入前校验：`pdfs/` 有文件、每个 PDF 前 4 字节是 `%PDF`（阶段 2 的
脚本已保证）、清单与 PDF 能按 `<paper_id>.pdf` 对上。

**允许与阶段 2 重叠**：链路慢的时候，已经下好的论文可以先派出去精读，不必等
整批下完——`fetch_report.jsonl` 是逐篇 flush 的，可以拿它当就绪信号。**但
3a→3b 的 gate 仍然按整批算**（笔记数 = 本轮参与深读的篇数），不能因为先开工
就提前进综合。

这是全流水线唯一没有脚本兜底的一段，质量全靠派工说清楚。**动手前先读
[references/deep-read-agent-prompt.md](references/deep-read-agent-prompt.md)**
——里面有现成的派工模板、PDF 读法规程和子 agent 回传契约，直接复制着用，
不要自己现编提示词。

### 3a 逐篇精读（fan-out，sonnet）

**一篇论文一个子 agent**，model 用 `sonnet`，提示词用上面那份模板。
每篇产出 `notes/<paper_id>.md`。三条铁律：

- **页码必须来自保留分页的读法**（Read 工具带 `pages` 参数，或
  `pdftotext -layout` 数换页符）。用丢掉分页的方式读完再回填页码，等于
  编造锚点——这是本 skill 最容易悄悄失守的一条。
- 每个字段锚定 PDF 具体位置（`(p.X)` / `(p.X, Fig.Y)` / 最低 `(§N.M)`），
  禁止「论文说」；拿不到可靠页码就标 `anchor-degraded` 并如实降级。
- 子 agent **不要把笔记正文回传**，只回 paper_id / note_path / depth /
  pages_read / anchor_coverage / 3 条 headline / unresolved。笔记已经在磁盘上，
  正文回传只会挤掉综合阶段的上下文预算。

回传的 `anchor_coverage` 比值低于 0.8、或 `depth` 与 fetch 账本矛盾（账本说 `ok`
却回了 abstract-only）的，**重派该篇**，不要在综合阶段补。子 agent 回了"全部字段
已锚"这类话而不是数字比值时，先要比值——这条规则的价值就在于它是机械的。

### 3b 跨篇综合（opus / 会话主模型）

全部笔记就绪后（phase gate：`notes/` 文件数 = 本轮参与深读的论文数，
含 abstract-only 的那些），按
[references/synthesis-templates.md](references/synthesis-templates.md)
产出 `synthesis/` 四件套 + claims 账本：

1. 方法对比表、2. 时间线、3. 流派/分类法、4. 矛盾点与研究空白清单；
5. `claims.jsonl` —— 每条跨篇论断挂证据（论文+页码）与四态
   （supported/weak/contested/gap），**无证据必须标 gap，禁止用模型
   常识补全**。

写 claims 时一次性对齐三条闸门，别等到阶段 4 才发现要返工：

| 闸门 | 判据 | 不过会怎样 |
|---|---|---|
| 本地回传门 | 证据非空率 ≥ 80% | `upload_session.py` 直接拒传 |
| 服务端计分门 | **每一条非 gap 的 claim 都要带证据** | 传得进去但不计积分（静默） |
| 发芽 | gap/contested 会原样进用户的问题收件箱 | 写几条就给人塞几条问题 |

最省事的做法是按最严的那条来：**非 gap 的一律配齐证据**，gap 的写成用户真会
想回答的问题。

综合难度高，用 `opus`（或不指定 model 由主会话直接做）。最后按模板的
报告结构整合成 `report.md`，正文论断随文标 `[paper_id p.X]` 引用。

**`report.md` 这个文件名会撞上一些 agent 运行时的写入护栏**（有的 harness 禁止
子 agent 写 report/summary/findings 命名的文件）。撞上了就用 shell heredoc 写同一
个路径，不要改名——`upload_session.py` 的回传门按 `report.md` 找它，改名等于过不了门。

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
（SKILL_SPEC §4 同意点），默认姿态是只读。向用户说清这五点再问——
**后两点是服务端会替他做的事，不说清就不算知情同意**：

1. 上传的只有你自己的派生分析（笔记/论断账本/报告），**绝不含 pdfs/
   与任何二进制**——原文永留本地（版权红线，AGENT_PROTOCOL §7）；
2. 幂等：同一 worklist 重复上传返回既有 session（`deduplicated:true`），
   不会产生重复条目；
3. 需要 key 带 `write:reading` scope（额度 10/min、100/天，每人最多 100 个
   session）；
4. **「发芽」**：`gap` / `contested` 的 claim 会被服务端**原样复制**进他的
   开放问题收件箱——你写几条，他就收到几条（详见阶段 3b 的三闸门表）；
5. **积分**：过门的上传计一次积分，fulltext 笔记另有每篇加成，幂等重传不重复
   计；服务端计分门比本地回传门严，可能传成功但不计分（同上表）。

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
不要绕行）。

两个必须转达、不能吞掉的响应细节：

- **`unknown_paper_ids` 非空**：服务端在图谱里找不到这些 id。笔记和 claims
  照常入库，但引用是悬空的。最常见的原因是笔记里的 `**paper_id**` 写成了安全化
  文件名（`arxiv_2409.10897`）而不是原始 id（`arxiv:2409.10897`）。脚本会打印
  这份清单——**核对后修正笔记重传**（幂等，不会产生重复 session），不要当没看见。
- **429 有两种**：带 `X-RateLimit-*` 头的是限流（脚本已自动退避重试）；不带的
  是「每人 100 个 session」的上限，重试无用，要去工作台删掉旧会话。脚本已经
  按这个区分处理并给出对应提示。

其余错误如实转告用户。

## 本 skill 不做什么

- 不改用户的 paperdaily 画像（follow/feedback 等 `write:profile` 操作）。
- 不管理 API key（签发/吊销走 web UI）。
- 不提供任何绕过付费墙的获取渠道。
- 不静默上传：阶段 4 只在用户明确同意后执行，且永不上传 PDF/二进制。

## Files

- `scripts/pd_inbox.sh` — 阶段 0：agent 任务收件箱（列取 pending / `--claim` 领取；缺 scope 软降级，自包含 bash+curl+jq）
- `scripts/pd_worklist.sh` — 阶段 1：taxonomy/查询/种子解析 + 推荐池 + 综述（自包含，bash+curl+jq）
- `scripts/fetch_fulltext.py` — 阶段 2：零依赖瀑布下载器（`--triage` 分诊 / `--jobs` 跨主机并发 / 标题检索层 / `needs_web_search.jsonl` 回路；playwright 可选）
- `scripts/pd_browser_fetch.py` — 阶段 2 浏览器层：借用户已有的 Chrome 会话取全文（纯 stdlib，内嵌 WebSocket 客户端，不依赖任何 MCP，任何 agent 都能 `python3` 调）
- `scripts/upload_session.py` — 阶段 4：phase gate + 回传（纯 stdlib，绝不上传 pdfs/）
- `references/deep-read-agent-prompt.md` — **阶段 3a 派工规程**：PDF 读法（页码从哪来）+ 子 agent 提示词模板 + 回传契约。派工前必读
- `references/reading-note-template.md` — 阶段 3a 精读模板、锚定铁律与三个降级标记
- `references/synthesis-templates.md` — 阶段 3b 四件套 + claims schema + 报告结构
- `references/fulltext-sources.md` — 全文渠道手册：层级/配置/判据/排查/合规
