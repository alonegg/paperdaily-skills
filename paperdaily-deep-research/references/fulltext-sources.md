# 全文获取渠道手册

适用阶段：Stage 2（取全文），配套 `scripts/fetch_fulltext.py`。按层级瀑布尝试，命中即停；每层的成功/失败都要记入产出账本（论文 → 命中层级 / 下述状态之一）。

## 瀑布层级

| 层级 | 触发条件 | 需要的配置 | 预期覆盖面 | 失败判据 |
|---|---|---|---|---|
| L0 arXiv 直连 | paper_id 或 metadata 能解出 arXiv ID | 无（公开端点 `https://arxiv.org/pdf/<id>`） | 几乎 100%（arXiv 论文均有 PDF） | 非 200，或响应不是 `%PDF` 开头（撤稿/占位页）→ `error` |
| L0b paperdaily 已解析的 PDF | worklist 行的 `pdf_url` 非空（v1 `resolved_pdf_url`，服务端 0.8.87+；老服务端为 null 自动跳过） | 无 | 平台自己的 pdf-worker 处理过的论文——arXiv 覆盖好，SSRN 车道已停故经管命中低 | 非 `%PDF` 且记录页也没有 `citation_pdf_url` → miss，进下一层 |
| L1 API 自带 oa_url | worklist.jsonl 行的 `oa_url` 非空（阶段 1 从 paperdaily API 的 external full-text url 抽出，兜底可能是 arxiv abs / doi.org 落地页） | 无 | 取决于上游 OA 判定，覆盖 OA 期刊 + green OA | 字段为空 → 当层 miss；**`oa_url` 就是裸 `doi.org/<doi>` 时直接 `skipped_no_config` 不发请求**——那不是 OA 链接，是 L5 本来就会去解析的出版商落地页，在这里试一次必 403 且会让账本开头看起来像一堵权限墙；有 url 但下载非 `%PDF`（落地页时先抓 `citation_pdf_url` 再试一跳）→ 当层 miss，进下一层 |
| L1b 回填候选 URL | worklist 行有 `urls_extra`（你跑完网页检索写回去的） | 无 | 由检索质量决定；对经管法闭源刊是**主力层** | 非 `%PDF` 且记录页也抓不到 `citation_pdf_url` → miss，进下一层 |
| L2 Unpaywall | 有 DOI，且 L0/L1 未命中 | `UNPAYWALL_EMAIL`（Unpaywall API 强制要求邮箱标识） | 约半数 OA 论文（聚合出版商 + 仓储白名单） | 未设 `UNPAYWALL_EMAIL` → `skipped_no_config`；404（不认识这个 DOI）或 `is_oa=false`/无 OA PDF 位置 → `miss`，进下一层；有 url 但非 `%PDF` → `error` |
| L3 PMC / Europe PMC | 有 PMID/PMCID，或 DOI 能在 Europe PMC 查到映射 | 无（公开 API） | 生物医学文献强，其他学科基本 0 命中 | 查不到 PMCID → `miss`；有 PMCID 但下载失败 → `error` |
| L4 出版商 TDM API | DOI 前缀属于 Elsevier / Wiley，且已申请 TDM 凭证 | `ELSEVIER_TDM_KEY` / `WILEY_TDM_TOKEN`（可选） | 仅限该出版商 + 已订阅内容 | 未配 key → `skipped_no_config`；配了 key 但 401/403 → `denied`（大概率没有该刊订阅或 TDM 权限，不要重试）；5xx/超时 → `error`（可重试） |
| L4b 标题检索兄弟版本 | 前面全 miss 且有标题 | 无（OpenAlex 免费；`S2_API_KEY` 才启用 S2 子层，keyless S2 首请求就 429） | 看学科：CS/生物有 preprint 兄弟时命中率高；**经管法实测这一层产出接近 0**，真正管用的是下面的网页检索回路 | 命中的兄弟版本没有可用 URL / 相似度或作者守卫不过 → miss |
| L5 机构订阅直连 | `PD_FETCH_INSTITUTIONAL=1` 且用户实际处于有订阅授权的网络环境（如校园网 IP） | `PD_FETCH_INSTITUTIONAL=1` + DOI 前缀路由表（前缀 → 出版商 PDF URL 模板）；未命中前缀时退化为抓落地页 `citation_pdf_url` meta | 完全取决于机构订阅范围，同一 DOI 换网络环境结果可能不同 | 未置 1 → `skipped_optin`；置了但收到登录页/付费墙页面（非 `%PDF`，通常是 HTML 且含 "subscribe"/"purchase"/"institutional access" 关键词）→ `denied`；网络超时/连接失败 → `error` |
| L6 Playwright headless 兜底 | 前面全部 miss，且 `PD_FETCH_BROWSER=1`（**与 L5 相互独立**，不需要同时开 `PD_FETCH_INSTITUTIONAL`） | `PD_FETCH_BROWSER=1`（需要能跑 headless Chromium 的环境） | 能处理需要 JS 渲染才出现下载链接的落地页（OA 仓储常见）；仍受限于是否有权限，不用于绕过付费墙 | 未置 1 → `skipped_optin`；无 oa_url 也无 doi 可渲染 → `skipped_no_config`；渲染完成后找不到 PDF 链接，或下载内容非 `%PDF` → `error` 或 `denied`（视页面内容是否明确呈现付费墙特征） |
| L6b 你自己的 Chrome 会话 | 前面全 miss，且 `PD_FETCH_CDP=1` | Chrome 开了 remote debugging（`chrome://inspect/#remote-debugging` 开关），用**日常 profile** | 覆盖 Cloudflare 挡的期刊、校园代理/SSO 后的内容、JS 拼下载链接的仓储——凡是你本人用浏览器打得开的 | 未置 1 → `skipped_optin`；连不上 Chrome → `skipped_no_config`（报错自带设置指引）；页面渲染完没有 PDF 入口，或人机验证在等待期内没被人工清掉 → `denied` |
| L7 交回网页检索 | L0-L6b 全部 miss | 无 | **对经管法这是承重层**：开放副本常以工作论文形态存在于会议站/作者主页，学术聚合器不收录、通用检索一搜就有 | 不适用；产出 `needs_web_search.jsonl`（含 `suggested_query`），你搜完把 URL 写回 worklist 的 `urls_extra` 再重跑（幂等，已下好的跳过） |
| L8 失败落账本 | 连检索回填也没结果 | 无 | 100% 兜底——不是拿到全文，而是保证每篇论文都有可追溯的下一步 | 不适用；产出 `doi.org/<doi>` 链接 + 前面各层的失败原因，供人工核实 |

