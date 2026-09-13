#!/usr/bin/env python3
"""
pd_route_check.py — 阶段 1d：taxonomy + 反向路由的确定性校验（纯标准库）。

阶段 1b 让 LLM 归纳章节（`taxonomy.json`），阶段 1c 让 LLM 把每篇论文反向路由
到 0-3 个章节（`routing.jsonl`）。**这两步都是 LLM 在写 id**，而 LLM 写 id 的
失败方式恰好是最难用眼睛看出来的那一类：编一个不存在的 paper_id、把节点 id
写成节点标题、同一篇重复出现、给一篇挂 5 个节点、或者干脆只路由了三分之一
的论文——每一种都会让后面的「按节精读 → 按节综合」看起来在正常工作，而实际
覆盖面早就塌了。所以这一步必须是机械的，不能是「agent 自己看一眼」。

本脚本只读三个文件、不发任何网络请求、不改任何东西：

    worklist.jsonl   阶段 1a 的候选池（每行 .id）——id 的**唯一真相**
    taxonomy.json    阶段 1b 的章节表
    routing.jsonl    阶段 1c 的路由表（每行 {"paper_id": …, "nodes": [...]}）

检查项与退出码
──────────────────────────────────────────────────────────────────────────
  0  通过（可能带 warning）
  1  coverage 低于目标 → **重跑阶段 1c 的路由**（不是重跑检索，也不是降目标）
  2  结构性错误（未知 paper_id / 未知节点 / 超过 max-targets / 重复 /
     孤儿 analytical 节点 / taxonomy 本身不合法）→ 修文件后重跑本脚本
  3  用法或输入错误（文件缺失、JSON 坏掉、CLI 参数不合法）

**结构性错误优先于 coverage**：路由表本身不合法时算出来的 coverage 没有意义
（比如把一半的 paper_id 写错，覆盖率会「看起来」很低，而真正要修的是 id）。
所以两类同时出现时退 2 而不是 1。

用法
──────────────────────────────────────────────────────────────────────────
  python3 scripts/pd_route_check.py --dir pd-research/<slug>/
  python3 scripts/pd_route_check.py --dir pd-research/<slug>/ --json
  # 阶段 1.5 收敛之后，用同一把尺子量收敛后的那批：
  python3 scripts/pd_route_check.py --dir pd-research/<slug>/ \
      --worklist worklist.selected.jsonl --routing routing.selected.jsonl

JSON 形状见 references/taxonomy-routing.md（与服务端 `llm/survey/` 同一形状）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ── 默认参数（与 references/taxonomy-routing.md 及服务端 llm/survey 对齐）──
DEFAULT_COVERAGE_TARGET = 0.6
DEFAULT_MAX_TARGETS = 3
DEFAULT_MAX_NODES = 8
DEFAULT_MIN_NODES = 3
DEFAULT_MAX_DEPTH = 2
DEFAULT_MIN_PER_NODE = 2  # 阶段 1.5「每个 analytical 节点保留 ≥2 篇」的同一个数

ROLES = ("analytical", "reflective", "navigational")

EXIT_OK = 0
EXIT_COVERAGE = 1
EXIT_STRUCTURAL = 2
EXIT_USAGE = 3


class InputError(Exception):
    """文件缺失 / JSON 坏掉 / 形状完全不对 —— 退出码 3，不是「校验没通过」。"""


# ─────────────────────────────────────────────────────────────────────────
# 载入
# ─────────────────────────────────────────────────────────────────────────
def load_worklist_ids(path: str) -> List[str]:
    """worklist.jsonl → 有序 id 列表（重复 id 直接报 InputError）。

    worklist 是全流程主键的来源。它自己带重复的话，后面每一个「按 id 对齐」的
    断言都站在流沙上，所以这里不容忍、也不静默去重。
    """
    ids: List[str] = []
    seen: Set[str] = set()
    for lineno, rec in _iter_jsonl(path):
        if not isinstance(rec, dict):
            raise InputError("%s 第 %d 行不是 JSON 对象" % (path, lineno))
        pid = rec.get("id")
        if not isinstance(pid, str) or not pid.strip():
            raise InputError("%s 第 %d 行缺少非空的 .id" % (path, lineno))
        pid = pid.strip()
        if pid in seen:
            raise InputError("%s 第 %d 行的 id 重复出现：%s" % (path, lineno, pid))
        seen.add(pid)
        ids.append(pid)
    if not ids:
        raise InputError("%s 里一行都没有" % path)
    return ids


def load_taxonomy(path: str) -> Dict[str, Any]:
    """taxonomy.json → dict。

    接受两种写法：规范形态 `{"topic": …, "nodes": [...]}`，以及裸的节点数组
    `[...]`（LLM 常直接吐数组）。裸数组会被包装成规范形态，`topic` 为空串。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except FileNotFoundError:
        raise InputError("找不到 %s" % path)
    except ValueError as exc:
        raise InputError("%s 不是合法 JSON：%s" % (path, exc))
    if isinstance(obj, list):
        return {"topic": "", "nodes": obj}
    if not isinstance(obj, dict):
        raise InputError("%s 顶层既不是对象也不是数组" % path)
    nodes = obj.get("nodes")
    if not isinstance(nodes, list):
        raise InputError("%s 缺少 .nodes 数组" % path)
    return obj


