# Paperdaily Skill 规范（v1）

> **Status**: 定稿（2026-07-26，随 0.8.0）。`paperdaily-deep-research` 的符合性自查已做（结果见文末兼容矩阵 + 已知偏差）；`paperdaily`（thin）自查待做。
> **适用**: 所有与 paperdaily v1 API 交互的 agent skills——现有 `paperdaily`（thin 查询）、`paperdaily-deep-research`（三阶段深读）；未来任何新 skill。
> **上游**: 协议契约见仓库根 `AGENT_PROTOCOL.md`；架构与合规红线的设计权威在 paperdaily 服务端内部设计文档（未随本仓库公开），本文与 `AGENT_PROTOCOL.md` 是其对外呈现层。

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

**10. 演进**。本规范 semver 化；skill 升级时同步声明兼容的协议版本；规范变更与消费它的实现同 commit；破坏性变更先在 `V1_KNOWN_ISSUES.md` 立案。

## 兼容矩阵

| skill | skill_ver | 协议版本 | 符合性 |
|---|---|---|---|
| paperdaily（thin） | — | v1 | 待自查（未随 0.8.0 批次，声明件与检索段未核对） |
| paperdaily-deep-research | 0.2.0 | v1 | 0.8.0 自查通过（2026-07-26；十条逐条核对，剩余不满足项如实列于下节「已知偏差」） |

## 已知偏差（deep-research 0.8.0 自查，如实记录）

自查口径：对照上文十条逐条核对 `paperdaily-deep-research/` 的 SKILL.md
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
