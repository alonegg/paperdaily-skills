# 阶段 1b–1d 的工件样例

这一组文件是**手造的**，用来做两件事：

1. 给 `scripts/pd_route_check.py` 的每一条判据一个可复现的输入（仓库测试
   `tests/test_pd_route_check.py` 直接读这些文件）；
2. 给执行 agent 一个「长什么样才算对」的对照物——`references/taxonomy-routing.md`
   讲的是形状，这里是一个填满了的实例。

## 文件

| 文件 | 是什么 | 对应阶段 |
|---|---|---|
| `worklist.jsonl` | 12 篇的候选池（字段与 `pd_worklist.sh` 的真实输出同形） | 1a |
| `taxonomy.json` | 7 个节点、深度 2、4 个 analytical + 1 navigational + 1 reflective | 1b |
| `routing.jsonl` | 11/12 篇被路由，coverage 0.92，**通过**校验 | 1c |
| `routing.broken.jsonl` | 故意每种错都犯一遍的反例，**退出码 2** | 1d 的反例 |
| `overview.md` | 由上面三件生成的「taxonomy + 路由表」新格式 | 1d 收尾 |

## ⚠️ 这些 id 是假的

`EX0001`…`EX0012` 是占位符，**不是真实的 paperdaily paper_id**，拿它们去打任何
端点都会 404。标题用的是这一支文献里真实存在的公开论文的标题（便于人读懂章节
划分是否合理），但**标题与这里的 id 之间没有任何真实对应关系**，也没有 DOI /
arXiv 号——`doi` / `arxiv_id` / `oa_url` 全是 `null`，正是为了让人不会误把这份
样例当成一次真实检索的结果拿去跑阶段 2。

真跑一轮时 id 来自 `pd_worklist.sh`，形如 `arxiv:2409.10897` / `W4416083181`。

## 跑一遍

```sh
cd skills/paperdaily-deep-research

# 通过的那份
python3 scripts/pd_route_check.py --dir references/examples
echo $?     # 0

# 反例：10 处结构性错误
python3 scripts/pd_route_check.py --dir references/examples \
        --routing routing.broken.jsonl
echo $?     # 2

# 把 coverage 门抬到 0.95，看「结构都对但覆盖不够」那条路径
python3 scripts/pd_route_check.py --dir references/examples --coverage-target 0.95
echo $?     # 1
```

`routing.broken.jsonl` 一次命中的错误类型（每种至少一条，按行号）：

| 行 | 犯的错 |
|---|---|
| 1 | 一篇挂了 4 个节点（上限 3） |
| 3 | `paper_id` 与第 2 行重复 |
| 4 | `paper_id=EX9999` 不在 worklist 里（未知 id） |
| 5 | `nodes` 里写的是节点**标题**而不是节点 id（未知节点） |
| 6 | 同一行里重复挂同一个节点 |
| 9 | 不是合法 JSON |
| — | 连带产生 4 个孤儿 analytical 节点（n2 / n2a / n2b / n3） |
