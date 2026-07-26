# Paperdaily Skill 规范（v1）

> **Status**: 定稿（2026-07-26）。两个 skill 的符合性自查均已完成，结果见文末兼容矩阵 + 已知偏差。
> **适用**: 所有与 paperdaily v1 API 交互的 agent skills——现有 `paperdaily`（thin 查询）、`paperdaily-deep-research`（三阶段深读）；未来任何新 skill。
> **上游**: 协议契约见 `AGENT_PROTOCOL.md`（合规红线在其 §7）。

## 十条规范

**1. 声明件**。`SKILL.md` 头部必须声明：所需 scopes 清单（最小化——用不到的不申请）、`skill_ver`（semver）、兼容的协议版本（AGENT_PROTOCOL vN）。pre-flight 检查 `~/.paperdaily-cli/env`（`PD_BASE`/`PD_KEY`），key 缺失时停下引导用户在 web `/account` 自签，**不代签**。

**2. 阶段化 + phase-gate**。多阶段工作流必须以**落盘工件**衔接（阶段 N 的输出文件是阶段 N+1 的输入），每个阶段入口有可机检的 gate 判据，支持从任意阶段续跑。（deep-research 首创：worklist 行数/字段完备率 → fetch 覆盖率 → notes=全文数。）

**3. Provenance 强制**。一切产物带 `{skill_ver, model, created_at}`；回传 payload 的 `provenance` 字段必填。服务端凭此审计与撤销。

**4. 同意点**。任何写操作（产物上传、画像回流、任务领取外的状态变更）必须**显式征得用户一次同意**后执行；key 缺写 scope 时静默降级只读并提示，不报错、不绕行、不诱导。默认姿态是只读。

**5. 诚实账本**。获取/分析结果按状态如实记账（如 fetch 四态 `ok/already/denied/error`、claims 四态 `supported/weak/contested/gap`）；降级必须标注（全文拿不到就标 `abstract-only`，不冒充全文级）；`denied` 是终态不是故障；失败向用户如实呈现，不盖章成功。

**6. 版权**。抓取的 PDF 永留本地、永不上传/转发；上传产物只含用户自著派生分析；笔记引用原文以短句为限；机构直连/浏览器兜底 opt-in（环境变量显式开启 + 首次使用向用户确认授权网络），不含 Sci-Hub 类渠道。

**7. 检索规范**。走分层检索端点（resolve 精准指向 / search 模糊匹配 / similar 相似扩张 / facet 浏览）；`/ask` 仅用于确需服务端合成的场景（如领域综述 overview），不作通用检索通道。见 AGENT_PROTOCOL §2。

**8. 产物树契约**。产物落 `pd-research/<slug>/`，布局同构于：

```
worklist.jsonl            检索池（每行含 id + doi/arxiv_id/oa_url 至少一项）
fetch_report.jsonl        获取账本（四态）
pdfs/                     原文（永留本地，不入回传 payload）
notes/<paper_id>.md       逐篇笔记（页码锚定；abstract-only 须标注）
synthesis/                跨篇产物（claims.jsonl 四态账本 + 综合件）
report.md / overview.md   综述
```

同构即可复用统一回传通道（`upload_session.py`），新 skill 不必重造上传。扩展文件允许，核心件名不改。

**9. 行为**。尊重限流（429 + Retry-After，退避上限 3 次）；回传幂等（worklist fingerprint）；网络瞬断指数退避；长任务分阶段可中断可续跑。

**10. 演进**。本规范 semver 化；skill 升级时同步声明兼容的协议版本；规范变更与消费它的实现同 commit；破坏性变更先在已知问题清单立案。

## 兼容矩阵

| skill | skill_ver | 协议版本 | 符合性 |
|---|---|---|---|
| paperdaily（thin） | 0.2.0 | v1 | 2026-07-26 自查通过（声明件补齐；条 4 同意点整改——watchlist 三个写动作全部改为显式确认、`write:profile` 改 just-in-time 索取；偏差见下节） |
| paperdaily-deep-research | 0.3.2 | v1 | 2026-07-26 自查通过（十条逐条核对；0.3.2 补凭证/隐私整改：跨 origin 剥凭证、UA 去 PII、账本脱敏、上传面禁跳；剩余不满足项如实列于下节「已知偏差」） |

## 已知偏差（thin 0.2.0 自查，2026-07-26）

1. **条 1（声明件）——本次补齐**。原无声明块，现于 SKILL.md 头部声明
   skill_ver / 协议版本 / scopes（默认只读三件，`write:profile` 按需索取）。
2. **条 4（同意点）——本次整改**。原文把 watchlist `run` 写成「mostly
   safe」并在 pre-flight 默认索取 `write:profile`，与本条直接冲突；现改为
   create/delete/run 三个动作一律显式确认、写 scope just-in-time 索取。
3. **条 2（phase-gate）——不适用**。thin skill 是单次查询包装，无多阶段
   落盘工件链。
4. **条 3/8（provenance / 产物树）——不适用**。不产生落盘产物，输出即
   终端 markdown；无回传通道。

## 已知偏差（deep-research 自查，如实记录）

自查口径：对照上文十条逐条核对 `skills/paperdaily-deep-research/` 的 SKILL.md
与三个脚本。满足项不赘述；以下为**仍不满足或仅部分满足**的条目：

1. **条 2（phase-gate 可机检）——部分满足**。阶段 1→2（worklist 行数/字段
   完备率）与阶段 4 入口（notes/claims/report 完备性 + evidence 非空率）有
   脚本机检（fetch_fulltext 的输入校验、upload_session 的回传门）；但阶段
   2→3（fetch 覆盖率 6 成）与 3a→3b（notes 数 = 全文数）的 gate 判据只写在
   SKILL.md 文字层，由执行 agent 自查，无独立校验脚本。upload_session 的门
   是回传前兜底，不等价于阶段 3 入口 gate。
2. **条 3（provenance 强制）——部分满足**。回传 payload 的 `provenance`
   完整（kind/skill_ver/model/taxonomy/worklist_fingerprint）；
   `worklist.meta.json` 侧车（0.2.0 新增）带检索目标 + created_at。但中间
   产物（worklist.jsonl 行、overview.md、notes/*.md、synthesis/*）未逐件
   携带 `{skill_ver, model, created_at}`——「一切产物带 provenance」尚未
   全面落地；notes 的元数据依赖 agent 遵守模板，无机检。
3. **条 7（检索规范）——满足但依赖 0.8.0 服务端**。resolve/search 是
   0.8.0 新增端点；pd_worklist 对旧版服务端只报错（提示改用 taxonomy
   目标），无进一步降级路径。v1 additive-only，视为部署过渡期现象，服务端
   上线后自然消失。
4. **条 9（网络瞬断指数退避）——部分满足**。upload_session 对 429/5xx/瞬断
   做指数退避（上限 3 次、尊重 Retry-After）；pd_worklist 对 429 仅退避重试
   一次（在「上限 3 次」之内，合规但未做退避序列）；fetch_fulltext 对瞬断
   记账（`error` 态）后继续下一层/下一篇，不做同层重试——对账本语义是
   自洽的（error ≠ denied，可重跑续传），但不是字面意义的指数退避。
