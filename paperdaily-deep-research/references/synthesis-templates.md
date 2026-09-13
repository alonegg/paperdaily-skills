# 跨篇综合模板

适用阶段：Stage 3（深度分析）跨篇综合，在 `notes/<paper_id>.md` 齐备之后执行。产出四件套 + `claims.jsonl` + `report.md`。所有跨篇引用统一用 `[paper_id p.X]` 格式，`paper_id` 命名规则见 `reading-note-template.md`。

## 四件套

### 1. 方法对比表 `comparison-table.md`

```markdown
| 方法名 | 论文 | 任务 | 数据集 | 指标 | 结果 | 是否开源 |
|---|---|---|---|---|---|---|
| <method> | [paper_id p.X] | <task> | <dataset> | <metric> | <数字> | <是，repo url / 否> |
```

- 每一行必须能追溯到某篇论文某个具体表/页；"结果"列写清楚是在哪个数据集/指标下的数字。
- 不同论文用的基线或数据集不可比时，不要硬凑同一行——拆成多行，或在行尾加脚注说明不可比原因。

### 2. 时间线 `timeline.md`

```markdown
## <年份>
- **[paper_id p.X]** (<venue>): <这篇相对上一个节点解决了什么新问题 / 引入了什么新组件>
```

- 按年份从早到晚排列，同年内按发布顺序。
- 每条不是复述摘要，目的是让读者看出演进脉络——谁受谁启发、谁解决了谁的局限、哪个节点是范式转折点。

### 3. 流派/分类法 `taxonomy.md`

⚠️ **0.6.0 起这一件不是从头归纳的**：分组在阶段 1b 就已经做完并落在
`taxonomy.json` 里，且被 1d 机械校验过、被 3a 的子 agent 带着各节的
`key_questions` 实际读过一遍。这里要做的是**把它写开**——给每个节点补上「组内
共享假设」和「与其他节点的分歧」，论文清单按 `routing.selected.jsonl` 重算
（不是 `routing.jsonl`，那是收敛前的）。

读完之后确实要改结构（拆一节、并两节）是正常的，但**必须改回
`taxonomy.json`**，不能只改这份 md——否则 `overview.md`、`report.md` 和它会给出
三份互相不一致的章节划分，而这三份都会被用户看到。

```markdown
## <node id> · <node title>（照 taxonomy.json，不要改名）
**核心假设/思路**: <这一节内所有方法共享的核心假设>
- [paper_id p.X]: <这篇论文在这一节里的具体做法>
- [paper_id p.X]: ...

**与其他节点的分歧**: <这一节和其他节在假设/适用条件上的分歧点>
```

- 分组维度在 1b 已定（按方法类型 或 理论假设，不混着分）；这里只写开，不重分。
- 每节必须写清楚组内共享假设，以及节间的分歧点——不是简单地把论文分桶罗列。
- `reflective` / `navigational` 节点不进这一件（它们进 `gaps-and-conflicts.md`
  和报告的引言）。

### 4. 矛盾点与研究空白 `gaps-and-conflicts.md`

```markdown
## 矛盾点

### 矛盾 1: <一句话概括冲突>
- **A 方声称**: [paper_id p.X]: <具体论断>
- **B 方结果**: [paper_id p.X]: <与之冲突的具体结果>
- **可能解释**: <数据集不同 / 评测协议不同 / 规模效应 / 复现问题等，并标注这是推测还是有直接依据>

## Open Problems
- <问题描述> — 提及/暗示: [paper_id p.X], [paper_id p.X]

## Missing Evaluations
- <缺失的评测维度或场景> — 提及/暗示: [paper_id p.X]

## Under-Explored Directions
- <方向> — 提及/暗示: [paper_id p.X]
```

- 每条 Open Problems / Missing Evaluations / Under-Explored Directions 必须挂至少一篇来源论文（哪篇提到或暗示了这个空白），不允许凭综述者自己的判断凭空列条目。

## claims.jsonl：跨篇论断防幻觉账本

每条跨篇论断记一行 JSON（JSONL，一行一条）：

