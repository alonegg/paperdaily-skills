# 语义检索手册（agent 向）

> 面向「用 agent 在 paperdaily 语料上找论文」的操作规范。
> 全部数字为 **2026-08-18 在生产环境实测**，不是估计值。
> 上游契约：`AGENT_PROTOCOL.md` §2 分层检索。

## 0. 先选通道（决策表）

| 用户诉求 | 走哪条 | 端点 |
|---|---|---|
| 「这个 DOI / arXiv 号 / W-id 是哪篇」 | 精准指向 | `GET /papers/resolve?id=` |
| 「找一下这篇论文」（给了完整标题） | 标题层 | `GET /papers/search?q=<标题>&mode=title` |
| **「XX 方向最近有什么」（方向不是 taxonomy 节点）** | **语义层** | `GET /papers/search?q=…&mode=semantic` |
| **「跟这几篇像的论文」** | **相似 + 语义并集** | `/papers/{id}/similar` ∪ `mode=semantic&q=<该篇标题>` |
| 「AI 这个学科里最新的」（是 taxonomy 节点） | 分面拉池 | `GET /papers?subfield_id=…&sort=recency\|novelty\|citations` |
| 要一段成文综述 | 服务端合成 | `POST /ask`（**只在这一种情况用**，见 §9） |

判「是不是 taxonomy 节点」不用猜：`/taxonomy/subfields` 一次拉回全部 ~250 个
（无 `q=` 参数，自己在本地匹配名字，结果可整会话复用）。匹配不上就是模糊方向，
走语义层。

---

## 1. 铁律：模糊方向一律 `mode=semantic`，不要用默认的 `mode=auto`

`mode=auto` 的瀑布是 标识符 → **标题 trgm** → 语义，而**标题层只要凑够 3 条命中
就被采纳、语义层再也不会执行**。于是一个概念型查询经常拿回三条字面撞词的结果。

实测（同一查询，同一时刻）：

| 查询 | `mode=auto` | `mode=semantic` |
|---|---|---|
| `mixture of experts routing` | 落 title 层，**19.6s / 3 条** | **5.1s / 20 条** |
| `survey attrition weighting` | 落 semantic，但**先白付了标题层 9.8s** | **0.55s / 20 条** |
| `causal inference with panel data staggered adoption` | 落 semantic，**23.6s** | **1.5s** |
| `in-context learning` | 落 title：`Is In-Context Learning Learning?` / `Schema for In-Context Learning`×2 | `Learning To Retrieve Prompts for ICL` / `An Explanation of ICL as Implicit Bayesian Inference` |

两个后果叠在一起：**又慢又差**。标题层冷缓存实测 15-30s（`took_ms` 服务端自报，
不是网络），语义层冷 4-5s、热 0.4-1.8s。

> 「响应慢」的投诉里，绝大部分是这一条 —— 不是服务器忙，是 agent 让服务器先跑了
> 一遍注定要丢弃的标题层。

**唯一该用 `mode=title` 的场合**：用户手里就是一个完整标题，要定位那一篇。
`mode=auto` 留给「不知道用户给的是 id 还是词」的通用入口。

---

## 2. 铁律：查询要写成一句话，不能是关键词

语义层是「查询向量 → 向量召回 → **按 dominant topic 共识重排**」。查询太短时向量
落在语义空间的模糊地带，dominant-topic 那一步会把整批结果拽进一个错误的簇，并且
**看起来很正常**（有分数、有 `why`、有 20 条）。

实测同一概念的四种写法：

| 查询 | top-1 | 分数 | `why` |
|---|---|---|---|
| `chain-of-thought`（1 词） | *JOINING UP THE THINKING* | 0.525 | dominant cluster **[Educational Leadership and Practices]** |
| `chain-of-thought prompting`（2 词） | *JOINING UP THE THINKING* | 0.542 | 同上 |
| `chain-of-thought prompting elicits reasoning in large language models`（8 词） | **CoT 原论文** | **0.759** | dominant cluster [Topic Modeling]; cited by 2629 papers |
| `step by step reasoning traces improve arithmetic accuracy of language models`（11 词，不含术语） | **CoT 原论文** | 0.685 | 同上 |

注意第四行：**不用行话、只描述现象也能命中**——这正是语义检索该被用来做的事。
反过来，把术语砍成两个词是最糟的用法。

**中文可以直接查**（bge-m3 多语言）：`LLM 在教育中的应用` 走语义层拿回 20 条英文
论文，而同一个词走标题层是 **0 条**。中文诉求千万别落到标题层。

---

