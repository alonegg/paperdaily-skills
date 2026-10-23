# 单篇补充流程（supplement.v1）

用于平台已有题录但分析缺失、不完整或需要纠正的论文。先探测服务capability；只有enabled且支持supplement.v1才使用新API。旧服务器或缺权限时明确报告，不回落旧上传。

## 任务与材料

已有任务ID时领取；无任务时先读取context并创建。下面路径相对本Skill，SESSION由用户指定。

```sh
uv run --with jsonschema python scripts/pd.py supplement context --paper PAPER_ID --out SESSION
uv run --with jsonschema python scripts/pd.py supplement create --paper PAPER_ID --goals methods,results --out SESSION
uv run --with jsonschema python scripts/pd.py supplement claim --task TASK_ID --out SESSION
uv run --with jsonschema python scripts/pd.py supplement prepare --source SELECTED_PDF --dir SESSION
```

同一个已有SESSION不要再create；claim会读取已保存任务。prepare只登记用户明确选择的常规PDF和hash、页数，不创建“已读全文”记录。需要本机pdfinfo；拒绝symlink、设备、FIFO与超过50MiB/1000页的文件。仅有摘要或HTML时也可手动登记用户已选择的本地材料，并使用对应carrier。

读取baseline.json了解平台已经有什么。核对DOI、题名、作者与版本，不按相似题名硬合并；身份uncertain不提交为本篇补充。另一个版本要记录same_work_other_version，不能把其样本量/表号套到目标版本。

## 实际阅读与证据

按用户目标读取必要页面/章节；只读Methods就记录partial，不声称阅读全文，也不改称abstract-only。
编辑supplement.json，使用如下字段：

- schema_version=supplement.v1；paper_id和baseline_hash来自task.json，不自行替换。
- provenance记录实际模型、0.8.0与实际执行时间；去除prepare的占位模型。
- outcome为supplemented/partial/insufficient_material；资料不足留reason和gap。
- sources最多3个；来源hash与local-sources.json内所选材料一致；PDF记录page_count和实际pages_read。full必须确实阅读全部页且reading_complete=true；partial不要求全篇阅读，reading_complete=false。
- 每个source的anchors带唯一anchor_id、可读location、≤300字符quote；PDF另带属于pages_read的page，HTML/parsed/abstract另带属于sections_read的section。总短引≤2000字符。
- findings最多30个：field为methods/datasets/results/limitations/claims；operation为add/correct；statement写派生判断，status为supported/weak/contested/gap。非gap引用本提交source_id/anchor_id；gap不造引文。
- correct附prior_locator，例如/analysis/methods，必须指向baseline实际字段。区分作者陈述、你的解释和仍未确认的部分。

最多30分钟主动处理、外部候选≤5、下载≤3；预算耗尽保存partial/缺口。领取后每≤10分钟运行renew，或在过期恢复时用claim --reclaim显式领取新租约；不要并行运行同任务。

```sh
python scripts/pd.py supplement renew --dir SESSION
uv run --with jsonschema python scripts/pd.py supplement validate --dir SESSION
uv run --with jsonschema python scripts/pd.py supplement submit --dir SESSION --dry-run
```

检查确认结构、材料hash与锚点关系，不证明语义蕴含或真实阅读；回到原文核对关键数字。不要从下载成功批量生成阅读记录。

## 用户授权、提交与修订

先展示changes和将发送的派生内容，说明默认私有；已有明确上传授权时继续，不重复询问。用户仅授权读取文件或领取任务，不代表同意上传。未授权不能设置allow-upload。

```sh
uv run --with jsonschema python scripts/pd.py supplement submit --dir SESSION --allow-upload
python scripts/pd.py supplement receipt --dir SESSION --verify
python scripts/pd.py supplement revise --task COMPLETED_TASK_ID --out NEW_SESSION
```

submit将receipt绑定当前API origin并回读校验内容/hash；成功状态为saved_and_read_back。201或本地receipt不能单独证明新修改已保存。网络失败保留工件、同键重试；回读失败如实说明未核验，不删receipt绕门。取消/过期/lease_lost需停止写入；不能盲重试或换key绕配额。

修订使用新目录与新任务，保留旧版本；成果不发积分、不自动画像回流。原文及本地路径不上传。平台保存补充不等于平台获得全文或更新标准分析。公开来源提案和公开共享尚未启用；不要代用户申请或承诺自动共享。
