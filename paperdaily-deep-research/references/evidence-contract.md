# 阅读证据与修订契约（0.7.0）

这是结构契约，不是生成真实证据的捷径。不得从下载账本批量造出“已读”记录。

## 文件与分母

- `worklist.jsonl` 是检索池；存在 `worklist.selected.jsonl` 时以其 ID 集合作为阅读覆盖分母。
- 外部材料用 `external_sources.jsonl` 登记 `{id,title,reason}`；ID 在整个项目中一致。
- 每个 selected/外部 ID 必须有一份 `notes/*.md` 或有理由的 skipped；没有选择清单时检查整个 worklist。
- 笔记正文含 `**paper_id**: 原始ID`；同 ID 的重复笔记、外来 claim ID 和未登记来源会被拒绝。
- source 文件留在会话目录内；上传只发送派生分析。涉及版本、许可、来源 URL 的字段保留本地以供复核。

## reading_manifest.json

下面仅演示形状，SHA256 和内容必须来自实际材料；占位符不能通过校验。

```json
{
  "papers": [{
    "paper_id": "EX1",
    "depth": "fulltext",
    "reading_complete": true,
    "model": "实际使用的模型",
    "created_at": "2026-10-22T08:00:00Z",
    "source": {
      "path": "pdfs/EX1.pdf",
      "sha256": "替换为实际64位SHA256",
      "carrier": "binary-pdf",
      "partial": false,
      "version": "preprint",
      "url": "https://example.org/paper"
    },
    "page_count": 2,
    "pages_read": [1, 2],
    "anchors": [{"location": "p.2 Table 1", "quote": "实际短摘或忠实转述"}]
  }],
  "skipped": [{"paper_id": "EX2", "reason": "无法获得可读原文或摘要"}]
}
```

获取 SHA256 可运行 `shasum -a 256 pd-research/example/pdfs/EX1.pdf`。
PDF 页码统一为物理页；fulltext 必须实际读过 1..page_count。HTML/解析全文用稳定的
章节、表格或保留的页标记定位，carrier 为 `html-fulltext` / `parsed-fulltext`。
无法定位的全文只保留为待复核材料，不能编页码。

只有摘要时：保存实际摘要为 `sources/EX1.abstract.txt`，source 指向该文件并计算哈希，
`depth` 为 `abstract-only`、carrier 为 `abstract`；锚点写 `Abstract`，不能写 PDF 页码。
每篇至少一个可回查锚点；模型、时间、阅读状态必须反映实际执行。

## claims.jsonl

```json
{"claim":"由来源支持的具体论断","status":"supported","evidence":[{"paper_id":"EX1","location":"p.2 Table 1","quote":"实际短摘或忠实转述"}],"note":"适用条件与版本限制"}
{"claim":"尚缺少的证据或开放问题","status":"gap","evidence":[],"note":"不拿模型常识补齐"}
```

四态 supported/weak/contested/gap。每条非 gap claim 至少一条证据；evidence 必须与该论文
manifest 的 `(location,quote)` 对应。单条 quote ≤500 字符，每 claim ≤20 evidence，
单篇笔记 ≤64KiB，papers ≤100，claims ≤200，报告/overview 各 ≤2MiB。
短摘上限不代表允许复制大段原文，应以自著分析和必要短引为主。

检查命令：`python3 scripts/upload_session.py --dir SESSION --dry-run`。
校验不做语义蕴含判断，不会因结构通过就宣布研究结论可靠。

## 旧会话迁移与重传

旧工件可继续本地阅读；上传前根据实际阅读补齐 manifest，修正选入清单及外部来源登记。
不能从 `status=ok` 推导 `reading_complete=true`。不清楚实际读过哪些页就重新核验。

同 worklist 首传 dedup 只确认既有对象，脚本回读核对，写 scope 之外还需要 `read:reading`。
内容变化后 `--revise ID` 生成不可变新 session。修订接口缺失/父件不属于当前用户时明确失败。
`upload_receipt.json` 按服务地址绑定，不包含 key。保留原回执；不要手改指纹或删除回执来绕过检查。
