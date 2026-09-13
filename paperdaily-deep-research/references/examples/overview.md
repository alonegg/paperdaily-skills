# Overview — 交错处理时点下的双重差分（staggered DiD）

_skill_ver=0.6.0 · nodes=7 · routed=11/12 (coverage 0.92) · route_check=pass ·
generated_at=2026-09-13T10:18:00Z_

> 这份 overview 是**章节地图 + 路由表**，不是综述散文。它的用途是让你（和用户）
> 在下载任何一个 PDF 之前，先看清「这个池子被切成了几节、每节有谁、哪一节薄」。
> 阶段 1.5 按节收敛、阶段 3a 按节派工、阶段 3b 按节综合，全部读这张表。

## 章节地图

```
n1  [analytical]   TWFE 的负权重诊断                       3 篇
n2  [analytical]   异质性稳健估计量                        （父节点，子树 6 篇）
├── n2a [analytical] 分组—时期 ATT 与聚合                  3 篇
└── n2b [analytical] 插补式与设计基础估计                  3 篇
n3  [analytical]   平行趋势：检验、预趋势与敏感性分析       4 篇
n4  [navigational] 综述与入门路径                          1 篇
n5  [reflective]   尚未解决的争议                          3 篇
```

## n1 · TWFE 的负权重诊断 [analytical]

把双向固定效应估计量分解成一组 2×2 比较，指出交错采纳下部分比较以已处理组为
对照、从而产生负权重的那一支文献。它回答的是「旧做法错在哪」，是整条线的起点。

**关键问题**

1. 负权重在什么条件下出现，占比多大才算致命？
2. 分解出来的诊断量（如坏比较权重和）在实证里怎么报告？
3. 诊断结论对处理效应异质性的形式有多敏感？

**论文（3 篇；主属 3）**

| paper_id | 标题 | 年 | 位次 |
|---|---|---|---|
| EX0002 | Difference-in-Differences with Variation in Treatment Timing | 2021 | 主属 |
| EX0003 | Estimating Dynamic Treatment Effects in Event Studies with Heterogeneous Treatment Effects | 2021 | 主属 |
| EX0004 | Two-Way Fixed Effects Estimators with Heterogeneous Treatment Effects | 2020 | 主属 |

## n2 · 异质性稳健估计量 [analytical]

针对负权重问题提出的替代估计量总集。本节自身不直接挂论文，只作为两条技术路线的
父节点，方便综合阶段按路线对比。

**关键问题**

1. 两条路线的识别假设差在哪里？
2. 在同一份数据上它们的点估计与标准误差多少？

**论文**：无直接挂载；见 n2a / n2b（子树合计 6 篇）。

### n2a · 分组—时期 ATT 与聚合 [analytical]

先估计每个 (采纳组, 时期) 的 ATT，再按事件时间/组别/日历时间聚合成可解释的汇总量
的一支。特征是把「聚合权重」显式化。

**关键问题**

1. 聚合权重怎么选，不同选法给出的汇总量各自回答什么问题？
2. 从未处理组 vs 尚未处理组作对照，在什么场景下必须二选一？
3. 多期动态效应的置信带怎么做才不过度乐观？

**论文（3 篇；主属 2）**

| paper_id | 标题 | 年 | 位次 |
|---|---|---|---|
| EX0001 | Difference-in-Differences with Multiple Time Periods | 2021 | 主属 |
| EX0011 | Difference-in-Differences Estimators of Intertemporal Treatment Effects | 2024 | 主属 |
| EX0003 | Estimating Dynamic Treatment Effects in Event Studies with Heterogeneous Treatment Effects | 2021 | 次属（主属在 n1） |

### n2b · 插补式与设计基础估计 [analytical]

先在未处理观测上拟合反事实、再对处理观测逐个插补的一支，以及把处理时点本身当作
随机来源的设计基础视角。特征是效率导向与有限样本性质。

**关键问题**

1. 插补估计量相对分组—时期估计量的效率优势有多大、代价是什么？
2. 设计基础推断把随机性放在处理时点上，这个假设在观测数据里站得住吗？
3. 合成控制思路与 DiD 在交错场景里怎么结合？

