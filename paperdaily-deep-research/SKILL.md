---
name: paperdaily-deep-research
description: >-
  Conduct citation-grounded literature research with PaperDaily: discover a
  candidate pool, organize and select papers, obtain authorized full texts,
  read with source anchors, synthesize evidence and optionally upload derived
  analysis. Use for deep reading, literature reviews, seed-paper research and
  the research task inbox and single-paper analysis supplements. Supports a single agent or explicitly authorized
  delegation; no specific model or client is required.
---

# PaperDaily 深度研究

- **skill_ver**: `0.8.1`
- **协议**: AGENT_PROTOCOL v1
- **基本 scope**: `read:paper`；可选 inbox/成果回读 `read:reading`，领取/上传/修订
  `write:reading`，显式服务端综合 `synth:ask`。
- **运行条件**: Python ≥3.10；兼容 `.sh` 入口另需 Bash；全文工具的可选依赖见来源手册。
- 所有 `scripts/…` 相对本 Skill 所在目录，不硬编码客户端安装路径。

用户要求的是带可核查证据的研究成果。按下面的落盘工件推进，缺项不能靠措辞盖成完成。
沿用会话的模型、权限与用户已给的授权；只在用户或适用指令允许时派子 Agent。

## 0. 任务与预检

运行 `python3 scripts/pd.py doctor --json`。配置在环境或 `~/.paperdaily-cli/env`，
不执行配置文件、不打印 key。明确研究问题、时间范围、研究对象、报告用途；有现成材料
就从对应阶段继续。文件落到用户指定目录或 `pd-research/<slug>/`。

可选任务入口：`scripts/pd_inbox.sh --json`；已有领取授权时使用 `--claim ID`。
缺 `read:reading`/`write:reading` 返回明确错误，不把权限不足显示为收件箱为空。
可以继续其他已授权的本地研究，但要报告该步骤未完成。

## 单篇补充入口

用户要补充已有论文的分析，或给出补充任务 ID 时，先读
[supplement-workflow.md](references/supplement-workflow.md) 与
[supplement-security.md](references/supplement-security.md)。此模式支持局部阅读，
不要求扩展检索池或制作跨篇综述；不要把它回落成旧 session 上传。

## 1. 检索、组织与筛选

```sh
scripts/pd_worklist.sh "causal inference with staggered adoption" --limit 40 --out pd-research/example --json
scripts/pd_worklist.sh --paper arxiv:2201.11903 --paper arxiv:2305.10601 --year-from 2023 --out pd-research/seeds --json
scripts/pd_worklist.sh 1702 --days 14 --no-extraction --out pd-research/ai --json
scripts/pd_worklist.sh --paper arxiv:2201.11903 --citation-neighborhood --out pd-research/expanded --json
```

- 方向默认 semantic；已知完整标题用 `--search-mode title`；标识符先 resolve。
  `--expand` 添加独立检索轴。`--scope auto` 不自动收窄，明确范围用 subfield ID 或 `field:ID`。
- 查询、similar、引用扩展统一过滤日期与 scope；用户指定的种子留在阅读目标里，即使它早于扩展时间窗。
- 使用 `/papers/batch` 保存详情和抽取；种子默认读取 context-bundle，`--context` 显式启用。
  `worklist.jsonl` 保留 canonical、group、DOI/arXiv 与来源 ID，按强身份去重，不按标题硬合并。
- `worklist.meta.json` 记录请求、时间口径、coverage、失败和追加历史；`--append` 合并，
  `--replace` 明确替换检索清单，默认不覆盖。重新筛选后须更新 downstream 工件，旧笔记不会自动变正确。
- 默认没有 `/ask` 开销；需要时才 `--synth`，写入 `synthesis-background.json`，仅作背景。
- 题录匹配、段落补引、共被引/共同参考、期刊查询均可用 `scripts/pd.py bibliography …`；
  参数见 `--help` 与 AGENT_PROTOCOL。保留 partial/coverage，不把推荐当成引用支持。

阅读 [taxonomy-routing.md](references/taxonomy-routing.md)，生成 `taxonomy.json`、
`routing.jsonl`，运行 `scripts/pd_route_check.py --help` 指示的校验。
按研究轴、年份、方法与反例筛选 `worklist.selected.jsonl`、`routing.selected.jsonl`，
记录 `decisions.md`。选择清单必须是已检索池的子集，新增外部材料登记在 `external_sources.jsonl`。
对筛选后清单重新校验，要求 coverage=1 且无孤儿 analytical 节点。

