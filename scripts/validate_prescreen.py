#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
初筛效果验证脚本

输入: 初筛结果(results.jsonl) + 人工评测结果(human_eval.csv)
输出: 混淆矩阵、召回率/精确率、分维度偏差、阈值扫描建议

用法:
    python3 validate_prescreen.py \\
        --prescreen ./results/results.jsonl \\
        --human ./human_eval.csv \\
        --out ./results/validation_report.md

仅用标准库，无需额外安装。
"""

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

# 人工判定为"不合格"的阈值: 任一维度低于此分即视为不合格
UNQUALIFIED_THRESHOLD = 3.0
# 一致性判定: 初筛与人工分差在此范围内视为一致
CONSISTENCY_TOLERANCE = 1.0


# ---------------------------------------------------------------- 数据读取

def load_prescreen(path: Path) -> dict:
    """读取初筛结果 jsonl，返回 {sample_id: record}"""
    data = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("status") != "succeeded":
                continue
            scores = {}
            try:
                scores = json.loads(rec.get("scores", "{}"))
            except Exception:
                pass
            data[rec["sample_id"]] = {
                "route": rec.get("route", ""),
                "confidence": rec.get("confidence", ""),
                "scores": {k: float(v) for k, v in scores.items()},
                "redline_hit": rec.get("redline_hit", "无"),
                "review_dims": rec.get("review_dims", ""),
            }
    return data


def load_human(path: Path) -> dict:
    """
    读取人工评测结果 CSV
    必需列: sample_id, verdict(qualified/unqualified)
    可选列: 各维度分数(指令遵循/主体一致性/镜头语言/结构与美学/画质稳定性/文字渲染)
    """
    data = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        dim_cols = [
            c for c in (reader.fieldnames or [])
            if c not in ("sample_id", "verdict", "note", "备注")
        ]
        for row in reader:
            sid = (row.get("sample_id") or "").strip()
            if not sid:
                continue
            scores = {}
            for col in dim_cols:
                raw = (row.get(col) or "").strip()
                if not raw:
                    continue
                try:
                    val = float(raw)
                except ValueError:
                    continue
                if val > 0:
                    scores[col] = val

            verdict = (row.get("verdict") or "").strip().lower()
            if verdict not in ("qualified", "unqualified"):
                if scores:
                    verdict = ("unqualified"
                               if min(scores.values()) < UNQUALIFIED_THRESHOLD
                               else "qualified")
                else:
                    continue
            data[sid] = {"verdict": verdict, "scores": scores}
    return data


# ---------------------------------------------------------------- 指标计算

def route_to_prediction(route: str) -> str:
    """
    把分流结果映射成二分类预测
    正类 = 不合格。high_conf_bad 判为不合格，其余判为合格
    edge_review 单列，因为它没有下结论，只是转人工
    """
    if route == "high_conf_bad":
        return "unqualified"
    if route == "high_conf_good":
        return "qualified"
    return "edge"


def compute_binary_metrics(pairs: list) -> dict:
    """
    pairs: [(预测, 真值)]，仅统计初筛给出明确结论的样本
    正类 = unqualified
    """
    tp = fp = fn = tn = 0
    for pred, truth in pairs:
        if pred == "unqualified" and truth == "unqualified":
            tp += 1
        elif pred == "unqualified" and truth == "qualified":
            fp += 1
        elif pred == "qualified" and truth == "unqualified":
            fn += 1
        else:
            tn += 1

    recall = tp / (tp + fn) if (tp + fn) else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    false_alarm = fp / (fp + tn) if (fp + tn) else 0.0
    accuracy = (tp + tn) / len(pairs) if pairs else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) else 0.0)

    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "recall": recall, "precision": precision,
        "false_alarm": false_alarm, "accuracy": accuracy, "f1": f1,
    }


def compute_dim_stats(matched: list) -> dict:
    """逐维度统计: 平均绝对偏差、代数和偏差、一致率、皮尔逊相关"""
    buckets = defaultdict(list)
    for pre, hum in matched:
        for dim, hval in hum["scores"].items():
            pval = pre["scores"].get(dim)
            if pval is None or pval <= 0:
                continue
            buckets[dim].append((pval, hval))

    stats = {}
    for dim, pairs in buckets.items():
        n = len(pairs)
        if n < 2:
            continue
        diffs = [p - h for p, h in pairs]
        mae = sum(abs(d) for d in diffs) / n
        bias = sum(diffs) / n
        consistent = sum(1 for d in diffs if abs(d) <= CONSISTENCY_TOLERANCE)

        mean_p = sum(p for p, _ in pairs) / n
        mean_h = sum(h for _, h in pairs) / n
        cov = sum((p - mean_p) * (h - mean_h) for p, h in pairs)
        var_p = sum((p - mean_p) ** 2 for p, _ in pairs)
        var_h = sum((h - mean_h) ** 2 for _, h in pairs)
        corr = (cov / math.sqrt(var_p * var_h)) if var_p and var_h else 0.0

        stats[dim] = {
            "n": n, "mae": mae, "bias": bias,
            "consistency": consistent / n, "corr": corr,
        }
    return stats


def sweep_thresholds(matched: list) -> list:
    """
    阈值扫描: 模拟不同边界区宽度下的人工量与漏检情况
    返回 [(low, high, 转人工占比, 自动判定中的漏检数, 误杀数)]
    """
    rows = []
    candidates = [
        (2.5, 3.5), (2.8, 3.2), (2.0, 4.0),
        (2.5, 4.0), (3.0, 4.0), (2.0, 3.5),
    ]
    for low, high in candidates:
        edge = auto_ok = auto_bad = miss = false_kill = 0
        for pre, hum in matched:
            scores = [v for v in pre["scores"].values() if v > 0]
            if not scores:
                edge += 1
                continue
            if pre["redline_hit"] not in ("无", ""):
                auto_bad += 1
                if hum["verdict"] == "qualified":
                    false_kill += 1
                continue
            lowest = min(scores)
            if low <= lowest <= high:
                edge += 1
            elif lowest > high:
                auto_ok += 1
                if hum["verdict"] == "unqualified":
                    miss += 1
            else:
                auto_bad += 1
                if hum["verdict"] == "qualified":
                    false_kill += 1
        total = len(matched)
        rows.append({
            "low": low, "high": high,
            "edge_pct": edge / total if total else 0,
            "auto_pct": (auto_ok + auto_bad) / total if total else 0,
            "miss": miss, "false_kill": false_kill,
        })
    return rows


# ---------------------------------------------------------------- 报告输出

def build_report(matched: list, unmatched_pre: list, unmatched_hum: list) -> str:
    lines = []
    total = len(matched)

    lines.append("# 初筛效果验证报告\n")
    lines.append("## 一、样本匹配情况\n")
    lines.append("| 项 | 数量 |")
    lines.append("|---|---|")
    lines.append("| 成功匹配样本 | %d |" % total)
    lines.append("| 仅初筛有、人工无 | %d |" % len(unmatched_pre))
    lines.append("| 仅人工有、初筛无 | %d |" % len(unmatched_hum))
    lines.append("")

    if total < 30:
        lines.append("> 匹配样本不足 30 条，以下指标仅供参考，"
                     "不具备统计意义，建议扩大验证集后重跑。\n")

    # 分流分布
    route_count = defaultdict(int)
    for pre, _ in matched:
        route_count[pre["route"]] += 1
    lines.append("## 二、分流分布\n")
    lines.append("| 分流结果 | 数量 | 占比 |")
    lines.append("|---|---|---|")
    name_map = {
        "high_conf_good": "高置信-明显好",
        "high_conf_bad": "高置信-明显差",
        "edge_review": "低置信-转人工",
    }
    for route, cnt in sorted(route_count.items(), key=lambda x: -x[1]):
        lines.append("| %s | %d | %.1f%% |" % (
            name_map.get(route, route), cnt, cnt / total * 100))
    auto_cnt = total - route_count.get("edge_review", 0)
    lines.append("")
    lines.append("自动处理占比 **%.1f%%**，需人工复核 **%.1f%%**\n" % (
        auto_cnt / total * 100, (total - auto_cnt) / total * 100))

    # 二分类指标
    pairs = []
    for pre, hum in matched:
        pred = route_to_prediction(pre["route"])
        if pred == "edge":
            continue
        pairs.append((pred, hum["verdict"]))

    lines.append("## 三、自动判定部分的准确性\n")
    lines.append("仅统计初筛给出明确结论的 %d 条（转人工的样本不计入，"
                 "因为它没有下结论）。正类 = 不合格。\n" % len(pairs))

    if pairs:
        m = compute_binary_metrics(pairs)
        lines.append("### 混淆矩阵\n")
        lines.append("| | 人工判不合格 | 人工判合格 |")
        lines.append("|---|---|---|")
        lines.append("| 初筛判不合格 | TP %d | FP %d（误杀） |" % (m["tp"], m["fp"]))
        lines.append("| 初筛判合格 | FN %d（漏放） | TN %d |" % (m["fn"], m["tn"]))
        lines.append("")
        lines.append("### 核心指标\n")
        lines.append("| 指标 | 数值 | 含义 |")
        lines.append("|---|---|---|")
        lines.append("| 召回率 | %.1f%% | 所有不合格样本里被拦下的比例 |" % (m["recall"] * 100))
        lines.append("| 精确率 | %.1f%% | 被拦下的样本里真不合格的比例 |" % (m["precision"] * 100))
        lines.append("| 误杀率 | %.1f%% | 合格样本被错误拦下的比例 |" % (m["false_alarm"] * 100))
        lines.append("| 准确率 | %.1f%% | 整体判对比例 |" % (m["accuracy"] * 100))
        lines.append("| F1 | %.3f | 召回与精确的调和平均 |" % m["f1"])
        lines.append("")

        if m["fn"] > 0:
            lines.append("> **漏放 %d 条**：这些不合格样本被初筛判为明显好、"
                         "直接进了抽检队列。抽检覆盖率若为 20%%，"
                         "预计仍有约 %.1f 条会流入最终结果。\n"
                         % (m["fn"], m["fn"] * 0.8))
        if m["false_alarm"] > 0.05:
            lines.append("> **误杀率超过 5%**：合格样本被错误打入 Bad Case，"
                         "会造成标注员返工和信任度损耗，建议放宽下沿阈值。\n")
    else:
        lines.append("初筛全部转人工，无自动判定样本可供统计。\n")

    # 分维度偏差
    dim_stats = compute_dim_stats(matched)
    if dim_stats:
        lines.append("## 四、分维度打分偏差\n")
        lines.append("| 维度 | 样本数 | 平均绝对偏差 | 系统性偏差 | 一致率(±1) | 相关系数 |")
        lines.append("|---|---|---|---|---|---|")
        for dim, s in sorted(dim_stats.items(), key=lambda x: -x[1]["mae"]):
            lines.append("| %s | %d | %.2f | %+.2f | %.1f%% | %.2f |" % (
                dim, s["n"], s["mae"], s["bias"],
                s["consistency"] * 100, s["corr"]))
        lines.append("")
        lines.append("**怎么读这张表**\n")
        lines.append("- **平均绝对偏差**反映整体准不准，超过 0.6 分说明该维度"
                     "初筛不可用，应强制转人工。")
        lines.append("- **系统性偏差**带符号，正值表示初筛比人工打得松。"
                     "绝对偏差小但系统性偏差大，说明是稳定跑偏而非随机噪声，"
                     "比前者更危险，需要改 Prompt 里的锚点描述。")
        lines.append("- **相关系数**低于 0.5 说明初筛和人工判断方向都不一致，"
                     "该维度的评分卡描述可能本身就不适合让模型执行。\n")

        worst = max(dim_stats.items(), key=lambda x: x[1]["mae"])
        if worst[1]["mae"] > 0.6:
            lines.append("> 建议：**%s** 维度偏差达 %.2f 分，"
                         "建议在置信度分流里把该维度设为强制转人工项。\n"
                         % (worst[0], worst[1]["mae"]))

        biased = [(d, s) for d, s in dim_stats.items() if abs(s["bias"]) > 0.3]
        if biased:
            for dim, s in biased:
                direction = "偏松" if s["bias"] > 0 else "偏严"
                lines.append("> 建议：**%s** 维度存在系统性%s（%+.2f 分），"
                             "属于可修正问题，优先调整 Judge Prompt 中该维度的"
                             "分档锚点描述。\n" % (dim, direction, s["bias"]))

    # 阈值扫描
    lines.append("## 五、阈值扫描建议\n")
    lines.append("模拟不同边界区宽度对人工量与风险的影响：\n")
    lines.append("| 边界区 | 转人工占比 | 自动处理占比 | 漏放数 | 误杀数 |")
    lines.append("|---|---|---|---|---|")
    for row in sweep_thresholds(matched):
        lines.append("| %.1f – %.1f | %.1f%% | %.1f%% | %d | %d |" % (
            row["low"], row["high"], row["edge_pct"] * 100,
            row["auto_pct"] * 100, row["miss"], row["false_kill"]))
    lines.append("")
    lines.append("对应 `run_prescreen` 工作流 conf 节点里的 `EDGE_LOW` / "
                 "`EDGE_HIGH` 两个常量。选型原则：初筛的定位是减负不是替代判断，"
                 "宁可多转人工也要压住漏放，等一致性数据积累够了再逐步收窄。\n")

    # 结论
    lines.append("## 六、可用性结论\n")
    if not pairs:
        lines.append("初筛未产出自动判定结果，无法评估可用性。\n")
    else:
        m = compute_binary_metrics(pairs)
        checks = []
        checks.append(("召回率 ≥ 85%", m["recall"] >= 0.85))
        checks.append(("误杀率 ≤ 5%", m["false_alarm"] <= 0.05))
        checks.append(("匹配样本 ≥ 100 条", total >= 100))
        if dim_stats:
            checks.append(("各维度平均绝对偏差 ≤ 0.6",
                           all(s["mae"] <= 0.6 for s in dim_stats.values())))
        lines.append("| 准入条件 | 是否达标 |")
        lines.append("|---|---|")
        for name, ok in checks:
            lines.append("| %s | %s |" % (name, "达标" if ok else "**未达标**"))
        lines.append("")
        if all(ok for _, ok in checks):
            lines.append("**结论：可进入生产环境试运行**，"
                         "建议前两周保持高置信档 30% 抽检，观察异议率后再降。\n")
        else:
            lines.append("**结论：暂不建议全量上线**，"
                         "先按上方建议调整阈值与 Prompt 锚点，重跑验证。\n")

    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程

def main():
    parser = argparse.ArgumentParser(description="初筛效果验证")
    parser.add_argument("--prescreen", required=True, help="初筛结果 results.jsonl")
    parser.add_argument("--human", required=True, help="人工评测结果 CSV")
    parser.add_argument("--out", default="./validation_report.md", help="报告输出路径")
    args = parser.parse_args()

    pre_path = Path(args.prescreen)
    hum_path = Path(args.human)
    if not pre_path.exists():
        sys.exit("找不到初筛结果文件: %s" % pre_path)
    if not hum_path.exists():
        sys.exit("找不到人工评测文件: %s" % hum_path)

    prescreen = load_prescreen(pre_path)
    human = load_human(hum_path)

    common = set(prescreen) & set(human)
    matched = [(prescreen[s], human[s]) for s in sorted(common)]
    unmatched_pre = sorted(set(prescreen) - common)
    unmatched_hum = sorted(set(human) - common)

    if not matched:
        sys.exit("没有匹配上的样本，请检查两份文件的 sample_id 是否一致")

    report = build_report(matched, unmatched_pre, unmatched_hum)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    print("匹配 %d 条，报告已生成: %s\n" % (len(matched), out_path))
    print(report)


if __name__ == "__main__":
    main()