**论文（3 篇；主属 3）**

| paper_id | 标题 | 年 | 位次 |
|---|---|---|---|
| EX0005 | Revisiting Event-Study Designs: Robust and Efficient Estimation | 2024 | 主属 |
| EX0006 | Design-Based Analysis in Difference-in-Differences Settings with Staggered Adoption | 2022 | 主属 |
| EX0007 | Synthetic Difference-in-Differences | 2021 | 主属 |

## n3 · 平行趋势：检验、预趋势与敏感性分析 [analytical]

关于识别假设本身的一支：预趋势检验的势与误用、把平行趋势从「是/否」改写成
「偏离多大」的部分识别路线。

**关键问题**

1. 预趋势检验通过，能推出平行趋势成立吗？它的势有多低？
2. 把假设放松成「偏离不超过 M」之后，结论的稳健区间怎么读？
3. 敏感性分析应该在正文报告还是附录？

**论文（4 篇；主属 2）**

| paper_id | 标题 | 年 | 位次 |
|---|---|---|---|
| EX0009 | Pre-Event Trends in the Panel Event-Study Design | 2019 | 主属 |
| EX0010 | A More Credible Approach to Parallel Trends | 2023 | 主属 |
| EX0001 | Difference-in-Differences with Multiple Time Periods | 2021 | 次属（主属在 n2a） |
| EX0005 | Revisiting Event-Study Designs: Robust and Efficient Estimation | 2024 | 次属（主属在 n2b） |

## n4 · 综述与入门路径 [navigational]

把上面几支串起来的综述与实践指南，给读者一条从旧做法到新估计量的迁移路线。
导航性节点，不承担论证。

**关键问题**

1. 一个只会跑 TWFE 的实证作者，最短的迁移路径是什么？

**论文（1 篇；主属 1）**

| paper_id | 标题 | 年 | 位次 |
|---|---|---|---|
| EX0008 | What's Trending in Difference-in-Differences? A Synthesis of the Recent Econometrics Literature | 2023 | 主属 |

## n5 · 尚未解决的争议 [reflective]

各支之间互相不同意、或者公开承认没有定论的地方：估计量选择缺乏统一判据、推断在
小采纳组下的表现、与合成控制族的边界。反思性节点，材料来自各节论文的 limitations
与 open_questions。

**关键问题**

1. 在同一份数据上，不同稳健估计量给出不同结论时该信谁？
2. 采纳组很少时，哪一类推断还站得住？

**论文（3 篇；主属 0 —— 全部是次属）**

| paper_id | 标题 | 年 | 位次 |
|---|---|---|---|
| EX0007 | Synthetic Difference-in-Differences | 2021 | 次属（主属在 n2b） |
| EX0008 | What's Trending in Difference-in-Differences? | 2023 | 次属（主属在 n4） |
| EX0010 | A More Credible Approach to Parallel Trends | 2023 | 次属（主属在 n3） |

> reflective 节点主属为 0 是**正常的**：它的材料是别的节点那些论文的 limitations /
> open_questions，不需要单独派精读 agent。analytical 节点主属为 0 才是问题，
> `pd_route_check.py` 会为那一种打 WARN。

## 未路由（1 篇）

| paper_id | 标题 | 为什么没进任何一节 |
|---|---|---|
| EX0012 | The Effect of Minimum Wages on Low-Wage Jobs | 应用论文，方法上直接沿用堆叠事件研究；本 taxonomy 没有应用节，刻意留白不硬塞 |

> 未路由不等于「不重要」，只等于「这份章节地图装不下它」。如果未路由的比例高到
> 让 `pd_route_check.py` 退 1，那要动的是 taxonomy（回 1b），不是把它们硬塞进
> 某个节点。

## 附：检索期 `/ask` 背景综述

（`pd_worklist.sh` 在阶段 1a 生成的那一段原样保留在这里，作为 provenance——它
描述的是**收敛之前的整个候选池**，且它的写作时点早于本页的章节划分。）

_tokens_in=…, tokens_out=…, cited=…_

…（`/ask` 原文）…