排名与模型抽取只能辅助组织，相关性由标题、摘要和研究内容确定。字段缺失表示未知，
不同抽取 schema 的覆盖不适合作为跨年份研究趋势。

## 2. 获取材料

```sh
python3 scripts/fetch_fulltext.py --worklist pd-research/example/worklist.selected.jsonl --out pd-research/example/pdfs
```

获取规则及续跑见 [fulltext-sources.md](references/fulltext-sources.md)。默认 OA；
机构直连与用户浏览器只在已确认的授权范围内开启。登录、验证码由用户处理，
`blocked` 表示验证阻断，`denied` 表示明确拒绝，`partial` 表示片段；三者不能混写。

下载账本只证明取得材料，**不证明读过**。完整 PDF、HTML、保留定位的解析全文均可使用；
读的是另一个版本时记录 version 与目标版本的关系，不能混用页码/样本量/表号。
没有全文可据实际摘要写 abstract-only 笔记，或显式跳过并说明原因。

## 3. 阅读与证据

阅读 [精读规程](references/deep-read-agent-prompt.md) 和
[笔记模板](references/reading-note-template.md)。每篇独立笔记使用原始 `paper_id`，
安全化只用于文件名。一篇论文只有一份笔记。主 Agent 可串行完成；授权并行时按章节分工，
明确文件归属，单批建议不超过三篇，不覆盖其他 Agent 的文件。

每篇记录到 `reading_manifest.json`：材料相对路径、SHA256、载体、版本、实际阅读深度、
模型、时间及真实位置锚点；格式见 [证据契约](references/evidence-contract.md)。
全文标记必须有完整材料与实际完成阅读记录；PDF 记录全部已读物理页。
只读片段就保留局部笔记，不能标成全文；需要上传时只使用实际读过的摘要并标 abstract-only，
或显式跳过该篇。不要把局部全文成果错误降称摘要证据。

按 [综合模板](references/synthesis-templates.md) 生成对比、时间线、分类、分歧/缺口和
`synthesis/claims.jsonl`，最后形成 `report.md`。每条非 gap claim 必须链接到已读来源和
manifest 中的真实锚点；区分研究发现、作者解释和你的推论。多版本不能计作独立复现。

```sh
python3 scripts/pd_evidence.py --dir pd-research/example
python3 scripts/upload_session.py --dir pd-research/example --dry-run
```

证据校验检查 ID、覆盖、源文件哈希、阅读记录及 claim-anchor 一致性，**不证明蕴含、
研究真实性或专家审稿质量**。自己回看关键原文；报告列出检索范围、选入数、全文实读数、
摘要数、跳过数、失败和局限。访问失败或不足以支持研究问题时明确交付为阶段性结果。

## 4. 可选上传与修订

用户已明确同意上传时继续，无需重复确认；没有该授权时先说明具体内容再取得同意：
只上传派生笔记、claims、报告、overview；原文不上传。首传可能把 gap/contested 原文加入
问题收件箱，并按服务端规则计积分。私有上传不等于公开分享。

```sh
python3 scripts/upload_session.py --dir pd-research/example
python3 scripts/upload_session.py --dir pd-research/example --task-id TASK_ID
python3 scripts/upload_session.py --dir pd-research/example --revise SESSION_ID
```

首传按 worklist 指纹幂等；HTTP 200 + deduplicated **不表示编辑已保存**，脚本会用
`read:reading` 回读比较内容。成功后写 `upload_receipt.json`，包含目标服务、session ID 和内容哈希。
改过报告/笔记/claims 后须显式 `--revise`，形成不可变私有新版本，原件保留。
修订按内容与 root 去重，不重复产生上传/全文积分或问题草稿，仍占 session 数量。

**修订依赖服务端新端点** `/me/reading-sessions/{id}/revisions`。
客户端升级不代表该端点已部署；404/405 必须报告未保存并保留本地工件，禁止换随机指纹重传。
未知 paper ID 必须转达；task_linked=false/缺失表示任务未闭环或未知，不冒报完成。
HTTP 429 可能是配额或 session 上限，不能删除既有会话来自动腾位置。

查询退出码沿用 thin：0 完成、2 参数、3 错误、4 partial、5 待消歧。
上传退出码：0 已确认、1 配置、2 证据门失败、3 请求失败或未核实。
