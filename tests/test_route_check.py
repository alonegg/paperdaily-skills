"""`skills/paperdaily-deep-research/scripts/pd_route_check.py` 的回归。

为什么这层要有测试：阶段 1b/1c **都是 LLM 在写 id**，而这个脚本是唯一一道机械
闸门。它自己判错的方式只有一种——**漏判**：报告照常打印、退出码照常 0、覆盖率
照常是个好看的数字，而路由表里其实有一半 paper_id 是编的。没有任何下游会发现
（memory: silent-stamp-antipattern 的同一形状），所以每一条判据都要在这里钉死。

fixture 有两份，都是**手造**的（`skills/paperdaily-deep-research/references/
examples/`，同时也是 skill 的文档样例——测试与文档读同一份文件，文档改了形状
测试就红）：

  worklist.jsonl        12 篇，id 是 EX0001..EX0012 占位符（不是真实 paper_id）
  taxonomy.json         7 节点、深度 2、4 analytical + 1 navigational + 1 reflective
  routing.jsonl         11/12 篇被路由，coverage 0.92，**应当通过**
  routing.broken.jsonl  每种错各犯一遍，**应当退 2**
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SKILL = _REPO / "skills" / "paperdaily-deep-research"
if not _SKILL.is_dir():
    _SKILL = _REPO / "paperdaily-deep-research"
_SCRIPT = _SKILL / "scripts" / "pd_route_check.py"
_EXAMPLES = _SKILL / "references" / "examples"

_spec = importlib.util.spec_from_file_location("pd_route_check", _SCRIPT)
assert _spec and _spec.loader, f"cannot locate {_SCRIPT}"
rc = importlib.util.module_from_spec(_spec)
sys.modules["pd_route_check"] = rc
_spec.loader.exec_module(rc)


# ── fixture 载入 ─────────────────────────────────────────────────────────
@pytest.fixture()
def worklist_ids() -> list[str]:
    return rc.load_worklist_ids(str(_EXAMPLES / "worklist.jsonl"))


@pytest.fixture()
def taxonomy() -> dict:
    return rc.load_taxonomy(str(_EXAMPLES / "taxonomy.json"))


@pytest.fixture()
def routing() -> list:
    return rc.load_routing(str(_EXAMPLES / "routing.jsonl"))


def _routing_from(rows: list[dict]) -> list:
    """把内存里的行伪装成 load_routing 的返回（(行号, 记录) 元组）。"""
    return [(i, r) for i, r in enumerate(rows, 1)]


def _rows(routing: list) -> list[dict]:
    return [copy.deepcopy(rec) for _, rec in routing]


# ── 样例本身 ─────────────────────────────────────────────────────────────
def test_example_fixture_is_the_documented_shape(worklist_ids, taxonomy) -> None:
    """文档说 12 篇 / 7 节点 / 深度 2；数字变了说明样例被改过，文档要跟着改。"""
    assert len(worklist_ids) == 12
    assert worklist_ids[0] == "EX0001"
    nodes = taxonomy["nodes"]
    assert len(nodes) == 7
    assert {n["role"] for n in nodes} == {"analytical", "reflective", "navigational"}
    # n2 是父节点、自己不挂论文——孤儿判据必须按子树算，这一条是那个分支的前提
    assert any(n["parent"] == "n2" for n in nodes)


def test_good_example_passes(worklist_ids, taxonomy, routing) -> None:
    report = rc.run_checks(worklist_ids, taxonomy, routing)
    assert report["errors"] == []
    assert report["exit_code"] == rc.EXIT_OK
    assert report["n_routed"] == 11
    assert report["coverage"] == pytest.approx(11 / 12)
    assert report["unrouted"] == ["EX0012"]


def test_good_example_has_no_warnings(worklist_ids, taxonomy, routing) -> None:
    """样例是「对的东西长什么样」的对照物，带 WARN 就不合格。"""
    report = rc.run_checks(worklist_ids, taxonomy, routing)
    assert report["warnings"] == []


# ── coverage ─────────────────────────────────────────────────────────────
def test_coverage_counts_papers_not_assignments(worklist_ids, taxonomy, routing) -> None:
    """一篇挂两节仍然只算一篇。11 篇共 17 个 assignment，coverage 仍是 11/12。"""
    n_assignments = sum(len(rec["nodes"]) for _, rec in routing)
    assert n_assignments == 17
    report = rc.run_checks(worklist_ids, taxonomy, routing)
    assert report["coverage"] == pytest.approx(11 / 12)


def test_coverage_below_target_exits_1(worklist_ids, taxonomy, routing) -> None:
    rows = _rows(routing)
    # 只留 6 篇有归属 → 6/12 = 0.5 < 0.6
    for r in rows[6:]:
        r["nodes"] = []
    report = rc.run_checks(worklist_ids, taxonomy, _routing_from(rows))
    assert report["coverage"] == pytest.approx(0.5)
    assert report["exit_code"] == rc.EXIT_COVERAGE
    assert report["errors"] == []  # 结构是好的，只是覆盖不够


def test_coverage_target_is_configurable(worklist_ids, taxonomy, routing) -> None:
    assert rc.run_checks(worklist_ids, taxonomy, routing, coverage_target=0.95)[
        "exit_code"
    ] == rc.EXIT_COVERAGE
    assert rc.run_checks(worklist_ids, taxonomy, routing, coverage_target=0.9)[
        "exit_code"
    ] == rc.EXIT_OK


def test_missing_routing_rows_count_as_unrouted(worklist_ids, taxonomy, routing) -> None:
    """routing.jsonl 里干脆没有那一行，和 nodes:[] 一样算未路由，且要 WARN。"""
    rows = [r for r in _rows(routing) if r["paper_id"] not in {"EX0009", "EX0010"}]
    report = rc.run_checks(worklist_ids, taxonomy, _routing_from(rows))
    assert set(report["unrouted"]) == {"EX0009", "EX0010", "EX0012"}
    assert any("没有对应行" in w for w in report["warnings"])


# ── 结构性错误：每一类一条 ────────────────────────────────────────────────
def test_unknown_paper_id_is_error(worklist_ids, taxonomy, routing) -> None:
    rows = _rows(routing) + [{"paper_id": "EX9999", "nodes": ["n1"]}]
    report = rc.run_checks(worklist_ids, taxonomy, _routing_from(rows))
    assert report["exit_code"] == rc.EXIT_STRUCTURAL
    assert any("EX9999" in e and "未知 id" in e for e in report["errors"])


def test_unknown_node_is_error(worklist_ids, taxonomy, routing) -> None:
    """最常见的形态：LLM 写了节点**标题**而不是节点 id。"""
    rows = _rows(routing)
    rows[1]["nodes"] = ["TWFE 的负权重诊断"]
    report = rc.run_checks(worklist_ids, taxonomy, _routing_from(rows))
    assert report["exit_code"] == rc.EXIT_STRUCTURAL
    assert any("未知节点" in e for e in report["errors"])


def test_more_than_max_targets_is_error(worklist_ids, taxonomy, routing) -> None:
    rows = _rows(routing)
    rows[0]["nodes"] = ["n1", "n2a", "n2b", "n3"]
    report = rc.run_checks(worklist_ids, taxonomy, _routing_from(rows))
    assert report["exit_code"] == rc.EXIT_STRUCTURAL
    assert any("上限是 3" in e for e in report["errors"])


def test_max_targets_is_configurable(worklist_ids, taxonomy, routing) -> None:
    rows = _rows(routing)
    rows[0]["nodes"] = ["n1", "n2a", "n2b", "n3"]
    report = rc.run_checks(worklist_ids, taxonomy, _routing_from(rows), max_targets=4)
    assert report["errors"] == []


def test_duplicate_paper_row_is_error(worklist_ids, taxonomy, routing) -> None:
    """同一篇两行是错，不是「合并」——合并会让它占两个阅读名额。"""
    rows = _rows(routing)
    rows.append({"paper_id": "EX0002", "nodes": ["n3"]})
    report = rc.run_checks(worklist_ids, taxonomy, _routing_from(rows))
    assert report["exit_code"] == rc.EXIT_STRUCTURAL
    assert any("重复出现" in e and "EX0002" in e for e in report["errors"])
    # 第一行仍然作数，第二行整条丢弃 —— 不能把 n3 也算上
    assert report["node_counts"]["n1"] == 3
    assert report["node_counts"]["n3"] == 4


def test_duplicate_node_within_row_is_error(worklist_ids, taxonomy, routing) -> None:
    rows = _rows(routing)
    rows[4]["nodes"] = ["n2b", "n2b"]
    report = rc.run_checks(worklist_ids, taxonomy, _routing_from(rows))
    assert report["exit_code"] == rc.EXIT_STRUCTURAL
    assert any("重复挂了同一个节点" in e for e in report["errors"])


def test_malformed_line_is_error_not_crash(worklist_ids, taxonomy, routing) -> None:
    rows = _routing_from(_rows(routing))
    rows.append((99, rc._BadJSON("Expecting property name")))
    report = rc.run_checks(worklist_ids, taxonomy, rows)
    assert report["exit_code"] == rc.EXIT_STRUCTURAL
    assert any("第 99 行不是合法 JSON" in e for e in report["errors"])


def test_structural_errors_outrank_coverage(worklist_ids, taxonomy, routing) -> None:
    """两者同时发生时退 2：路由表不合法的时候那个覆盖率没有意义。"""
    rows = _rows(routing)
    for r in rows[6:]:
        r["nodes"] = []
    rows.append({"paper_id": "EX9999", "nodes": ["n1"]})
    report = rc.run_checks(worklist_ids, taxonomy, _routing_from(rows))
    assert report["coverage"] < 0.6
    assert report["exit_code"] == rc.EXIT_STRUCTURAL


# ── 孤儿节点与主属/次属 ───────────────────────────────────────────────────
def test_orphan_analytical_node_is_error(worklist_ids, taxonomy, routing) -> None:
    rows = [r for r in _rows(routing) if "n1" not in r["nodes"]]
    report = rc.run_checks(worklist_ids, taxonomy, _routing_from(rows))
    assert report["exit_code"] == rc.EXIT_STRUCTURAL
    assert any("n1" in e and "孤儿节点" in e for e in report["errors"])


def test_orphan_reflective_node_is_only_a_warning(worklist_ids, taxonomy, routing) -> None:
    """reflective 的材料来自别节论文的 limitations，空着是允许的。"""
    rows = _rows(routing)
    for r in rows:
        r["nodes"] = [n for n in r["nodes"] if n != "n5"]
    report = rc.run_checks(worklist_ids, taxonomy, _routing_from(rows))
    assert report["errors"] == []
    assert report["exit_code"] == rc.EXIT_OK
    assert any("reflective 节点 n5" in w for w in report["warnings"])


def test_parent_node_is_not_an_orphan_when_children_are_populated(
    worklist_ids, taxonomy, routing
) -> None:
    """n2 自己一篇都没直接挂，但 n2a/n2b 挂满了 —— 不能判它孤儿。"""
    report = rc.run_checks(worklist_ids, taxonomy, routing)
    assert report["node_counts"]["n2"] == 0
    assert report["subtree_counts"]["n2"] == 6
    assert report["has_children"]["n2"] is True
    assert not any("n2（" in e for e in report["errors"])


def test_analytical_node_with_only_secondary_papers_warns(
    worklist_ids, taxonomy, routing
) -> None:
    """3a 按主属节点派工 —— 只拿到次属的 analytical 节点不会有人读。"""
    rows = _rows(routing)
    for r in rows:
        if r["paper_id"] in {"EX0009", "EX0010"}:
            # 把 n3 从首位挪到次位
            r["nodes"] = ["n4", "n3"]
    report = rc.run_checks(worklist_ids, taxonomy, _routing_from(rows))
    assert report["errors"] == []
    assert report["subtree_primary_counts"]["n3"] == 0
    assert report["subtree_counts"]["n3"] > 0
    assert any("n3" in w and "次属" in w for w in report["warnings"])


def test_thin_analytical_node_warns(worklist_ids, taxonomy, routing) -> None:
    rows = [r for r in _rows(routing) if r["paper_id"] not in {"EX0003", "EX0004"}]
    report = rc.run_checks(worklist_ids, taxonomy, _routing_from(rows))
    assert report["subtree_counts"]["n1"] == 1
    assert any("n1" in w and "收敛下限" in w for w in report["warnings"])


def test_primary_counts_differ_from_node_counts(worklist_ids, taxonomy, routing) -> None:
    """两套计数不是同一个数，混用会把 3a 的 agent 数量估多。"""
    report = rc.run_checks(worklist_ids, taxonomy, routing)
    assert report["node_counts"]["n3"] == 4  # 含两篇次属
    assert report["primary_counts"]["n3"] == 2
    assert sum(report["primary_counts"].values()) == report["n_routed"]


# ── taxonomy 校验 ────────────────────────────────────────────────────────
def test_duplicate_node_id_is_error(taxonomy) -> None:
    tax = copy.deepcopy(taxonomy)
    tax["nodes"].append(copy.deepcopy(tax["nodes"][0]))
    errors, _, _ = rc.check_taxonomy(tax)
    assert any("节点 id 重复" in e for e in errors)


def test_bad_role_is_error(taxonomy) -> None:
    tax = copy.deepcopy(taxonomy)
    tax["nodes"][0]["role"] = "background"
    errors, _, _ = rc.check_taxonomy(tax)
    assert any("role=" in e for e in errors)


def test_missing_parent_is_error(taxonomy) -> None:
    tax = copy.deepcopy(taxonomy)
    tax["nodes"][2]["parent"] = "nope"
    errors, _, _ = rc.check_taxonomy(tax)
    assert any("parent=nope 不存在" in e for e in errors)


def test_parent_cycle_is_error_not_hang(taxonomy) -> None:
    tax = copy.deepcopy(taxonomy)
    by_id = {n["id"]: n for n in tax["nodes"]}
    by_id["n1"]["parent"] = "n3"
    by_id["n3"]["parent"] = "n1"
    errors, _, _ = rc.check_taxonomy(tax)
    assert any("成环" in e for e in errors)


def test_too_deep_is_error(taxonomy) -> None:
    tax = copy.deepcopy(taxonomy)
    tax["nodes"].append(
        {
            "id": "n2a1",
            "title": "三层",
            "description": "d",
            "role": "analytical",
            "key_questions": ["q"],
            "parent": "n2a",
        }
    )
    errors, _, _ = rc.check_taxonomy(tax)
    assert any("深度是 3" in e for e in errors)


def test_node_count_bounds(taxonomy) -> None:
    tax = copy.deepcopy(taxonomy)
    tax["nodes"] = tax["nodes"][:2]
    errors, _, _ = rc.check_taxonomy(tax)
    assert any("低于下限" in e for e in errors)

    tax = copy.deepcopy(taxonomy)
    for i in range(3):
        tax["nodes"].append(
            {
                "id": "x%d" % i,
                "title": "t",
                "description": "d",
                "role": "analytical",
                "key_questions": ["q"],
                "parent": None,
            }
        )
    errors, _, _ = rc.check_taxonomy(tax)
    assert any("超过上限" in e for e in errors)


def test_no_analytical_node_is_error(taxonomy) -> None:
    tax = copy.deepcopy(taxonomy)
    for n in tax["nodes"]:
        n["role"] = "navigational"
    errors, _, _ = rc.check_taxonomy(tax)
    assert any("没有任何 analytical 节点" in e for e in errors)


def test_bare_node_array_is_accepted(tmp_path: Path, taxonomy) -> None:
    """LLM 常直接吐一个数组 —— 接受它，只是 topic 为空。"""
    p = tmp_path / "taxonomy.json"
    p.write_text(json.dumps(taxonomy["nodes"], ensure_ascii=False), encoding="utf-8")
    loaded = rc.load_taxonomy(str(p))
    assert loaded["topic"] == ""
    assert len(loaded["nodes"]) == 7


# ── 载入层的硬失败（退出码 3，不是「校验没通过」）────────────────────────
def test_duplicate_worklist_id_is_input_error(tmp_path: Path) -> None:
    p = tmp_path / "worklist.jsonl"
    p.write_text('{"id":"A"}\n{"id":"A"}\n', encoding="utf-8")
    with pytest.raises(rc.InputError, match="重复"):
        rc.load_worklist_ids(str(p))


def test_missing_file_is_input_error(tmp_path: Path) -> None:
    with pytest.raises(rc.InputError, match="找不到"):
        rc.load_worklist_ids(str(tmp_path / "nope.jsonl"))


def test_taxonomy_without_nodes_is_input_error(tmp_path: Path) -> None:
    p = tmp_path / "taxonomy.json"
    p.write_text('{"topic":"x"}', encoding="utf-8")
    with pytest.raises(rc.InputError, match="缺少 .nodes"):
        rc.load_taxonomy(str(p))


# ── CLI 端到端（退出码是判据，所以必须真跑一次进程）────────────────────────
def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def test_cli_help_works() -> None:
    r = _run("--help")
    assert r.returncode == 0
    assert "--coverage-target" in r.stdout


def test_cli_good_example_exits_0() -> None:
    r = _run("--dir", str(_EXAMPLES))
    assert r.returncode == rc.EXIT_OK, r.stdout + r.stderr
    assert "coverage=0.92" in r.stdout
    assert "通过" in r.stdout


def test_cli_broken_example_exits_2() -> None:
    r = _run("--dir", str(_EXAMPLES), "--routing", "routing.broken.jsonl")
    assert r.returncode == rc.EXIT_STRUCTURAL
    # 文档承诺这个反例一次命中六类错 + 孤儿节点
    for needle in (
        "上限是 3",
        "重复出现",
        "未知 id",
        "未知节点",
        "重复挂了同一个节点",
        "不是合法 JSON",
        "孤儿节点",
    ):
        assert needle in r.stdout, needle


def test_cli_coverage_failure_exits_1() -> None:
    r = _run("--dir", str(_EXAMPLES), "--coverage-target", "0.95")
    assert r.returncode == rc.EXIT_COVERAGE
    assert "重跑阶段 1c 的路由" in r.stdout


def test_cli_missing_file_exits_3(tmp_path: Path) -> None:
    r = _run("--dir", str(tmp_path))
    assert r.returncode == rc.EXIT_USAGE
    assert "找不到" in r.stderr


def test_cli_json_output_is_machine_readable() -> None:
    r = _run("--dir", str(_EXAMPLES), "--json")
    assert r.returncode == rc.EXIT_OK
    payload = json.loads(r.stdout)
    assert payload["coverage"] == pytest.approx(11 / 12)
    assert payload["unrouted"] == ["EX0012"]
    assert payload["primary_counts"]["n3"] == 2
    assert payload["exit_code"] == 0


def test_cli_rejects_out_of_range_coverage_target() -> None:
    r = _run("--dir", str(_EXAMPLES), "--coverage-target", "1.5")
    assert r.returncode == 2  # argparse 的用法错误
    assert "coverage-target" in r.stderr