L5 DOI 前缀路由表示例（需按实际订阅范围维护，不要照抄）：

```
10.1007  -> https://link.springer.com/content/pdf/{doi}.pdf
10.1145  -> https://dl.acm.org/doi/pdf/{doi}
10.1109  -> (无直连模板，退化到落地页抓 citation_pdf_url)
```

## 判据铁律

**校验 `%PDF` magic bytes，不信 `Content-Type` 头。** 出版商/代理常把付费墙 HTML 页面用 `Content-Type: application/pdf` 返回；必须读取文件前 4-5 字节确认是 `%PDF-` 开头才算成功，HTTP 200 不等于拿到了正确内容。

状态定义（每次尝试必须落入其中之一，写进产出账本）：

- `skipped_no_config` — 缺少必需的环境变量/凭证，这一层根本没跑。不算失败，只是没配置。
- `skipped_optin` — 该层需要显式 opt-in（L5/L6）而用户没打开开关。合规默认关闭，不是 bug。
- **`miss`（0.4.2 新增）** — 这一层**没有这篇论文**：404/410，或查询 API 正常응答但记录为空（Unpaywall 不认识这个 DOI、DOI 查不到 PMCID、落地页没有 `citation_pdf_url`）。**这不是权限问题**，换一层就好。
- `denied` — 尝试过，收到明确的"无权限"信号（401/403、付费墙/登录页内容特征）。大概率是真的没有权限，**不要重试**；重试大概率触发反爬限流或违反服务条款。
- `error` — 瞬时性失败（超时、5xx、429、网络错误、内容格式异常）。可以重试，重试次数需设上限。

