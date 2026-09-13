# taxonomy + 反向路由：形状、提示词与判据（阶段 1b / 1c / 1d）

> 本文是阶段 1b–1d 的规程。SKILL.md 讲流程，这里讲**形状**与**提示词**。
> 形状与服务端 `llm/survey/`（`POST /api/matrix/draft` 的骨架生成）**是同一份
> 契约**：同一个 JSON 既能被本 skill 的 `pd_route_check.py` 校验，也能被服务端
> 的 `taxonomy.py` / `routing.py` 消费。两边任何一方改形状都要回来改这份文件。

## 0. 为什么要在检索和精读之间插这三步

阶段 1a 交出来的是一个**平的**候选池：几十行 jsonl，按相似度排着。拿它直接进
精读，会有三件事按顺序发生——

1. 阶段 1.5 的收敛退化成「按相似度取前 N」，选出来的是一堆彼此重复的论文；
2. 阶段 3a 的 fan-out 是逐篇的，每个子 agent 都不知道自己这篇在整体里的位置，
   回来的笔记互相之间没有可比的轴；
3. 阶段 3b 拿到一堆笔记，要在综合的时候**临时**发明章节——而这时上下文里已经
   塞满了笔记，是全流程里最不适合做结构决策的时刻。

先归纳章节、再把论文反向路由进去，等于把结构决策提前到材料最薄、最便宜的时候
做一次，后面每一步都顺着这个结构走。这也是 DAS 消融里增益最稳的那一项。

**反向路由**（每篇论文问「你属于哪几节」）不是**正向分配**（每节问「谁属于
我」）。方向反过来是刻意的：正向会让 LLM 为了填满每一节而硬塞，而反向允许一篇
论文**属于零个节点**——池子里本来就有噪声，这个出口必须留着。

## 1. `taxonomy.json`

```json
{
  "topic": "交错处理时点下的双重差分（staggered DiD）",
  "generated_at": "2026-09-13T10:12:00Z",
  "skill_ver": "0.6.0",
  "nodes": [
    {
      "id": "n1",
      "title": "TWFE 的负权重诊断",
      "description": "一到三句，说清这一节收什么、不收什么。",
      "role": "analytical",
      "key_questions": ["…", "…"],
      "parent": null
    }
  ]
}
```

### 节点字段（**这一层的形状不可改**——服务端 `llm/survey/taxonomy.py` 同形）

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | string | 全表唯一、非空。短标识（`n1` / `n2a`），**不是标题**。 |
| `title` | string | 非空。章节名，会直接印进 `overview.md` 与 `report.md`。 |
| `description` | string | 一到三句。它是 1c 路由时 LLM 唯一的判据，写虚了路由就飘。 |
| `role` | enum | `analytical` \| `reflective` \| `navigational`，见下表。 |
| `key_questions` | string[] | 这一节要回答的问题。阶段 3a 的派工直接抄它。 |
| `parent` | string \| null | 父节点 id，或 `null` 表示顶层。 |

外层 `topic` / `generated_at` / `skill_ver` 是本 skill 的附加字段，校验器不强求
（`taxonomy.json` 直接写成裸的节点数组也能被读，只是 `topic` 为空）。

### 三种 role

| role | 它是什么 | 精读预算 | 主属论文可以为 0 吗 |
|---|---|---|---|
| `analytical` | 综述的**主体**：一条技术路线、一类机制、一个争点。要靠论文里的证据支撑。 | 要派精读 agent | **不可以**（0 篇是 ERROR，只有次属是 WARN） |
| `reflective` | 反思：矛盾、空白、方法论上的不安。材料来自**别的节点那些论文**的 `limitations` / `open_questions`。 | 不单独派 | 可以 |
| `navigational` | 导航：背景、综述入口、术语表。给读者指路，不承担论证。 | 不单独派 | 可以 |

**至少要有一个 `analytical` 节点**，否则这份 taxonomy 撑不起综述主体，1d 直接
报错。全是 `analytical` 也完全合法——`reflective` / `navigational` 是允许项不是
必需项。

### 规模约束（与 `pd_route_check.py` 的默认值一致）

- 节点数 ∈ `[3, 8]`；
- 深度 ≤ 2（顶层 + 一层子节点），`parent` 必须指向存在的节点且不成环；
- 父节点自己可以不挂任何论文——校验按**子树**判孤儿。