```json
{"claim": "...", "evidence": [{"paper_id": "...", "location": "p.X Fig.Y", "quote_or_paraphrase": "..."}], "status": "supported|weak|contested|gap", "note": "..."}
```

字段说明：
- `claim`：一句可核查的论断，不是问题也不是模糊感想。
- `evidence`：数组，每条含 `paper_id`（对应 notes 文件名）、`location`（`p.X` / `p.X Fig.Y` / `p.X Table.Y` / `§N.M`，与精读笔记锚定格式一致）、`quote_or_paraphrase`（原文引用或忠实转述，不是概括后的二次结论）。
- `status`：
  - `supported` — 至少两篇独立论文（非同一作者组、非直接引用转述关系）给出一致证据
  - `weak` — 只有一篇证据，或证据是间接推断而非直接陈述
  - `contested` — 至少两条 evidence 互相冲突（对应 report 矛盾点章节的条目应该能在这里找到 claim）
  - `gap` — evidence 为空，或精读范围内没有覆盖到
- `note`：证据强度评估、是否需要在 report 里加 hedge（"初步证据显示…"这类限定语）。
- `node`（0.6.0，可选）：这条论断属于 `taxonomy.json` 的哪个节点，纯粹方便本地
  按节组织正文。⚠️ **它不会被上传**——`upload_session.py` 只取 `claim` /
  `status` / `evidence` / `note` 四个键（服务端 schema 没有节点这一维），所以
  不要把只有 `node` 才说得清的信息藏在那里，该写进 `note` 的写进 `note`。

### 铁律

1. **`evidence` 为空必须 `status: "gap"`，并在 `note` 写明 gap 原因**（是没检索到相关论文，还是检索到了但论文没讨论，还是这个领域确实没人做过）。
2. **禁止用模型自身常识/训练知识补全 evidence。** 每条 evidence 必须能对应到某篇 `notes/<paper_id>.md` 或原始 PDF 里真实存在的内容——写不出 `quote_or_paraphrase` 就不能算作 evidence。
3. claims.jsonl 应随综合过程持续追加，不要等 report 写完再补记——事后补记时最容易把"记得论文这么说"当成真实证据写进去。

## report.md 建议章节结构

1. **执行摘要** — 3-5 句话，回答"这个领域现在处于什么状态、最值得读的是哪几篇"
2. **领域地图/分类法** — 基于 `taxonomy.md`（节点顺序照 `taxonomy.json` 的树：
   顶层按 `navigational` → `analytical` → `reflective` 读起来最顺）
3. **逐节正文** — 每个 `analytical` 节点一节，150-250 词起，回答该节的
   `key_questions`，只引这一节的论文（主属 + 次属都能引），末尾列本节论文 id
4. **时间线** — 基于 `timeline.md`（跨节）
5. **方法对比** — 基于 `comparison-table.md`（跨节；对比轴取自各节的分歧点）
6. **矛盾与空白** — 基于 `gaps-and-conflicts.md`，与 `reflective` 节点合并写
7. **结论与建议阅读顺序** — 3-5 篇必读 + 理由，并给出建议阅读顺序（先读哪篇建立框架，再读哪篇看反例/边界条件）

正文所有可核查论断随文标注 `[paper_id p.X]`；不允许无引用的事实性陈述（执行摘要里对全篇的整体判断除外，此时可引用报告内部小节代替）。

## 带引用问答操作范式（PaperQA2 范式）

用于回答"某个具体问题，跨这批论文的答案是什么"这类查询，而不是直接从记忆或摘要拼答案：

1. 把问题拆解成检索 query。
2. 对每篇候选论文的 notes（必要时回 PDF 原文段落）做"问题语境下的打分摘要"——不是摘录整段，而是针对当前问题重新概括这段说了什么、和问题的相关度打几分。
3. 按打分重排，只取 top-K 真正相关的段落进入下一步。
4. 生成答案：每句可验证的论断后标注来源 `[paper_id p.X]`；所有候选段落都答不了这个问题时，明确写"未找到证据"，不能靠常识补答案。
5. 与 claims.jsonl 联动：问答过程中产出的新论断顺手记一行进账本，保持账本随综合过程更新，而不是事后一次性补记。