## 3. 判「这次检索没打中」的判据

不要凭结果条数判断——没打中的时候条数一样是满的。两个信号都在响应里、零额外
请求，但**强度差很多**：

**强信号（主判据）：`why` 里的 `in dominant cluster [X]`。** X 不是你要的领域就是
没打中，没有例外。这一条只能由你（知道用户想要哪个领域）来判，任何阈值都替代不了。

**弱信号（次判据）：top-1 `score`。** 实测分布：

| 查询 | score | dominant cluster | 实际 |
|---|---|---|---|
| `chain-of-thought` | 0.5248 | [Educational Leadership and Practices] | **没打中** |
| `chain-of-thought prompting` | 0.5419 | [Educational Leadership and Practices] | **没打中** |
| `mixture of experts routing in sparse transformers` | 0.5936 | [Domain Adaptation and Few-Shot Learning] | 打中（8/8 全是 MoE 论文） |
| `graph` | 0.6102 | [Advanced Graph Theory Research] | 打中，但**查询过泛** |
| `differentially private stochastic gradient descent` | 0.6435 | [Privacy-Preserving Technologies] | 打中 |
| `teacher professional development and student achievement` | 0.6456 | [Technology-Enhanced Education Studies] | 打中 |
| `spectral clustering on hypergraphs` | 0.6576 | [Complex Network Analysis] | 打中 |
| `retrieval augmented generation reduces hallucination in QA` | 0.6638 | [Topic Modeling] | 打中 |

miss ≤0.542、hit 从 0.5936 起 ⇒ **阈值取 0.56**（落在实测空档里）。

⚠️ **这个阈值先前定成 0.60，上线第一次跑就误伤了**——`mixture of experts routing
in sparse transformers` 拿 0.5936 被判 miss，而它 8 条结果全对。**0.60 卡在 hit
区间里，不在空档里。别再往上调。**

⚠️ **分数高不代表查询够具体**：`graph`（1 个词）拿 0.6102、cluster 也对，但这种
查询对任何具体问题都没用。**「太泛」这一类没有任何阈值抓得到**，只能靠 §2 那条
纪律（写成一整句）在源头避免。

命中时 `why` 还会给 `cited by N papers` / `recent`，可以直接用来向用户解释推荐
理由（模板生成、非 LLM 编造）。

处置：把查询改写成更长更具体的一句话重跑，或者改用 §5 的探针定 scope。

---

## 4. 铁律：一个查询向量只是一个点，模糊方向必须扇出

对**同一个问题**的四种改写，各取 top-20：

```
large language models for personalized learning
LLM 个性化学习 自适应教学
AI tutor adaptive instruction student outcomes
generative AI in classroom teaching practice
```

结果：**union = 80 篇，两两重叠 0 篇**。四个列表完全不相交。

这意味着两件事，都要记住：

- **扇出不是冗余劳动**，是覆盖一个方向的唯一办法。一次查询只能看到这个方向的
  一个侧面（上例里中英文改写分别落在 subfield 1702 和 1706）。
- **不要指望「多个改写都命中 = 核心论文」这种共识排序**，在这个语料上共识集通常
  是空的。RRF 融合此时退化成按秩轮转合并——那正是想要的行为，但别把它读成投票。
  真正会出共识的是 §6 的 seed 双通道。

改写由 agent（也就是你）来写，服务端不做查询扩展。写 3-4 条，覆盖：中文/英文、
术语/白话、方法侧/应用侧。

---

## 5. 探针定 scope：先无 scope 探一次，再收窄

`subfield_id` / `field_id` / `year_from` / `year_to` 会把过滤**下推进向量召回**
（不是召回后过滤），所以 scope 是提质的，不是省钱的。但你通常不知道该填哪个 id。

做法：先不带 scope 跑一次 `limit=30`，看返回结果的 `subfield_id` 分布——这个分布
就是答案。实测非常干净：

| 探针查询 | subfield 分布 |
|---|---|
| `staggered difference-in-differences estimator` | **30/30 → 2613**（Statistics & Probability） |
| `retrieval augmented generation hallucination` | **30/30 → 1702**（Artificial Intelligence） |

然后带 `subfield_id=<那个>` 重跑，两批并集就是池子。
`scenarios/semantic-search.sh --scope auto` 就是这两步。

**scope 的代价**：稳态下可以忽略（同一查询实测 unscoped 653ms / subfield 634ms /
field 616ms / year_from 747ms 中位数）。真正贵的是**冷缓存**——同一个 case 里出现
过 15.5s 的冷启动离群值。**不要把偶发的慢归因到 scope 参数上**。

