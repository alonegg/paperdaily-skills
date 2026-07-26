# paperdaily-skills

[paperdaily](https://www.paperdaily.org) 是一个"重后端、轻前端"的每日论文系统：把论文流入知识图谱，按你的兴趣画像做每日 4 层推荐，并提供可编程的 v1 API。本仓库是它的公开 Claude Code skills 与 agent 协议文档。

> **English.** This repository ships the public Claude Code skills and agent-protocol docs for [paperdaily](https://www.paperdaily.org), a daily research-paper recommendation platform with a knowledge-graph backend and a bearer-key v1 API. Two skills are included: `paperdaily` (thin read-only query CLI: field digests, author lookups, watchlists) and `paperdaily-deep-research` (a three-stage pipeline that goes from a research field or a single seed paper to a citation-grounded literature review — platform retrieval, local open-access full-text fetching, and agent-team deep reading/synthesis, with optional upload of your own analysis back to the paperdaily workbench). All full-text fetching is OA-first, institutional access is strictly opt-in, downloaded PDFs never leave your machine, and nothing here touches Sci-Hub.

## Skills 清单

| 目录 | 用途 |
|---|---|
| [`paperdaily/`](paperdaily/) | thin 查询 skill：领域/学科/话题日报、作者近作、watchlist 周追踪。只读，不改画像。 |
| [`paperdaily-deep-research/`](paperdaily-deep-research/) | 深度调研 skill：从「一个领域 / 一篇种子论文」到「可溯源的深度文献综述」——平台检索推荐 → 本地 OA 全文瀑布下载 → agent team 逐篇精读 + 跨篇综合；可选把你自己的分析产物回传 paperdaily 工作台。 |

配套文档：

| 文件 | 内容 |
|---|---|
| [`AGENT_PROTOCOL.md`](AGENT_PROTOCOL.md) | agent ↔ paperdaily 服务端的对外契约（scopes、分层检索、回传、任务收件箱、行为规范、合规红线）。 |
| [`SKILL_SPEC.md`](SKILL_SPEC.md) | Skill 编写十条规范（声明件、phase-gate、provenance、同意点、诚实账本……）与兼容矩阵。 |
| [`docs/quickstart-zh.md`](docs/quickstart-zh.md) | v1 API 快速上手（签 key、调用示例、scope、限流、错误码、FAQ）。 |

## 安装（三步）

```sh
# 1. clone
git clone https://github.com/alonegg/paperdaily-skills.git
cd paperdaily-skills

# 2. 把 skill 软链进 Claude Code 的 skills 目录（两个都装或只装一个）
ln -s "$(pwd)/paperdaily-deep-research" ~/.claude/skills/
ln -s "$(pwd)/paperdaily" ~/.claude/skills/

# 3. 在 https://www.paperdaily.org/account 签发 API key，写入 ~/.paperdaily-cli/env
mkdir -p ~/.paperdaily-cli && chmod 700 ~/.paperdaily-cli
cat > ~/.paperdaily-cli/env <<'EOF'
export PD_BASE="https://www.paperdaily.org/api/v1"
export PD_KEY="pd_live_你的key"
EOF
chmod 600 ~/.paperdaily-cli/env
```

依赖：`bash`、`curl`、`jq`、`python3`（≥3.9，标准库即可）。可选：`playwright`（deep-research 的浏览器兜底层才需要）。

key 的 scopes 至少勾 `read:digest, read:paper, synth:ask`；要用 deep-research 的回传功能再勾 `write:reading`。明文只显示一次，请立即存好。

## 快速开始

在 Claude Code 里直接说：

> /paperdaily-deep-research --paper arxiv:2605.10419

（就着这一篇种子论文向外扩张 → 下载全文 → 精读 → 出一份带页码引用的综述报告。）或者用自然语言：

> 用 paperdaily 深度调研一下 graph neural fraud detection，把推荐论文全文下下来做一份深度综述

thin 查询 skill 的日常用法：

> AI 今天有什么新论文？ / Bengio 最近发了啥？

## 协议与规范

- 想写自己的 paperdaily agent/skill → 先读 [`AGENT_PROTOCOL.md`](AGENT_PROTOCOL.md)（检索一律走 resolve/search 分层端点，`/ask` 只用于服务端合成）。
- 想给本仓库贡献新 skill → 对照 [`SKILL_SPEC.md`](SKILL_SPEC.md) 的十条规范逐条自查。
- 只想裸调 API → [`docs/quickstart-zh.md`](docs/quickstart-zh.md)，OpenAPI 在 <https://www.paperdaily.org/api/v1/openapi.json>。

## 合规红线（摘录）

摘自 `AGENT_PROTOCOL.md` §7，全部 skills 强制遵守：

1. **不含 Sci-Hub**——不包含、不实现、不允许接入任何绕过付费墙/未授权访问的渠道。
2. **机构直连 opt-in**——出版商直连（`PD_FETCH_INSTITUTIONAL=1`）与浏览器兜底（`PD_FETCH_BROWSER=1`）默认关闭；仅当你的机器确实处于自己机构有订阅授权的网络内才应开启，开启前 skill 会向你确认。
3. **PDF 永留本地**——抓取的原文永不上传/转发；回传 paperdaily 的只有你自己的派生分析（笔记/论断账本/报告），且必须经你显式同意。
4. `denied`（付费墙拒绝）是终态不是故障，不做技术性重试。

## License

MIT，见 [LICENSE](LICENSE)。