节点数上限不是美学：8 节 × 每节 ≥2 篇 = 16 篇，已经是「正经方向综述」档位的
上限（SKILL.md 开头那张档位表）。切到 12 节，每节只剩一篇，阶段 3b 就没有对比
可写了。

## 2. `routing.jsonl`

每行一篇论文：

```json
{"paper_id": "EX0001", "nodes": ["n2a", "n3"], "why": "可选，一句话"}
```

| 字段 | 类型 | 约束 |
|---|---|---|
| `paper_id` | string | **必须逐字等于** `worklist.jsonl` 里的 `.id`。 |
| `nodes` | string[] | 0–3 个节点 id，**按相关性降序**。空数组合法。 |
| `why` | string | 可选。本 skill 用来给人看；服务端不消费。 |

三条不显眼但会咬人的规则：

1. **顺序是有语义的。** `nodes[0]` 是这篇的**主属节点**——阶段 3a 按主属节点派
   工，一篇论文只被读一次、只产一份 `notes/<paper_id>.md`。次属节点只在综合时
   引用它，不重复精读。所以「这篇最该放哪一节」要写在第一位。
2. **每篇论文最多一行。** 同一个 `paper_id` 出现两次是 ERROR，不是「合并」。
3. **空数组是正当出口，不是失败。** 池子里的噪声、或者确实不属于这个 taxonomy
   的论文，就该 `"nodes": []`。它会拉低 coverage——这正是我们想量的那个数。

## 3. 阶段 1b：归纳章节

### 输入

worklist 的每一行 + 它的抽取。**worklist.jsonl 本身没有 tldr / methods**（它只有
id/title/date/venue/doi/arxiv_id/oa_url/pdf_url/source），所以 1b 开始前先补一次：

```sh
cd pd-research/<slug>/
source ~/.paperdaily-cli/env
# ≤100 id / 次，**整批只算 1 个 rate-cost 单位**，比逐篇 GET /papers/{id} 便宜得多
IDS=$(jq -r '.id' worklist.jsonl | head -100 | jq -R . | jq -sc .)
curl -sS -X POST "$PD_BASE/papers/batch" \
     -H "Authorization: Bearer $PD_KEY" -H 'Content-Type: application/json' \
     -d "{\"ids\": $IDS}" > worklist.extractions.json
```

响应是 `{items: [PaperDetail…], missing: [id…]}`；`items` 里每篇带 `tldr_en` /
`tldr_zh` / `methods[]` / `datasets[]` / `contributions[]` / `key_claims[]` /
`limitations[]` / `open_questions[]` / `applicable_to[]` / `identification{}` /
`sample{}`，以及 `has_extraction`。
**`missing` 非空要当回事**：那几篇 worklist 里有、图谱里查不到，1b 的材料就少了
这几篇，而 1c 照样会被要求路由它们（worklist 才是 id 的真相）。池子超过 100 篇
就分两批调。

**喂给 1b 的是裁剪过的视图，不是整条记录**——上下文预算要留给后面：

| 视图 | 字段 | 用在哪 |
|---|---|---|
| slim | `paper_id, title, year, tldr_en, methods[].name, contributions[0]` | 池子 >60 篇时的 1b |
| org | slim + `key_claims, limitations, open_questions, datasets[].name, identification.strategy, sample.{period,region,unit}` | **1b 与 1c 的默认视图** |
| full | 整条抽取 | 只在阶段 3 对**分到该节的**论文加载 |

（**v0.9.26 起 `identification` / `sample` 可用**——`POST /papers/batch` 与
`GET /papers/{id}` 都导出了它们，skill 侧的 org 视图与服务端 `org_view` 形状一致，
此前那条「拿不到」的不对称说明作废。两个字段的形状：
`identification = {strategy, description_zh, description_en}`，`strategy` 取值
`DiD｜IV｜RDD｜RCT｜event-study｜structural｜matching｜panel-FE｜other｜none`；
`sample = {period, region, unit}`，三个键各自可缺。

⚠️ **它们很稀疏，而且 `null` 与 `"none"` 不是一回事。** 这两个块只存在于 v2 抽取
schema，约四分之一的已抽取论文才带（其余返回 `null` = **不知道**，不是「没有识别
策略」）；带了的那批里 `strategy` 又以 `"none"` 居多，因为被抽取的人口大部分不是
实证社科。**所以：能用它切章节，但不能用它统计**——按发表年 GROUP BY 会把 schema
上线的波前读成「识别策略在迁移」的假结构断点。1b 的用法是「有值就当一条切分依据，
没值就当这篇没告诉我」，绝不要因为 `identification` 是 `null` 就把一篇论文归到
「非实证」那一节去。）