---

## 6. 种子论文找相似：单个种子是一个窄锥

`/papers/{id}/similar` 是 SPECTER2（缺失时退 BGE-M3）的 kNN，快且稳（实测 1.1-1.8s，
同一种子重复调用**结果完全同序**，检索本身是确定性的）。

但它的邻域比直觉窄得多。实测某篇论文的邻居距离：

| 排名 | 1 | 10 | 30 |
|---|---|---|---|
| 余弦距离 | 0.017 | 0.024 | 0.027 |

而**同一篇论文的两条记录**（arXiv 版 + OpenAlex W 版）之间的距离是 **0.0399**。
也就是说 top-30 覆盖的半径，比「同一篇论文的两个副本」之间的距离还小。后果：

- 两条同一篇论文的记录，各自的 top-30 邻居 **零重叠**；
- GPT-3 和 CoT 这两篇明显相关的论文，各自 top-30 邻居也 **零重叠**。

所以**单个种子 + `/similar` 拿不到「相关工作」，只拿到「最贴的那一小簇」**。正确做法
是并两条通道：

1. `GET /papers/{id}/similar?k=…` —— 向量邻域；
2. `GET /papers/search?q=<该篇标题>&mode=semantic` —— 文本语义层。

这两条实测 **20 条里只重合 1 条**，是互补而非冗余的。并起来之后跨通道命中 ≥2 的
论文就是真正的核心件——实测种子 = {CoT, GPT-3} 时，*Large Language Models are
Zero-Shot Reasoners* 被 4 条通道里的 3 条命中，排在第一。

多种子时逐个种子跑这两条，再按命中通道数排序。

**别忘了排除种子自己**：种子会以**另一个 id**（孪生记录）回到结果里，只按 id 排除
挡不住，要按归一化标题再排一次。

---

## 7. `k` 超过 38 是无效的

`/papers/{id}/similar?k=` 上限写着 50，但 HNSW 的 `ef_search` 默认值把实际返回数
截在 ~38：

```
k=10 -> 10 条
k=30 -> 30 条
k=40 -> 38 条
k=50 -> 38 条     ← 不是语料没有了，是索引参数
```

要更宽的池子，加**种子**或加**改写**，不要加 `k`。

---

## 8. 孪生行折叠（拿到结果之后立刻做）

同一篇论文在图谱里有多个顶点（arXiv 记录 / OpenAlex W 记录 / 期刊记录），检索结果
里会各占一行。判据按可靠性排序：

1. 归一化标题相同（去大小写、去标点空白）→ 合并；
2. `arxiv_id` ↔ DOI 形如 `10.48550/arxiv.X` → 合并；
3. 详情里的 `paper_group_id` 相同 → 合并，但**只能用来合并、不能用来判定两篇不同**
   （覆盖率约 64%）；
4. **两行都有 `arxiv_id` 且号码不同 → 绝不合并**，即使标题一样。

不折叠的代价不只是名额浪费：下游做证据计数时，一篇论文被算成两篇会把「单一来源」
误升成「多来源互证」。

---

## 9. `/ask` 该用与不该用（不是"agent 别碰"）

先说清楚定位：**`/ask` 是合成端点，不是检索端点。** 「agent 不走 /ask」这句话在协议
里是针对**检索**说的，不是说 agent 不能用它。用错的是工种，不是这个端点本身。

### 成本

生产实测（`query_metrics`，近 90 天 n=122）：

| 指标 | 值 |
|---|---|
| 总延迟 p50 | **37.3s**（7 月 46.5s / 8 月 56.8s，逐月在涨） |
| p90 | 95s（8 月有 945s 的离群） |
| 其中工具循环 / LLM | 18.1s / 23.1s，平均 4 次工具调用 |
| 排队等 slot | ~0（不是拥塞问题） |

加上：v1 **无流式**（只能等整段返回）、10 积分/次、免费档 2 req/min。

### 什么时候不该用

**当检索通道用**。你自己就是 LLM，「自然语言→工具调用」和「工具结果→散文」这两段
对你是净损失：慢一个数量级、有损、多一个幻觉面。要论文清单就走 §0 那张表的结构化
端点。

### 什么时候该用（这些是正当用途）

1. **合成一段面向人的综述**——你已经用结构化端点选定了一批论文，要一段成文的、
   带引用的叙述交给用户。这正是两个 skill 各自那一次调用。
   **必须锚定**：让它 `load_extractions(paper_ids=[…你已检出的 id…])`，否则它会自己
   跑 `find_papers_by_keyword_semantic`，拿回一批 2018-2022 的综述当材料。
