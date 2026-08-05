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
`poppler`（`pdftotext`，读 PDF 的兜底路径）、`playwright`（无会话的 headless 兜底层）。

**取不到全文时的两个开关**（都不需要装东西）：

```sh
# ① 浏览器层：借你自己已经登录好的 Chrome 会话
#    先开一次 chrome://inspect/#remote-debugging 的开关（用日常 profile）
python3 scripts/pd_browser_fetch.py --list                # 确认连得上
export PD_FETCH_CDP=1                                     # 让瀑布自动用它

# ② 先分诊再决定读哪些（只查不下载）
python3 scripts/fetch_fulltext.py --worklist w.jsonl --out pdfs/ --triage
```

闭源期刊的经管法论文，DOI 只登记在付费正刊版上，Unpaywall/PMC 一律
`miss`——但同一篇的工作论文/会议稿往往公开可得。脚本穷尽后会写
`needs_web_search.jsonl`，你（或 agent）每篇搜一次、把 URL 填回 worklist 行的
`urls_extra` 再重跑即可。详见 `SKILL.md` 阶段 2。

## 用法

在 Claude Code 里直接说：

> 用 paperdaily 深度调研一下 <某领域>，把推荐论文全文下下来做一份深度综述

或分阶段调用（详见 `SKILL.md`）。

## 全文获取的合规说明

默认只走开放获取渠道（arXiv / Unpaywall / PMC / 公开的工作论文副本等）。

- `PD_FETCH_INSTITUTIONAL=1` 启用出版商直连——**仅当你的机器在自己
  机构有订阅授权的网络内**（如校园网）时才应开启。
- `PD_FETCH_CDP=1` 用的是**你自己的浏览器、你自己的登录、你自己的
  权限**：它不接触密码、不导出 cookie，遇到人机验证或登录墙会**停下
  来等你本人处理**——agent 不解验证码。

本 skill 不含任何绕过付费墙的渠道。
