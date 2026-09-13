# paperdaily-deep-research

paperdaily 平台的公开 Claude Code skill：从「一个研究领域」到「一份可
溯源的深度文献综述」——平台检索推荐 + 章节归纳与反向路由 + 本地全文
获取 + agent team 按节深读，四阶段流水线。

```
阶段 0   收件箱（可选） pd_inbox.sh         → 用户在 web 排的任务 → 种子论文
阶段 1a  检索          pd_worklist.sh      → worklist.jsonl（+ /ask 背景综述）
阶段 1b  归纳章节      agent（LLM）        → taxonomy.json
阶段 1c  反向路由      agent（10 篇一批）   → routing.jsonl
阶段 1d  确定性校验    pd_route_check.py   → 覆盖率/孤儿节点 + 重写 overview.md
阶段 1.5 按节收敛      与用户一起挑         → worklist.selected.jsonl
阶段 2   全文获取      fetch_fulltext.py   → pdfs/*.pdf + fetch_report.jsonl
                     pd_browser_fetch.py
阶段 3a  逐篇精读      agent team（按节派工）→ notes/<paper_id>.md
阶段 3b  按节综合      主会话               → synthesis/* + report.md
阶段 4   回传（可选）  upload_session.py   → paperdaily 工作台 reading session
```

阶段 1b–1d 是 0.6.0 新加的：先把平的候选池归纳成 3–8 个章节、把每篇论文
反向路由进去（每篇 0–3 节）、再机械校验一遍（未知 id / 未知节点 / 超过
3 节 / 孤儿节点 / 覆盖率），后面的收敛、派工、综合全部按同一个结构走。
形状与提示词见 `references/taxonomy-routing.md`，填满了的实例见
`references/examples/`。

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

阶段 1d 的校验器不需要 key、不发网络请求，可以直接拿样例跑一遍看它长什么样：

```sh
python3 scripts/pd_route_check.py --dir references/examples            # 退 0
python3 scripts/pd_route_check.py --dir references/examples \
        --routing routing.broken.jsonl                                 # 退 2，每种错各一条
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
