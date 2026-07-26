# paperdaily-deep-research

paperdaily 平台的公开 Claude Code skill：从「一个研究领域」到「一份可
溯源的深度文献综述」——平台检索推荐 + 本地全文获取 + agent team 深度
分析，三阶段流水线。

## 安装

```sh
# 1. 把本目录整个拷到 Claude Code 的 skills 目录
cp -r paperdaily-deep-research ~/.claude/skills/

# 2. 配置 paperdaily API key（web UI → Settings → API keys → Issue key）
mkdir -p ~/.paperdaily-cli && chmod 700 ~/.paperdaily-cli
cat > ~/.paperdaily-cli/env <<'EOF'
export PD_BASE="https://www.paperdaily.org/api/v1"
export PD_KEY="pd_live_你的key"
EOF
chmod 600 ~/.paperdaily-cli/env
```

依赖：`bash`、`curl`、`jq`、`python3`（≥3.9，标准库即可）。可选：
`playwright`（浏览器兜底层才需要）。

## 用法

在 Claude Code 里直接说：

> 用 paperdaily 深度调研一下 <某领域>，把推荐论文全文下下来做一份深度综述

或分阶段调用（详见 `SKILL.md`）。

## 全文获取的合规说明

默认只走开放获取渠道（arXiv / Unpaywall / PMC 等）。设
`PD_FETCH_INSTITUTIONAL=1` 可启用出版商直连——**仅当你的机器在自己
机构有订阅授权的网络内**（如校园网）时才应开启。本 skill 不含任何绕
过付费墙的渠道。
