# 检索、题录匹配与覆盖判读

适用 thin 0.4.0 / deep 0.7.0；协议 v1。脚本中的 API 查询默认不调用 `/ask`。

## 查询与范围

- 标识符先 `/papers/resolve`；已知标题用 `mode=title`；概念问题用 `mode=semantic`。
  `auto` 保留为输入形态不明时的选择。旧版本的时延测量不是当前性能承诺。
- 将主题写成包含对象、方法和任务的完整查询；不同研究轴可重复 `--expand`。
  每个改写占用请求配额，批内默认串行节流，不使用无界并发。
- 多个 `--paper` 会逐个 resolve，调用 similar 和标题语义查询；失败通道写入 errors。
  `/papers/batch` 按最多 100 个 ID 补齐细节，保留缺失明细。
- `--scope 1702` 指 subfield，`--scope field:17` 指 field；`auto` 不自动收窄。
  时间和 scope 在补齐详情后统一过滤，包括 similar 与引用扩展；缺少约束字段则排除。
- `paper_group_id`、DOI、arXiv ID、库内 ID 是折叠依据，canonical 优先，`all_ids`
  保留来源。标题相同只能作为人工核验线索，不能据此吞并两篇论文。

## 排名不是相关性或支持率

RRF 与 hits 用来融合多路候选，不跨请求解释成概率。score 的定义由各端点决定。
`why` 可能只是结果集统计；查看 `cluster_rerank_applied` 判断是否参与重排。
最终逐篇核对标题、摘要、任务、对象、方法与结果；需要支持某个论断时继续读取原文。

`truncated`、`partial`、`next_cursor`、`coverage`、stub 和缺失详情表示不同的覆盖边界。
零条引用可能是未建边、未覆盖或没有结果，不能证明论文没被引用。
返回 20 条是候选数量，不是研究领域的文献总量。

## 题录端点的 JSON 请求

`bibliography KIND --input FILE --json` 透传这些请求，并保留服务端完整响应。

`match` → `POST /papers/match`：

```json
{"items":[{"ref_id":"r1","doi":"10.0000/example","title":"Example paper","authors":["Example Author"],"year":2025}],"options":{"fuzzy":true,"max_candidates":5}}
```

示例 DOI 是占位符。精确标识优先；审查 ambiguity/candidates/mismatches，保留 `ref_id`。
命中的是题录身份；正文有没有支持该引用仍需阅读核验。

`related` → `POST /papers/related`：

```json
{"units":[{"id":"p1","text":"The research paragraph to find related literature for.","kind":"paragraph","section_kind":"related"}],"seed_ids":[],"filters":{"year_from":2023},"limit_per_unit":5,"limit_doc":20}
```

`document` 与 `units` 二选一。按段提供任务语境；推荐结果用于候选补引，不能直接写入参考文献。

`citation-neighborhood` → `POST /papers/citation-neighborhood`：

```json
{"paper_ids":["arxiv:2201.11903"],"directions":["cocited","common_refs"],"limit":30}
```

保留 `coverage` 分母、`partial`、`stubs`；引用边稀疏不能等价为没有学术关联。

`journals` → `POST /journals/lookup`：

```json
{"items":[{"ref_id":"j1","name":"Example Journal"}]}
```

ISSN 优先于名称；区分匹配状态、评级体系、评级年份。期刊评级不是单篇研究质量证明。
这些端点使用 `read:paper`，但有独立的按条配额；读响应头或协议，不假定一次 POST 只计一次。

## 成本与错误

`--synth` 才调用一次 `/ask`，需要 `synth:ask`，可能消耗积分；默认关闭。
综合输出只是基于检索/抽取的背景材料，不替代全文精读。写操作不自动重试；
已声明幂等的 reading-session 创建和修订例外。HTTP 200 非 JSON、错误 envelope、
429、503、超时均不得伪装成成功空集。