2. **够到 REST 没开的三个工具**（下表）。这三件事目前**只有 `/ask` 这一个门**。
3. **把合成成本转移到服务端**——`/ask` 烧的是服务端 token 与积分，不是你的上下文。
   要把 40 篇论文的抽取压成一段话时，这个转移是划算的。

### 工具覆盖审计（15 个 query-bridge 工具 → REST 门）

`/ask` 内部能调的工具与 v1 REST 的对应关系（2026-08-18 逐个核对 openapi.json）：

| query-bridge 工具 | REST 等价物 |
|---|---|
| `find_papers_by_keyword_semantic` | `GET /papers/search?mode=semantic` |
| `find_papers_by_topics` | `GET /papers?topic_ids=&topic_match_op=` |
| `find_papers_by_authors` | `GET /authors/{id}/papers` |
| `find_similar_papers` | `GET /papers/{id}/similar` |
| `find_citing_papers` / `find_references` | `GET /papers/{id}/citations?direction=in\|out` |
| `lookup_field` / `lookup_subfield` / `lookup_topic` | `GET /taxonomy/{fields,subfields,topics}` |
| `lookup_author` | `GET /authors?q=` |
| `count_papers` | `GET /papers/_count?group_by=` |
| `load_extractions` | **`POST /papers/batch`（≤100 id / 次，比工具更好用）** |
| **`find_community_overview`** | ❌ **无** |
| **`find_papers_by_venue`** | ❌ **无** |
| **`lookup_venue`** | ❌ **无** |

两个要点：

- **别为了拿抽取去调 `/ask`。** `POST /papers/batch` 一次 100 个 id 就把
  `contributions / key_claims / methods / limitations / open_questions / tldr_zh`
  全给你了（`GET /papers/{id}` 也内联同样的字段）。这是 12 个有 REST 门的工具里
  最容易被误以为「只有 /ask 能做」的一个。
- **`find_community_overview` 是 GraphRAG 式的预计算领域概览**（读
  `topic_community_summaries`，Field/Subfield 各一条摘要 + 代表论文 id），
  **零 LLM 成本却只能从最贵的端点进**。要「这个领域大致什么情况」时，它比你自己
  拉 30 篇再总结要准得多。⚠️ 但**先看 `refreshed_at`**：生产上 278 行全部停在
  **2026-05-06**，没有 timer 在刷。当成「几个月前的领域快照」用，别当最新动态。

---

## 10. 配额与节奏

- `read:paper` 免费档 **60/min 固定窗**。扇出 4 条 + scope 重跑 + 逐种子两通道，
  一次就是 10+ 请求，按 ~1.05s 自节流，429 时按 `Retry-After` 退避（≤3 次）。
- 语义层每次调用要做一次 TEI 向量化 —— 它是**真的在算**，不要为了"多试几个词"
  无脑打十几次。先按 §3 判断上一次是不是没打中。
- 冷/热差异极大（冷 4-25s、热 0.4-1.8s）。同一会话里重复的查询几乎免费；
  第一次慢是正常的，**不要因此重试**（重试只会再排一次队）。

---

## 11. 五个常见场景 → 具体命令

```sh
# ① 模糊方向，先探后收窄
scenarios/semantic-search.sh "retrieval augmented generation reduces hallucination in QA" \
    --scope auto --limit 20

# ② 模糊方向，要覆盖面（自己写改写，中英文各来一条）
scenarios/semantic-search.sh "large language models for personalized learning" \
    --expand "LLM 个性化学习 自适应教学系统" \
    --expand "AI tutor adaptive instruction student outcomes" --limit 20

# ③ 指定论文找相似（自动并 similar + 语义两条通道，并排除种子孪生）
scenarios/semantic-search.sh --paper arxiv:2201.11903 --paper W7165154520 --limit 30

# ④ 限定学科 + 时间窗
scenarios/semantic-search.sh "text as data methods in empirical economics" \
    --scope 2002 --year-from 2023 --limit 20

# ⑤ 拿 JSON 接下游
scenarios/semantic-search.sh "graph neural networks for fraud detection" --json
```

裸 curl 形态（脚本不可用时）：

```sh
source ~/.paperdaily-cli/env
curl -sS -H "Authorization: Bearer $PD_KEY" \
  "$PD_BASE/papers/search?q=$(jq -rn --arg s 'your full sentence here' '$s|@uri')&mode=semantic&limit=20" \
  | jq '{layer:.layer_used, ms:.took_ms,
         items:[.items[]|{id,title,score,subfield_id,why}]}'
```
