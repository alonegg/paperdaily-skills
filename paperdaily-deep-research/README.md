# PaperDaily Deep Research 0.7.0

从检索池到带来源锚点的综述，支持单 Agent 与已获授权的并行阅读。

安装、凭证和升级见公开仓库根 README；完整执行规程在 [SKILL.md](SKILL.md)。
单独安装本目录时保留 `scripts/`、`references/`、`lib/`，不依赖另一个 Skill。

- Python ≥3.10；基本检索只需 `read:paper`，默认不调用收费综合。
- 源文件、获取账本和实际阅读记录分开；支持 PDF/HTML/解析全文和摘要。
- 上传前校验选入覆盖、来源SHA256、阅读状态与 claim 锚点。
- 可选上传只发送派生分析；修改后使用显式 revision，服务端尚无此端点时会明确失败。
- 客户端与服务端部署分开验收，安装新 Skill 不等于启用服务端新端点。

参阅 [证据契约与旧会话迁移](references/evidence-contract.md)。