def load_routing(path: str) -> List[Tuple[int, Any]]:
    """routing.jsonl → [(行号, 记录)]；行级坏 JSON 在校验阶段报，不在这里抛。"""
    return list(_iter_jsonl(path, tolerate_bad_json=True))


def _iter_jsonl(
    path: str, tolerate_bad_json: bool = False
) -> Iterable[Tuple[int, Any]]:
    if not os.path.isfile(path):
        raise InputError("找不到 %s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield lineno, json.loads(line)
            except ValueError as exc:
                if tolerate_bad_json:
                    yield lineno, _BadJSON(str(exc))
                else:
                    raise InputError("%s 第 %d 行不是合法 JSON：%s" % (path, lineno, exc))


class _BadJSON:
    def __init__(self, message: str) -> None:
        self.message = message


# ─────────────────────────────────────────────────────────────────────────
# taxonomy 校验
# ─────────────────────────────────────────────────────────────────────────
def check_taxonomy(
    taxonomy: Dict[str, Any],
    *,
    min_nodes: int = DEFAULT_MIN_NODES,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> Tuple[List[str], List[str], Dict[str, Dict[str, Any]]]:
    """返回 (errors, warnings, 按 id 索引的节点表)。

    索引表即便在有 error 时也尽量填满——路由校验还要用它报「未知节点」，
    半张表比空表有用。
    """
    errors: List[str] = []
    warnings: List[str] = []
    index: Dict[str, Dict[str, Any]] = {}

    raw_nodes = taxonomy.get("nodes") or []
    for i, node in enumerate(raw_nodes):
        where = "nodes[%d]" % i
        if not isinstance(node, dict):
            errors.append("%s 不是 JSON 对象" % where)
            continue
        nid = node.get("id")
        if not isinstance(nid, str) or not nid.strip():
            errors.append("%s 缺少非空的 .id" % where)
            continue
        nid = nid.strip()
        if nid in index:
            errors.append("节点 id 重复：%s" % nid)
            continue
        title = node.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append("节点 %s 缺少非空的 .title" % nid)
        role = node.get("role")
        if role not in ROLES:
            errors.append(
                "节点 %s 的 role=%r 不在 %s 里" % (nid, role, "/".join(ROLES))
            )
        desc = node.get("description")
        if not isinstance(desc, str) or not desc.strip():
            warnings.append("节点 %s 的 description 是空的" % nid)
        kq = node.get("key_questions")
        if kq is None:
            kq = []
        if not isinstance(kq, list):
            errors.append("节点 %s 的 key_questions 不是数组" % nid)
            kq = []
        elif role == "analytical" and not kq:
            warnings.append(
                "节点 %s 是 analytical 但没有 key_questions —— "
                "阶段 3a 的派工要靠它，补上" % nid
            )
        parent = node.get("parent")
        if parent is not None and (not isinstance(parent, str) or not parent.strip()):
            errors.append("节点 %s 的 parent 既不是 null 也不是非空字符串" % nid)
            parent = None
        index[nid] = {
            "id": nid,
            "title": title if isinstance(title, str) else "",
            "description": desc if isinstance(desc, str) else "",
            "role": role,
            "key_questions": kq,
            "parent": parent.strip() if isinstance(parent, str) else None,
        }

    n = len(index)
    if n < min_nodes:
        errors.append(
            "只有 %d 个合法节点，低于下限 %d —— 章节切得太粗，"
            "按节精读和按节综合都没什么可分的" % (n, min_nodes)
        )
    if n > max_nodes:
        errors.append(
            "有 %d 个节点，超过上限 %d —— 章节切太碎会让每节只剩一两篇，"
            "综合阶段写不出对比" % (n, max_nodes)
        )

    # parent 存在性 + 成树 + 深度
    for nid, node in index.items():
        parent = node["parent"]
        if parent is not None and parent not in index:
            errors.append("节点 %s 的 parent=%s 不存在" % (nid, parent))

    depth_of: Dict[str, int] = {}
    for nid in index:
        seen_path: List[str] = []
        cur: Optional[str] = nid
        depth = 1
        cyclic = False
        while cur is not None:
            if cur in seen_path:
                errors.append(
                    "节点 %s 的 parent 链成环：%s" % (nid, " → ".join(seen_path + [cur]))
                )
                cyclic = True
                break
            seen_path.append(cur)
            nxt = index.get(cur, {}).get("parent")
            if nxt is None or nxt not in index:
                break
            cur = nxt
            depth += 1
        if not cyclic:
            depth_of[nid] = depth
            if depth > max_depth:
                errors.append(
                    "节点 %s 的深度是 %d，超过上限 %d" % (nid, depth, max_depth)
                )

    if index and not any(nd["role"] == "analytical" for nd in index.values()):
        errors.append(
            "没有任何 analytical 节点 —— 全是背景/反思节的 taxonomy "
            "撑不起一份综述的主体"
        )

    return errors, warnings, index


# ─────────────────────────────────────────────────────────────────────────
# routing 校验 + coverage
# ─────────────────────────────────────────────────────────────────────────
def check_routing(
    records: Sequence[Tuple[int, Any]],
    worklist_ids: Sequence[str],
    node_index: Dict[str, Dict[str, Any]],
    *,
    max_targets: int = DEFAULT_MAX_TARGETS,
) -> Tuple[List[str], List[str], Dict[str, List[str]]]:
    """返回 (errors, warnings, {paper_id: [node_id, …]} 清洗后的路由表)。

    清洗后的表只保留「合法行 + 合法节点」，coverage 与各节篇数都按它算——
    这样「报了错还给出一个漂亮的覆盖率」这种事不可能发生。
    """
    errors: List[str] = []
    warnings: List[str] = []
    allowed_papers = set(worklist_ids)
    routed: Dict[str, List[str]] = {}

    for lineno, rec in records:
        if isinstance(rec, _BadJSON):
            errors.append("routing 第 %d 行不是合法 JSON：%s" % (lineno, rec.message))
            continue
        if not isinstance(rec, dict):
            errors.append("routing 第 %d 行不是 JSON 对象" % lineno)
            continue
        pid = rec.get("paper_id")
        if not isinstance(pid, str) or not pid.strip():
            errors.append("routing 第 %d 行缺少非空的 .paper_id" % lineno)
            continue
        pid = pid.strip()
        if pid not in allowed_papers:
            errors.append(
                "routing 第 %d 行的 paper_id=%s 不在 worklist 里（未知 id）"
                % (lineno, pid)
            )
            continue
        if pid in routed:
            errors.append(
                "routing 第 %d 行的 paper_id=%s 重复出现 —— 一篇论文只能有一行"
                % (lineno, pid)
            )
            continue
        nodes_raw = rec.get("nodes")
        if nodes_raw is None:
            nodes_raw = []
        if not isinstance(nodes_raw, list):
            errors.append("routing 第 %d 行的 nodes 不是数组" % lineno)
            continue
        if len(nodes_raw) > max_targets:
            errors.append(
                "routing 第 %d 行（%s）挂了 %d 个节点，上限是 %d"
                % (lineno, pid, len(nodes_raw), max_targets)
            )
            continue
        clean: List[str] = []
        bad = False
        for item in nodes_raw:
            if not isinstance(item, str) or not item.strip():
                errors.append("routing 第 %d 行（%s）的 nodes 里有非字符串项" % (lineno, pid))
                bad = True
                break
            nid = item.strip()
            if nid not in node_index:
                errors.append(
                    "routing 第 %d 行（%s）指向未知节点 %s" % (lineno, pid, nid)
                )
                bad = True
                break
            if nid in clean:
                errors.append(
                    "routing 第 %d 行（%s）重复挂了同一个节点 %s" % (lineno, pid, nid)
                )
                bad = True
                break
            clean.append(nid)
        if bad:
            continue
        routed[pid] = clean

    missing = [pid for pid in worklist_ids if pid not in routed]
    if missing:
        warnings.append(
            "worklist 里有 %d 篇论文在 routing 里没有对应行（按未路由计入 coverage）：%s"
            % (len(missing), ", ".join(missing[:5]) + (" …" if len(missing) > 5 else ""))
        )
    return errors, warnings, routed


def compute_stats(
    worklist_ids: Sequence[str],
    node_index: Dict[str, Dict[str, Any]],
    routed: Dict[str, List[str]],
) -> Dict[str, Any]:
    """coverage / 每节篇数 / 主属篇数 / 未路由清单。

    两套计数刻意都报：
      node_counts    —— 论文出现在该节（任意位置）的数量，用来判孤儿节点；
      primary_counts —— 论文的**首个**节点是该节的数量，阶段 3a 按它派 agent
                        （一篇论文只被读一次，见 SKILL.md 阶段 3a「主属节点」）。
    """
    total = len(worklist_ids)
    node_counts = {nid: 0 for nid in node_index}
    primary_counts = {nid: 0 for nid in node_index}
    n_routed = 0
    for pid in worklist_ids:
        nodes = routed.get(pid) or []
        if not nodes:
            continue
        n_routed += 1
        primary_counts[nodes[0]] += 1
        for nid in nodes:
            node_counts[nid] += 1
    coverage = (n_routed / total) if total else 0.0

    # 子树累加：只做分组用的父节点，自己没被直接挂论文、但孩子挂满了，不是孤儿。
    children = _children_map(node_index)
    subtree_counts = {
        nid: _subtree_sum(nid, node_counts, children, set()) for nid in node_index
    }
    subtree_primary_counts = {
        nid: _subtree_sum(nid, primary_counts, children, set()) for nid in node_index
    }
    has_children = {nid: bool(children.get(nid)) for nid in node_index}

    return {
        "n_papers": total,
        "n_routed": n_routed,
        "coverage": coverage,
        "n_nodes": len(node_index),
        "node_counts": node_counts,
        "primary_counts": primary_counts,
        "subtree_counts": subtree_counts,
        "subtree_primary_counts": subtree_primary_counts,
        "has_children": has_children,
        "unrouted": [pid for pid in worklist_ids if not routed.get(pid)],
    }


def _children_map(node_index: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    children: Dict[str, List[str]] = {nid: [] for nid in node_index}
    for nid, node in node_index.items():
        parent = node["parent"]
        if parent in children:
            children[parent].append(nid)
    return children


def _subtree_sum(
    nid: str,
    counts: Dict[str, int],
    children: Dict[str, List[str]],
    seen: Set[str],
) -> int:
    # 环已在 taxonomy 校验里报过，这里只保证不死循环。
    if nid in seen:
        return 0
    seen.add(nid)
    return counts.get(nid, 0) + sum(
        _subtree_sum(c, counts, children, seen) for c in children.get(nid, [])
    )


def check_nodes_populated(
    node_index: Dict[str, Dict[str, Any]],
    stats: Dict[str, Any],
    *,
    min_per_node: int = DEFAULT_MIN_PER_NODE,
) -> Tuple[List[str], List[str]]:
    """孤儿节点与「有论文但没人读」的节点。

    判据一律用 `compute_stats` 算好的**子树**计数，与报告表里印出来的数字
    同源——否则会出现「表里 n2 写着 0 篇却没报孤儿」这种读者无法复核的结论。
    """
    errors: List[str] = []
    warnings: List[str] = []
    subtree_counts: Dict[str, int] = stats["subtree_counts"]
    subtree_primary: Dict[str, int] = stats["subtree_primary_counts"]

    for nid, node in sorted(node_index.items()):
        total_here = subtree_counts.get(nid, 0)
        primary_here = subtree_primary.get(nid, 0)
        if node["role"] == "analytical":
            if total_here == 0:
                errors.append(
                    "analytical 节点 %s（%s）一篇论文都没分到（孤儿节点）—— "
                    "要么它不该存在，要么 1c 路由漏了" % (nid, node["title"])
                )
            elif primary_here == 0:
                warnings.append(
                    "analytical 节点 %s 只拿到次属论文（首位节点都是别人）—— "
                    "阶段 3a 按主属节点派工，这一节不会有人读；"
                    "把某篇的 nodes 顺序调过来，或者并掉这一节" % nid
                )
            elif total_here < min_per_node:
                warnings.append(
                    "analytical 节点 %s 只有 %d 篇（阶段 1.5 的收敛下限是 %d）"
                    % (nid, total_here, min_per_node)
                )
        else:
            if total_here == 0:
                warnings.append(
                    "%s 节点 %s 没有分到论文 —— %s 节点允许空着（背景/反思），"
                    "但确认这是你的本意" % (node["role"], nid, node["role"])
                )
    return errors, warnings


# ─────────────────────────────────────────────────────────────────────────
# 顶层
# ─────────────────────────────────────────────────────────────────────────
def run_checks(
    worklist_ids: Sequence[str],
    taxonomy: Dict[str, Any],
    routing_records: Sequence[Tuple[int, Any]],
    *,
    coverage_target: float = DEFAULT_COVERAGE_TARGET,
    max_targets: int = DEFAULT_MAX_TARGETS,
    min_nodes: int = DEFAULT_MIN_NODES,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    min_per_node: int = DEFAULT_MIN_PER_NODE,
) -> Dict[str, Any]:
    """跑完全部检查，返回可直接序列化的报告 dict（含 `exit_code`）。"""
    errors: List[str] = []
    warnings: List[str] = []

    tax_errors, tax_warnings, node_index = check_taxonomy(
        taxonomy, min_nodes=min_nodes, max_nodes=max_nodes, max_depth=max_depth
    )
    errors += tax_errors
    warnings += tax_warnings

    route_errors, route_warnings, routed = check_routing(
        routing_records, worklist_ids, node_index, max_targets=max_targets
    )
    errors += route_errors
    warnings += route_warnings

    stats = compute_stats(worklist_ids, node_index, routed)

    node_errors, node_warnings = check_nodes_populated(
        node_index, stats, min_per_node=min_per_node
    )
    errors += node_errors
    warnings += node_warnings

    coverage_ok = stats["coverage"] >= coverage_target
    if errors:
        exit_code = EXIT_STRUCTURAL
    elif not coverage_ok:
        exit_code = EXIT_COVERAGE
    else:
        exit_code = EXIT_OK

    return {
        "topic": taxonomy.get("topic") or "",
        "nodes": node_index,
        "errors": errors,
        "warnings": warnings,
        "coverage_target": coverage_target,
        "coverage_ok": coverage_ok,
        "exit_code": exit_code,
        **stats,
    }


def format_report(report: Dict[str, Any], *, show_nodes: bool = True) -> str:
    node_index: Dict[str, Dict[str, Any]] = report.get("nodes") or {}
    if not show_nodes:
        node_index = {}
    lines: List[str] = []
    topic = report.get("topic")
    lines.append("== pd_route_check ==%s" % ((" " + topic) if topic else ""))
    lines.append(
        "nodes=%d  papers=%d  routed=%d  coverage=%.2f (target %.2f)"
        % (
            report["n_nodes"],
            report["n_papers"],
            report["n_routed"],
            report["coverage"],
            report["coverage_target"],
        )
    )
    if node_index:
        lines.append("")
        lines.append("每节篇数（主属 / 任意位置；父节点另标子树合计）：")
        for nid in sorted(node_index):
            node = node_index[nid]
            sub = ""
            if report.get("has_children", {}).get(nid):
                sub = " (子树 %d/%d)" % (
                    report["subtree_primary_counts"].get(nid, 0),
                    report["subtree_counts"].get(nid, 0),
                )
            lines.append(
                "  %-14s %-12s %3d / %-3d%-14s %s"
                % (
                    nid,
                    node["role"] or "?",
                    report["primary_counts"].get(nid, 0),
                    report["node_counts"].get(nid, 0),
                    sub,
                    node["title"],
                )
            )
    if report["unrouted"]:
        shown = report["unrouted"][:10]
        lines.append("")
        lines.append(
            "未路由 %d 篇：%s%s"
            % (
                len(report["unrouted"]),
                ", ".join(shown),
                " …" if len(report["unrouted"]) > len(shown) else "",
            )
        )
    for w in report["warnings"]:
        lines.append("WARN  " + w)
    for e in report["errors"]:
        lines.append("ERROR " + e)

    lines.append("")
    if report["exit_code"] == EXIT_STRUCTURAL:
        lines.append(
            "结论：路由表结构不合法（%d 处）。先按上面的 ERROR 修 "
            "taxonomy.json / routing.jsonl 再重跑本脚本；**在修好之前上面那个 "
            "coverage 不作数**。" % len(report["errors"])
        )
    elif report["exit_code"] == EXIT_COVERAGE:
        lines.append(
            "结论：coverage %.2f < %.2f —— **重跑阶段 1c 的路由**（同一份 "
            "taxonomy、把未路由的那批单独再送一轮，多轮取覆盖率最高的一次）。"
            "不要靠降低目标或删论文来过这一关：没被路由的论文在后面的"
            "「按节精读 / 按节综合」里等于不存在。若重跑两轮仍上不去，说明是 "
            "taxonomy 的切法没盖住这个池子，回阶段 1b 改章节。"
            % (report["coverage"], report["coverage_target"])
        )
    else:
        lines.append("结论：通过。可以进阶段 1.5（按节收敛）。")
    return "\n".join(lines)


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("%r 不是整数" % value)
    if n < 1:
        raise argparse.ArgumentTypeError("%r 必须 ≥ 1" % value)
    return n


def _ratio(value: str) -> float:
    try:
        f = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("%r 不是小数" % value)
    if not 0.0 <= f <= 1.0:
        raise argparse.ArgumentTypeError("%r 必须落在 [0, 1]" % value)
    return f


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pd_route_check.py",
        description="阶段 1d：校验 taxonomy.json + routing.jsonl（纯标准库，只读）。",
        epilog=(
            "退出码：0 通过 / 1 coverage 不够（重跑阶段 1c 路由）/ "
            "2 结构性错误（修文件）/ 3 用法或输入错误。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dir", default=".", help="pd-research/<slug>/ 目录（默认当前目录）")
    ap.add_argument("--worklist", default="worklist.jsonl", help="相对 --dir 或绝对路径")
    ap.add_argument("--taxonomy", default="taxonomy.json", help="相对 --dir 或绝对路径")
    ap.add_argument("--routing", default="routing.jsonl", help="相对 --dir 或绝对路径")
    ap.add_argument(
        "--coverage-target",
        type=_ratio,
        default=DEFAULT_COVERAGE_TARGET,
        help="覆盖率下限（默认 %.2f）" % DEFAULT_COVERAGE_TARGET,
    )
    ap.add_argument(
        "--max-targets",
        type=_positive_int,
        default=DEFAULT_MAX_TARGETS,
        help="每篇论文最多几个节点（默认 %d）" % DEFAULT_MAX_TARGETS,
    )
    ap.add_argument(
        "--min-nodes",
        type=_positive_int,
        default=DEFAULT_MIN_NODES,
        help="节点数下限（默认 %d）" % DEFAULT_MIN_NODES,
    )
    ap.add_argument(
        "--max-nodes",
        type=_positive_int,
        default=DEFAULT_MAX_NODES,
        help="节点数上限（默认 %d）" % DEFAULT_MAX_NODES,
    )
    ap.add_argument(
        "--max-depth",
        type=_positive_int,
        default=DEFAULT_MAX_DEPTH,
        help="taxonomy 最大层数（默认 %d）" % DEFAULT_MAX_DEPTH,
    )
    ap.add_argument(
        "--min-per-node",
        type=_positive_int,
        default=DEFAULT_MIN_PER_NODE,
        help="analytical 节点的建议篇数下限，低于只 WARN（默认 %d）"
        % DEFAULT_MIN_PER_NODE,
    )
    ap.add_argument("--json", action="store_true", help="机器可读输出（stdout 只有 JSON）")
    ap.add_argument("--quiet", action="store_true", help="只打结论与 ERROR/WARN")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    def _resolve(p: str) -> str:
        return p if os.path.isabs(p) else os.path.join(args.dir, p)

    try:
        worklist_ids = load_worklist_ids(_resolve(args.worklist))
        taxonomy = load_taxonomy(_resolve(args.taxonomy))
        routing_records = load_routing(_resolve(args.routing))
    except InputError as exc:
        sys.stderr.write("pd_route_check: %s\n" % exc)
        return EXIT_USAGE

    report = run_checks(
        worklist_ids,
        taxonomy,
        routing_records,
        coverage_target=args.coverage_target,
        max_targets=args.max_targets,
        min_nodes=args.min_nodes,
        max_nodes=args.max_nodes,
        max_depth=args.max_depth,
        min_per_node=args.min_per_node,
    )
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        text = format_report(report, show_nodes=not args.quiet)
        sys.stdout.write(text + "\n")
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