`has_extraction=false` 的论文只有标题——照样喂进去，但在 prompt 里标明
「这篇只有标题」，让 LLM 不要为它编一个章节。

### 提示词骨架

> 下面是「<topic>」方向的 N 篇论文（每篇：id、标题、年份、一句话摘要、方法名、
> 主要贡献、关键论断、局限、开放问题，以及**有的话**还有识别策略与样本框）。
>
> 请归纳出一份**章节表**，用来组织一篇文献综述。要求：
>
> 1. 3–8 个节点，最多两层（顶层 + 一层子节点）。
> 2. 每个节点给 `id`（短标识如 n1/n2a，不要用标题当 id）、`title`、
>    `description`（1–3 句，说清收什么不收什么）、`role`、`key_questions`
>    （2–4 个这一节要回答的问题）、`parent`（顶层为 null）。
> 3. `role` 三选一：`analytical`（技术路线/机制/争点，综述主体，要有论文支撑）、
>    `reflective`（矛盾与空白，材料来自别人的 limitations）、`navigational`
>    （背景与入口，不承担论证）。**至少一个 analytical。**
> 4. 章节要按**这批论文实际存在的分歧**来切——按方法路线、按识别策略、按它们
>    互相不同意的地方切，**不要**按年份切，也不要切成「早期/中期/近期」。
>    识别策略缺失的论文只说明这一项没抽出来，不要因此把它归进「非实证」一类。
> 5. 一个节点如果只能装下一篇论文，就不要单独设它；并进相邻节点。
> 6. 只输出 JSON：`{"topic": "...", "nodes": [...]}`，不要任何解释文字。

落盘 `taxonomy.json`。

### 判它好不好的两条人工判据（脚本查不出来的那部分）

- **每个 analytical 节点的 `description` 能不能当作一条「收/不收」的判据用？**
  写成「关于 X 的研究」是废话，写成「以 X 为对照组的那一支，不含 Y」才有用。
- **把两个节点的 title 交换，路由结果会不会变？** 不会变就说明这两节切得没有
  区分度，合并掉。

## 4. 阶段 1c：反向路由

**10 篇一批**，每批把**完整的 taxonomy**（全部节点的 id/title/description/role）
连同这 10 篇的 org 视图一起送。分批是为了让每批的输出短到不会被截断；taxonomy
不能分批，每批都要看到全表，否则它只会在看得见的那几节里选。

### 提示词骨架

> 这是一份章节表（下面 JSON），和 10 篇论文。
>
> 对**每一篇**论文，判断它属于哪些章节：
>
> 1. 每篇 **0 到 3 个**节点 id，按相关性**降序**排列。第一个是主属节点——
>    后面的精读会按主属节点组织，所以「这篇最该放哪一节」放第一位。
> 2. 只能用章节表里**已有的 id**，不要发明新节点，也不要写节点标题。
> 3. 一篇论文如果哪一节都不贴切，就给 `"nodes": []`。**这是正当答案**，
>    不要为了让它有归属而硬塞进最接近的那一节。
> 4. `paper_id` 必须逐字照抄我给你的 id。
> 5. 每篇输出一行 JSON：`{"paper_id": "...", "nodes": [...], "why": "一句话"}`，
>    共 10 行，不要任何其他文字。

把每批的输出追加进 `routing.jsonl`。

### 覆盖率不够时怎么补

`pd_route_check.py` 退 1 的时候，做法是**重跑路由，不是降门槛**：

1. 把 `--json` 输出里的 `unrouted` 单独捞出来，连同**完整 taxonomy** 再送一轮
   （这一轮每批可以小到 5 篇，并在 prompt 里加一句「这些论文上一轮都没找到
   归属，请再判一次；仍然不贴切的照旧给空数组」）；
2. 用覆盖率更高的那一轮结果覆盖对应行（同一 `paper_id` 只能留一行）；
3. **两轮之后仍不够，问题在 taxonomy 不在路由**——回 1b 重切章节。典型症状是
   未路由的那批彼此之间有明显共性（比如全是应用论文），那就是缺一个节点。

不要用这两种方式过关：把 `--coverage-target` 调低（它是判据不是参数）、把未路由
的论文从 worklist 里删掉（那是在改分母）。

## 5. 阶段 1d：确定性校验

