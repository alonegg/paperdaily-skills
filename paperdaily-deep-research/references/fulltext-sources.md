# 全文获取渠道手册

适用阶段：Stage 2（取全文），配套 `scripts/fetch_fulltext.py`。按层级瀑布尝试，命中即停；每层的成功/失败都要记入产出账本（论文 → 命中层级 / 四态之一）。

## 瀑布层级

| 层级 | 触发条件 | 需要的配置 | 预期覆盖面 | 失败判据 |
|---|---|---|---|---|
| L0 arXiv 直连 | paper_id 或 metadata 能解出 arXiv ID | 无（公开端点 `https://arxiv.org/pdf/<id>`） | 几乎 100%（arXiv 论文均有 PDF） | 非 200，或响应不是 `%PDF` 开头（撤稿/占位页）→ `error` |
| L1 API 自带 oa_url | worklist.jsonl 行的 `oa_url` 非空（阶段 1 从 paperdaily API 的 external full-text url 抽出，兜底可能是 arxiv abs / doi.org 落地页） | 无 | 取决于上游 OA 判定，覆盖 OA 期刊 + green OA | 字段为空 → 当层 miss；有 url 但下载非 `%PDF`（落地页时先抓 `citation_pdf_url` 再试一跳）→ 当层 miss，进下一层 |
| L2 Unpaywall | 有 DOI，且 L0/L1 未命中 | `UNPAYWALL_EMAIL`（Unpaywall API 强制要求邮箱标识） | 约半数 OA 论文（聚合出版商 + 仓储白名单） | 未设 `UNPAYWALL_EMAIL` → `skipped_no_config`；`is_oa=false` 或无 `best_oa_location` → 当层 miss，进下一层；有 url 但非 `%PDF` → `error` |
| L3 PMC / Europe PMC | 有 PMID/PMCID，或 DOI 能在 Europe PMC 查到映射 | 无（公开 API） | 生物医学文献强，其他学科基本 0 命中 | 查不到 PMCID → 当层 miss；有 PMCID 但下载失败 → `error` |
| L4 出版商 TDM API | DOI 前缀属于 Elsevier / Wiley，且已申请 TDM 凭证 | `ELSEVIER_TDM_KEY` / `WILEY_TDM_TOKEN`（可选） | 仅限该出版商 + 已订阅内容 | 未配 key → `skipped_no_config`；配了 key 但 401/403 → `denied`（大概率没有该刊订阅或 TDM 权限，不要重试）；5xx/超时 → `error`（可重试） |
| L5 机构订阅直连 | `PD_FETCH_INSTITUTIONAL=1` 且用户实际处于有订阅授权的网络环境（如校园网 IP） | `PD_FETCH_INSTITUTIONAL=1` + DOI 前缀路由表（前缀 → 出版商 PDF URL 模板）；未命中前缀时退化为抓落地页 `citation_pdf_url` meta | 完全取决于机构订阅范围，同一 DOI 换网络环境结果可能不同 | 未置 1 → `skipped_optin`；置了但收到登录页/付费墙页面（非 `%PDF`，通常是 HTML 且含 "subscribe"/"purchase"/"institutional access" 关键词）→ `denied`；网络超时/连接失败 → `error` |
| L6 Playwright headless 兜底 | 前面全部 miss，且 `PD_FETCH_BROWSER=1` | `PD_FETCH_BROWSER=1`（需要能跑 headless Chromium 的环境） | 能处理需要 JS 渲染才出现下载链接的落地页；仍受限于是否有权限，不用于绕过付费墙 | 未置 1 → `skipped_optin`；渲染完成后找不到 PDF 链接，或下载内容非 `%PDF` → `error` 或 `denied`（视页面内容是否明确呈现付费墙特征） |
| L7 失败落账本 | L0-L6 全部 miss | 无 | 100% 兜底——不是拿到全文，而是保证每篇论文都有可追溯的下一步 | 不适用；产出 `doi.org/<doi>` 链接 + 前面各层的失败原因，供人工核实 |

L5 DOI 前缀路由表示例（需按实际订阅范围维护，不要照抄）：

```
10.1007  -> https://link.springer.com/content/pdf/{doi}.pdf
10.1145  -> https://dl.acm.org/doi/pdf/{doi}
10.1109  -> (无直连模板，退化到落地页抓 citation_pdf_url)
```

## 判据铁律

**校验 `%PDF` magic bytes，不信 `Content-Type` 头。** 出版商/代理常把付费墙 HTML 页面用 `Content-Type: application/pdf` 返回；必须读取文件前 4-5 字节确认是 `%PDF-` 开头才算成功，HTTP 200 不等于拿到了正确内容。

四态定义（每次尝试必须落入其中之一，写进产出账本）：

- `skipped_no_config` — 缺少必需的环境变量/凭证，这一层根本没跑。不算失败，只是没配置。
- `skipped_optin` — 该层需要显式 opt-in（L5/L6）而用户没打开开关。合规默认关闭，不是 bug。
- `denied` — 尝试过，收到明确的"无权限"信号（401/403、付费墙/登录页内容特征）。大概率是真的没有权限，**不要重试**；重试大概率触发反爬限流或违反服务条款。
- `error` — 瞬时性失败（超时、5xx、网络错误、内容格式异常）。可以重试，重试次数需设上限。

## 合规边界声明

- 只走 open access（各层公开 OA 判定）与用户自身确实拥有的机构订阅授权（L5，必须 opt-in 且限定在真实处于该网络环境时才生效）。
- **不包含、不实现、不允许接入 Sci-Hub 或任何绕过付费墙 / 未授权访问的渠道。** L6 headless 浏览器仅用于处理需要 JS 渲染才能拿到合法下载入口的场景（如某些 OA 仓储、机构库的落地页），不用于绕过登录墙或验证码。
- 某一层返回 `denied` 应视为"当前权限下拿不到全文"，直接转 L7 人工渠道，而不是切换手段继续尝试绕过。

## 常见失败排查

**denied 与 error 怎么区分**：先看状态码类别——4xx 优先判 `denied`；5xx/超时/连接错误判 `error`；HTTP 200 但内容不是 `%PDF` 时看正文关键词（"login"/"subscribe"/"purchase"/"institutional access" 等）区分是付费墙（`denied`）还是内容异常（`error`）。

**怎么确认自己是否真有订阅权限**：不要凭"学校应该订阅这本期刊"猜测。

1. 用浏览器（不是 curl）在目标网络环境下手动访问一次该 DOI 的落地页，确认是否真的能直接看到/下载 PDF。
2. 查机构图书馆的电子资源列表或 EZproxy 配置，确认该出版商/期刊在订阅范围内。
3. 如果 L5 对同一出版商连续返回 `denied`，大概率不是订阅范围问题，而是网络环境判定问题（没有真的处于授权网段，被出版商按 IP 白名单拒绝）——先确认自己的出口 IP 是否落在授权网段，而不是反复重试。

**opt-in 开关用完记得关**：`PD_FETCH_INSTITUTIONAL` / `PD_FETCH_BROWSER` 只应在明确需要、且明确知道自己处于合规网络环境时打开；任务结束后显式关掉，不要留成全局默认。
