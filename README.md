# PaperDaily Skills

两个可独立安装的 Agent Skills，通过 PaperDaily v1 API 检索论文和完成有来源证据的深读。
支持 Codex、Claude Code 和使用 `.agents/skills` 的客户端；执行工具与模型由当前环境决定。

| Skill | 版本 | 用途 |
|---|---|---|
| [paperdaily](paperdaily/SKILL.md) | 0.4.0 | 标识符/标题/方向检索、作者消歧、学科近期论文、题录匹配与 watchlist |
| [paperdaily-deep-research](paperdaily-deep-research/SKILL.md) | 0.7.0 | 检索、结构化筛选、全文获取、真实阅读记录、证据综合与可选上传/修订 |

本次更新：统一有状态节流和 HTTP 错误处理；默认不调用 `/ask`；日期/scope 覆盖所有扩展通道；
批量详情与 canonical/group 去重；每个选入 ID 有笔记或明确跳过；来源哈希和锚点校验；
上传回执及显式修订；安装版本/哈希检测和真实行为回归。

## 安装与更新

依赖 Python ≥3.10。`.sh` 兼容入口另需 Bash；基础查询不需要 curl/jq 或第三方 Python 包。
全文浏览器后备能力的依赖单独见 [全文来源手册](paperdaily-deep-research/references/fulltext-sources.md)。

```sh
git clone https://github.com/alonegg/paperdaily-skills.git ~/.paperdaily-skills
cd ~/.paperdaily-skills
python3 install.py --agent codex --check
python3 install.py --agent codex --apply
# 可选 --agent claude、--agent agents 或 --agent all
```

`--check` 只读；有漂移退出 1，发布清单/哈希不合法退出 2。`--apply` 先验证发布清单，
再把旧文件夹/软链备份到 `~/.paperdaily-skill-backups/`，然后创建指向这个 clone 的软链。
备份不会留在 Skills 扫描目录里。两个 Skill 各自内含 `lib/`，也可完整复制其中一个目录独立使用。
发布哈希用于检查内容一致性，不是发布者签名。

升级时先检查 clone 的本地修改，保留自定义内容后 `git pull --ff-only`，再运行 check/apply。
安装后新建会话或重载 Skills，使客户端重新读取说明；已开启会话可能仍持有旧内容。
恢复时按 apply 输出中的 backup 路径逐项恢复，并保留当前副本以便比较。

## 凭证与最小权限

在 [账号设置](https://www.paperdaily.org/account) 自行签发 API key，保存为本机配置：

```sh
mkdir -p ~/.paperdaily-cli
chmod 700 ~/.paperdaily-cli
cat > ~/.paperdaily-cli/env <<'EOF'
export PD_BASE="https://www.paperdaily.org/api/v1"
export PD_KEY="pd_live_你的key"
EOF
chmod 600 ~/.paperdaily-cli/env
```

基础检索仅需 `read:paper`。`synth:ask` 仅供显式 `--synth`（可能使用积分）；
inbox/上传回读需 `read:reading`；领取、上传和修订需 `write:reading`；watchlist 需
`write:profile`。不因安装 Skill 自动申请全部权限。环境值优先于配置文件，配置不会作为 shell 执行。

```sh
python3 paperdaily/scenarios/pd.py doctor --json
python3 paperdaily/scenarios/pd.py search "multi agent collaboration for code review" --json
python3 paperdaily/scenarios/pd.py field 1702 --days 7 --no-extraction --json
bash paperdaily-deep-research/scripts/pd_worklist.sh --paper arxiv:2201.11903 --out pd-research/example --json
```

在 Agent 中明确调用相应 Skill，并描述研究问题和时间范围。相关性由研究内容核对，
分数、检索推荐、题录匹配均不能代替证据支持。运行时输出 complete/partial/error 的边界须保留。

## 深读与修订兼容性

已有会话升级需按 [证据契约](paperdaily-deep-research/references/evidence-contract.md)
补齐实际阅读记录。下载成功不等于全文实读；校验通过不等于语义蕴含或专家审稿通过。

上传只包含用户派生的分析，PDF/原文留本地；上传、任务领取、watchlist 写操作在用户授权范围内执行。
服务端综合与稿件补引会把输入文本发送到配置的服务，应符合用户对材料使用的授权。

`--revise` 需要服务端 `/me/reading-sessions/{id}/revisions`。
客户端已实现兼容检测；新服务端接口的部署需另行完成。旧服务返回 404/405 时明确失败，
不自动创建重复成果。首传 dedup 不会更新已保存的报告。

## 协议、开发与 MCP

- [AGENT_PROTOCOL.md](AGENT_PROTOCOL.md)：端点、scopes、限流与返回值边界。
- [SKILL_SPEC.md](SKILL_SPEC.md)：工件、证据、授权与发布约定。
- [API 快速上手](docs/quickstart-zh.md)。
- [MCP 接入](mcp/README.md)：独立的 `paperdaily-mcp` 包；工具清单以所装版本的 `tools/list` 为准。

公开 CI 使用离线 fixture 验证 HTTP 错误、节流/重试、身份去重、时间过滤、证据门、上传回执与安装。
这些 fixture 是合成测试数据，不是独立研究证据。可运行：

```sh
uv run --no-project --with pytest pytest tests/ -q
```

MIT，见 [LICENSE](LICENSE)。安全问题参见 [SECURITY.md](SECURITY.md)。