```sh
python3 scripts/pd_route_check.py --dir pd-research/<slug>/
python3 scripts/pd_route_check.py --dir pd-research/<slug>/ --json   # 机器可读
```

| 检查 | 判为 | 说明 |
|---|---|---|
| taxonomy 节点 id 重复 / 缺 title / role 不合法 / parent 不存在 / 成环 / 超深度 | ERROR | |
| 节点数不在 `[3, 8]` | ERROR | |
| 没有 analytical 节点 | ERROR | |
| routing 行不是合法 JSON / 缺 `paper_id` | ERROR | |
| `paper_id` 不在 worklist（未知 id） | ERROR | LLM 编 id 或写成了安全化文件名 |
| `nodes` 里有未知节点 | ERROR | 最常见是写了标题而不是 id |
| 一篇挂 >3 个节点 | ERROR | |
| 同一 `paper_id` 多行 / 同一行重复挂同一节点 | ERROR | |
| analytical 节点子树 0 篇（孤儿） | ERROR | |
| analytical 节点只有次属论文 | WARN | 3a 不会为它派 agent |
| analytical 节点 <2 篇 | WARN | 阶段 1.5 的收敛下限 |
| reflective / navigational 节点 0 篇 | WARN | 允许，但确认是本意 |
| `description` 为空 / analytical 节点无 `key_questions` | WARN | |
| coverage < 0.6 | **退出码 1** | 重跑 1c |

退出码：`0` 通过 / `1` coverage 不够 / `2` 结构性错误 / `3` 文件缺失或 JSON 坏掉。

**结构性错误盖过 coverage**：路由表不合法时那个覆盖率没有意义（把一半 id 写错，
覆盖率会「看起来」很低，而要修的是 id），所以两者同时出现时退 2。

`--json` 的字段：`coverage` / `n_papers` / `n_routed` / `n_nodes` /
`node_counts`（任意位置）/ `primary_counts`（主属）/ `subtree_counts` /
`subtree_primary_counts` / `has_children` / `unrouted[]` / `errors[]` /
`warnings[]` / `nodes{}` / `exit_code`。

**`node_counts` 与 `primary_counts` 是两个不同的数，别混用**：判孤儿看前者
（含次属），估阶段 3a 的 agent 数量看后者（主属，一篇只读一次）。

## 6. 1d 收尾：改写 `overview.md`

阶段 1a 的 `pd_worklist.sh` 已经用一次 `/ask` 写了 `overview.md`。1d 通过之后把它
**改写成「taxonomy + 路由表」**，并把原来那段 `/ask` 综述保留到文末的「附」里
（它是 provenance：写作时点早于章节划分、描述的是收敛前的整个池子）。

新格式见 [`examples/overview.md`](examples/overview.md)，每节包含：

- 章节地图（树形，带每节篇数）；
- 每个节点：`description`、`key_questions`、论文表（paper_id / 标题 / 年 /
  主属还是次属）；
- 「未路由」表，每篇写一句**为什么**没进任何一节；
- 文末「附：检索期 `/ask` 背景综述」。

不要把 `overview.md` 改名或拆成两个文件：`upload_session.py` 按这个文件名把它
inline 进回传 payload（SKILL_SPEC §8「核心件名不改」）。

## 7. 与服务端 `llm/survey/` 的关系

| | skill（本文） | 服务端 `llm/survey/` |
|---|---|---|
| 谁在跑 LLM | 执行 agent（会话主模型） | `POST /api/matrix/draft` 调本地 vLLM |
| taxonomy 形状 | **同一份** | **同一份** |
| routing 行形状 | **同一份** | **同一份** |
| 校验 | `pd_route_check.py`（纯标准库，只读文件） | `taxonomy.py` / `routing.py` 内的确定性检查，失败回灌重试 ≤2 |
| 输入视图 | `POST /papers/batch` 的裁剪视图 | `views.py` 的 `slim/org/full` |
| coverage 门 | 0.6，退出码 1 | `coverage_target=0.6`，多轮取最高 |

形状一致的用处很直接：用户在 web 工作台点「生成综述骨架」拿到的 `taxonomy` /
`routing`，可以原样落进 `pd-research/<slug>/` 当作阶段 1b/1c 的产物，跳过这两步
直接跑 1d + 阶段 1.5；反过来本 skill 产的 taxonomy 也能作为矩阵草稿的输入。
**任何一方改形状，先回来改这份文件和契约，不要单边改。**