**为什么把 `miss` 从 `denied` 里拆出来（0.4.2）**：`denied` 带着一个承诺——"问过了，被拒了，别重试"——triage 会照这个承诺行事。而 404 说的完全是另一回事：这一层没有这篇而已。混在一起会让账本看上去像一堵权限墙，把"这个 DOI Unpaywall 没收录"误读成"你没有订阅权限"，进而误导用户去开 `PD_FETCH_INSTITUTIONAL`。状态码归类：401/403 → `denied`；404/410 → `miss`；429/5xx/超时 → `error`；其余 4xx → `denied`。

## 合规边界声明

- 只走 open access（各层公开 OA 判定）与用户自身确实拥有的机构订阅授权（L5，必须 opt-in 且限定在真实处于该网络环境时才生效）。
- **不包含、不实现、不允许接入 Sci-Hub 或任何绕过付费墙 / 未授权访问的渠道。** L6 headless 浏览器仅用于处理需要 JS 渲染才能拿到合法下载入口的场景（如某些 OA 仓储、机构库的落地页），不用于绕过登录墙或验证码。
- 某一层返回 `denied` 应视为"当前权限下拿不到全文"，直接转 L7 人工渠道，而不是切换手段继续尝试绕过。

## 常见失败排查

**miss / denied / error 怎么区分**：先看状态码——404/410 判 `miss`（这层没有这篇）；401/403 及其余 4xx 判 `denied`（有这篇但不给你）；5xx/429/超时/连接错误判 `error`（可重试）。HTTP 200 但内容不是 `%PDF` 时看正文关键词（"login"/"subscribe"/"purchase"/"institutional access" 等）区分是付费墙（`denied`）还是内容异常（`error`）。

**看到满屏 `miss` 不要去开 `PD_FETCH_INSTITUTIONAL`**——那是给 `denied` 准备的开关。`miss` 多说明这批论文的 OA 覆盖本来就低（或 DOI 不在 Unpaywall/PMC 的收录范围），开机构直连也变不出来。**正确的下一步是 L7 的网页检索回路**：读 `needs_web_search.jsonl`，每篇搜一次，把 URL 写回 `urls_extra` 重跑。

**满屏 `miss` 也不代表这些论文没有公开全文。** 2026-08-02 实测（hhag020 那批 8 个失败 DOI）：

| 查询层说的 | 实际情况 |
|---|---|
| 5 篇「没有任何 OA 位置」 | 其中数篇在会议站/作者主页有完整公开 PDF |
| 2 篇「OA」指向 SSRN 落地页 | 需登录，且 SSRN 被 GFW 封 |
| 1 篇有机构仓储位置 | 只有记录页，且该仓储的 `citation_pdf_url` 写着 `localhost:4000`（对方配错了） |

结论：**闭源期刊的经管法论文，DOI 体系在构造上就查不到开放副本**——DOI 只登记在付费正刊版上，公开的那份是另一个 work（工作论文/会议稿）。配 `UNPAYWALL_EMAIL` 对这批最多救回 1/8。一次标题网页检索则直接命中。别把这类 `miss` 读成「这篇没有公开版本」。

**怎么确认自己是否真有订阅权限**：不要凭"学校应该订阅这本期刊"猜测。

1. 用浏览器（不是 curl）在目标网络环境下手动访问一次该 DOI 的落地页，确认是否真的能直接看到/下载 PDF。
2. 查机构图书馆的电子资源列表或 EZproxy 配置，确认该出版商/期刊在订阅范围内。
3. 如果 L5 对同一出版商连续返回 `denied`，大概率不是订阅范围问题，而是网络环境判定问题（没有真的处于授权网段，被出版商按 IP 白名单拒绝）——先确认自己的出口 IP 是否落在授权网段，而不是反复重试。

**opt-in 开关用完记得关**：`PD_FETCH_INSTITUTIONAL` / `PD_FETCH_BROWSER` 只应在明确需要、且明确知道自己处于合规网络环境时打开；任务结束后显式关掉，不要留成全局默认。
