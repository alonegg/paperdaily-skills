# 当前限制与升级说明

更新于 2026-10-22，对应 thin 0.4.0 / deep 0.7.0。下列边界依赖实际服务端版本和数据状态。

| 情况 | 正确处理 |
|---|---|
| 429/503/超时 | 保留失败或 partial；遵循 Retry-After；不能说“未收录” |
| title 模式超时 | 保留原查询与错误；可重试或改走 resolve，不能缓存为有效空结果 |
| similar 返回不足 k | 看 truncated/覆盖说明；扩大种子或改写，不能推断语料只剩这些 |
| 论文缺作者/引用边 | 尝试 detail/context-bundle；报告缺失，不据空图断言零引用 |
| 无 scope 的 topic 查找 | 只覆盖 top 500；明确 field 或显式 deep scan，保留失败的 subfield |
| worklist 已存在 | 明确 --append、--replace 或新目录；不静默覆盖 |
| 作者重名 | 返回候选，exit 5；用 ORCID、机构及研究方向选择 |
| revision 返回 404/405 | 未保存；保留本地报告，确认父 session/部署版本；禁止改随机指纹绕过去重 |

升级变化：旧的自动学科收窄已取消；旧的固定 score 阈值与 dominant cluster 判据已取消；
`/ask` 改为显式 `--synth`；`--no-synth` 仍兼容。查询脚本统一为 Python 标准库运行时。
深读上传增加 reading_manifest 与来源哈希校验，旧会话需依据真实阅读补齐；不能批量伪造记录。

历史版本的性能数字与历史数据覆盖比例不代表当前服务。运行 `doctor` 只能确认基础读取，
不能证明所有端点、写 scope、生产发布或数据面均已验收。
