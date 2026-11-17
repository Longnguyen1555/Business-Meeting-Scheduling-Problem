"""Check generated manuscript values against the complete matched-run evidence."""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from statistics import median


ROOT = Path(__file__).resolve().parents[1]


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def macro_body(tex, name):
    match = re.search(r"\\newcommand\{\\" + re.escape(name) + r"\}\{%\n(.*?)\n\}", tex, re.S)
    if match is None:
        raise ValueError(f"missing result macro: {name}")
    return match.group(1)


def number(cell):
    cell = re.sub(r"\$\^\\dagger\$$", "", cell.strip())
    return float(re.sub(r"\\[A-Za-z]+|[{},%\s]", "", cell))


def numeric_cell(cell):
    """Read a numeric cell, allowing bold only around the complete value."""
    value = cell.strip().rstrip("\\%").strip()
    bold = re.fullmatch(r"\\textbf\{(.*)\}", value)
    value = bold[1] if bold else value
    value = value.strip("$").replace("{,}", "").replace(",", "")
    assert re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value), cell
    return float(value), bold is not None


def check_numeric_cell(cell, expected, *, bold=False, decimals=None):
    observed, emphasized = numeric_cell(cell)
    target = float(expected)
    if decimals is not None:
        target = round(target, decimals)
    assert observed == target, (cell, target)
    assert emphasized == bold, (cell, "expected bold", bold)


def audit_primary_runtime(tex, summaries):
    index = {(r["dataset"], r["objective"], r["encoding"]): r for r in summaries}
    sizes = {"All-126": 126, "Stress": 60, "Stress-high": 40}
    lines = []
    dataset = None
    for line in macro_body(tex, "MatchedRuntimePrimaryRows").splitlines():
        heading = re.search(r"\\textit\{(All-126|Stress-high|Stress) \(\$n=(\d+)\$\)\}", line)
        if heading:
            dataset = heading[1]
            assert int(heading[2]) == sizes[dataset], line
        elif line.count("&") == 6:
            lines.append((dataset, line.split("&")))
    expected = [(d, o) for d in sizes for o in ("BG-d2", "IR", "IR-IM-IS")]
    assert len(lines) == len(expected)
    for (dataset, cells), (expected_dataset, objective) in zip(lines, expected):
        assert dataset == expected_dataset
        assert cells[0].strip() == rf"\textsc{{{objective}}}", cells[0]
        peers = [index[(dataset, objective, e)] for e in ("Direct", "Compact-Objective")]
        opt_differ = int(peers[0]["OPT"]) != int(peers[1]["OPT"])
        for offset, row in zip((1, 4), peers):
            outcomes = cells[offset].strip().split("/")
            assert len(outcomes) == 3
            for i, field in enumerate(("OPT", "UNSAT", "timeout")):
                bold = i == 0 and opt_differ and int(row[field]) == max(int(p[field]) for p in peers)
                check_numeric_cell(outcomes[i], row[field], bold=bold)
            assert sum(numeric_cell(c)[0] for c in outcomes) == sizes[dataset]
            for cell, field in zip(cells[offset+1:offset+3],
                                   ("mean_PAR2_seconds", "median_peak_RSS_mb")):
                check_numeric_cell(cell, row[field], decimals=1,
                                   bold=float(row[field]) == min(float(p[field]) for p in peers))
    return {"outcome_totals": 18, "opt_highlights": 18, "metric_highlights": 36}


def table_block(source, label):
    blocks = [b for b in re.findall(r"\\begin\{table\}.*?\\end\{table\}", source, re.S)
              if rf"\label{{{label}}}" in b]
    assert len(blocks) == 1, ("missing or duplicate table", label)
    return blocks[0]


def table_cells(block, columns):
    return [[c.strip() for c in re.sub(r"\\\\%?\s*$", "", line.strip()).split("&")]
            for line in block.splitlines()
            if line.count("&") == columns - 1 and re.search(r"\\\\%?\s*$", line)]


def objective_name(cell):
    match = re.fullmatch(r"\\textsc\{([^{}]+)\}", cell)
    assert match, cell
    return match[1]


def audit_appendix_tables(appendix, *, extended, stress, ablations, formulas):
    """Check the four result tables actually included in the current appendix."""
    assert extended["valid"]
    checked = {}
    label = "tab:app-extended-quality"
    block = table_block(appendix, label)
    common = extended["five_objective_results"]["common_optimal_instance_count"]
    final_n = extended["final_tier_instances"]
    assert common == 62 and final_n == 83
    assert f"Five-objective medians ({common} instances)" in block
    assert rf"minus \textsc{{IR-IM-IS}} ({final_n} instances)" in block
    cells = table_cells(block, 6)
    rows = [c for c in cells if c[0].startswith(r"\textsc")]
    names = ((r"BG-d$\infty$", "break_groups"), ("IS", "total_idle"),
             ("ISQ", "squared_idle"), ("IM-IS", "maximum_then_total"),
             ("IR-IS", "range_then_total"))
    fields = ("total_break_groups", "idle_range_pstar", "maximum_internal_idle_slots",
              "total_internal_idle_slots", "squared_internal_idle_slots")
    medians = extended["five_objective_results"]["medians"]
    assert len(rows) == len(names)
    for row, (display, key) in zip(rows, names):
        assert objective_name(row[0]) == display
        for cell, field in zip(row[1:], fields):
            value = medians[key][field]
            check_numeric_cell(cell, value, bold=value == min(m[field] for m in medians.values()))
    differences = [c for c in cells if c[0].startswith("$")]
    measures = ((r"$I_{\mathrm{SQ}}$ (slots$^2$)", "squared_internal_idle_slots", 0),
                (r"$\sigma_I$ (slots)", "population_sd_idle_slots", 3),
                (r"$I_\Sigma$ (slots)", "total_internal_idle_slots", 0))
    assert len(differences) == len(measures)
    for row, (display, key, precision) in zip(differences, measures):
        assert row[0] == display
        data = extended["final_tier_comparisons"][key]
        counts = data["lower_equal_higher"]
        assert sum(counts) == final_n
        for cell, value in zip(row[1:4], counts):
            check_numeric_cell(cell, value)
        for cell, field in zip(row[4:], ("minimum", "maximum")):
            check_numeric_cell(cell, data[field], decimals=precision)
    checked[label] = {"median_rows": 5, "difference_rows": 3,
                      "instances": [common, final_n], "numeric_cells": 40}

    label = "tab:app-stress-quality"
    rows = [c for c in table_cells(table_block(appendix, label), 6) if c[2].startswith("$")]
    assert len(rows) == len(stress) == 6
    metric_names = {r"$\Delta_I$": "idle_range_pstar",
                    r"$I_{\max}$": "maximum_internal_idle_slots",
                    r"$I_\Sigma$": "total_internal_idle_slots"}
    dataset = None
    seen = set()
    index = {(r["dataset"], r["measure"]): r for r in stress}
    for row in rows:
        if row[0]:
            dataset = row[0]
            assert row[1], row
        key = (dataset, metric_names[row[2]])
        assert key not in seen
        seen.add(key)
        data = index[key]
        if row[1]:
            check_numeric_cell(row[1], data["instances"])
        for cell, field in zip(row[3:], ("lower", "equal", "higher")):
            check_numeric_cell(cell, data[field])
        assert sum(numeric_cell(c)[0] for c in row[3:]) == int(data["instances"])
    assert seen == set(index)
    checked[label] = {"rows": 6, "instances": [47, 21], "count_cells": 18}

    label = "tab:app-objective-layer-ablation"
    block = table_block(appendix, label)
    assert "126 instances" in block
    rows = [c for c in table_cells(block, 6) if c[1] in {"DT", "DI", "AV"}]
    wanted = {"IS", "ISQ", "IM-IS", "IR-IS"}
    data = [r for r in ablations if r["objective"] in wanted]
    index = {(r["objective"], r["configuration"]): r for r in data}
    assert len(rows) == len(index) == 8
    objective = None
    seen = set()
    fields = ("median_variables", "median_clauses", "median_literals")
    for row in rows:
        if row[0]:
            objective = objective_name(row[0])
        key = (objective, row[1])
        assert key not in seen
        seen.add(key)
        target = index[key]
        peers = [r for r in data if r["objective"] == objective]
        for cell, field in zip(row[2:5], fields):
            check_numeric_cell(cell, target[field],
                               bold=float(target[field]) == min(float(r[field]) for r in peers))
        reduction = target["paired_median_clause_reduction_percent"]
        if reduction:
            check_numeric_cell(row[5], reduction, decimals=2)
        else:
            assert row[5] == "--", row[5]
    assert seen == set(index)
    checked[label] = {"rows": 8, "instances": 126, "formula_cells": 24, "reduction_cells": 4}

    label = "tab:app-formula-summary"
    block = table_block(appendix, label)
    assert "126 instances" in block
    rows = [c for c in table_cells(block, 5) if c[1].startswith(r"\textsc")]
    data = [r for r in formulas if r["dataset"] == "All-126"
            and r["encoding"] in {"Direct", "Compact-Objective"}]
    index = {(r["objective"], r["encoding"]): r for r in data}
    assert len(rows) == len(index) == 6
    objective = None
    seen = set()
    for row in rows:
        if row[0]:
            objective = objective_name(row[0])
        key = (objective, objective_name(row[1]))
        assert key not in seen
        seen.add(key)
        target = index[key]
        assert int(target["instances"]) == 126
        peers = [r for r in data if r["objective"] == objective]
        for cell, field in zip(row[2:], fields):
            check_numeric_cell(cell, target[field],
                               bold=float(target[field]) == min(float(r[field]) for r in peers))
    assert seen == set(index)
    checked[label] = {"rows": 6, "instances": 126, "formula_cells": 18}
    return checked


def audit(manuscript: Path, analysis: Path, backup: Path | None = None):
    report = json.loads((analysis / "analysis_report.json").read_text())
    assert report["production_campaign_complete"] and report["time_rss_policy"] == "optimal-runs"
    source = Path(report["source_results"])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == report["source_results_sha256"]
    for name, sha in report["source_data_sha256"].items():
        assert hashlib.sha256((analysis / name).read_bytes()).hexdigest() == sha, name
    rows = read_csv(source)
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["planned_configuration_id"], row["instance_sha256"])].append(row)
    outcomes = {(r["configuration"], r["instance_sha256"]): r
                for r in read_csv(analysis / "repetition_outcomes.csv")}
    assert len(rows) == 6102 and len(grouped) == 2034 and len(outcomes) == 2034
    for key, group in grouped.items():
        assert sorted(int(row["repetition"]) for row in group) == [1, 2, 3]
        optimal = [row for row in group if row["status"] == "OPTIMAL"]
        selected = optimal or group
        value = outcomes[key]
        for field, source_field in (("median_runtime_seconds", "runtime_seconds"),
                                    ("median_peak_rss_mb", "peak_memory_mb")):
            assert float(value[field]) == median(float(row[source_field]) for row in selected), key
        penalty = median(14400 if row["status"] == "TIMEOUT" else float(row["runtime_seconds"])
                         for row in group)
        assert float(value["median_par2_seconds"]) == penalty, key
        assert int(value["time_rss_repetitions"]) == len(selected)
    tex = (manuscript / "generated-results-rows.tex").read_text()
    formulas = read_csv(analysis / "benchmark_formula_summary.csv")
    formula_index = {(r["dataset"], r["objective"], r["encoding"]): r for r in formulas}
    formula_lines = [line for line in macro_body(tex, "MatchedFormulaRows").splitlines()
                     if line.count("&") == 3]
    assert len(formula_lines) == 9
    for line, (dataset, encoding) in zip(formula_lines,
            [(d, e) for d in ("All-126", "Stress", "Stress-high")
             for e in ("Direct", "Compact-Base", "Compact-Objective")]):
        for cell, objective in zip(line.split("&")[1:], ("BG-d2", "IR", "IR-IM-IS")):
            target = formula_index[(dataset, objective, encoding)]
            assert number(cell.rstrip("\\%")) == float(target["median_clauses"])
            minimum = min(float(formula_index[(dataset, objective, e)]["median_clauses"])
                          for e in ("Direct", "Compact-Base", "Compact-Objective"))
            assert (r"\textbf" in cell) == (float(target["median_clauses"]) == minimum)
    detail_lines = [line for line in macro_body(tex, "MatchedFormulaDetailRows").splitlines()
                    if line.count("&") == 5]
    assert len(detail_lines) == len(formulas) == 27
    for line, row in zip(detail_lines, formulas):
        assert [number(cell.rstrip("\\%")) for cell in line.split("&")[3:]] == [
            float(row[field]) for field in ("median_variables", "median_clauses", "median_literals")]
    summaries = read_csv(analysis / "benchmark_runtime_summary.csv")
    checked_bold = 0
    for macro, datasets in (("MatchedRuntimeAllRows", {"All-126"}),
                            ("MatchedRuntimeStressRows", {"Stress", "Stress-high"})):
        lines = [line for line in macro_body(tex, macro).splitlines() if line.count("&") == 7]
        expected = [row for row in summaries if row["dataset"] in datasets]
        assert len(lines) == len(expected)
        for line, row in zip(lines, expected):
            peers = [r for r in summaries if r["dataset"] == row["dataset"] and r["objective"] == row["objective"]]
            cells = line.split("&")[2:]
            fields = ["OPT", "UNSAT", "timeout", "median_runtime_seconds", "mean_PAR2_seconds", "median_peak_RSS_mb"]
            for cell, field, decimals in zip(cells, fields, (0, 0, 0, 3, 3, 2)):
                target = float(f"{float(row[field]):.{decimals}f}")
                assert number(cell.rstrip("\\%")) == target, (row, field, cell)
                if field != "UNSAT":
                    best = (max if field == "OPT" else min)(float(r[field]) for r in peers)
                    assert (r"\textbf" in cell) == (float(row[field]) == best), (row, field)
                    checked_bold += 1
    qualities = read_csv(analysis / "quality_summary.csv")
    lines = [line for line in macro_body(tex, "MainQualityMedianRows").splitlines() if line.count("&") == 5]
    assert len(lines) == len(qualities) == 3
    quality_highlights = 0
    for line, row in zip(lines, qualities):
        fields = ["median_total_break_groups", "median_idle_range_pstar", "median_maximum_internal_idle_slots",
                  "median_total_internal_idle_slots", "median_population_sd_idle_slots"]
        for cell, field in zip(line.split("&")[1:], fields):
            precision = 3 if field == "median_population_sd_idle_slots" else 1
            assert number(cell.rstrip("\\%")) == round(float(row[field]), precision), (row, field)
            lowest = min(float(peer[field]) for peer in qualities)
            assert (r"\textbf" in cell) == (float(row[field]) == lowest), (row, field)
            quality_highlights += 1
    primary_runtime = audit_primary_runtime(tex, summaries)
    assert number(macro_body(tex, "MainQualityN")) == 84
    assert number(macro_body(tex, "StressHighQualityN")) == 21
    assert number(macro_body(tex, "StressHighQualityTotalLower")) == 20
    # The manuscript consumes native figure data, not the historical Matplotlib exports.
    spec = importlib.util.spec_from_file_location(
        "jair_tikz_data", manuscript / "figures/prepare_tikz_data.py")
    tikz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tikz)
    tikz.ANALYSIS = analysis
    native_figures = tikz.prepare(check=True)
    main = (manuscript / "JAIR_Example_Template.tex").read_text()
    appendix = (manuscript / "appendix-supporting-material.tex").read_text()
    from Reproduce_JAIR_Review import extended_quality
    appendix_tables = audit_appendix_tables(
        appendix, extended=extended_quality(), stress=read_csv(analysis / "stress_quality_pairs.csv"),
        ablations=read_csv(analysis / "ablation_summary.csv"), formulas=formulas)
    both = main + appendix
    structural = read_csv(analysis / "benchmark_primary_ablation_summary.csv")
    block = main.split(r"\label{tab:main-ablation-primary}", 1)[1].split(r"\end{tabular*}", 1)[0]
    lines = [line for line in block.splitlines() if r"\tablecheck" in line and "&" in line]
    extensions = read_csv(analysis / "attainable_extension_formula_rows.csv")
    expected = []
    for row in structural:
        if row["dataset"] != "All-126":
            continue
        expected.append(row)
        if row["configuration"] == "B + DS + RC + AMO":
            optional = [r for r in extensions if r["objective"] == row["objective"]]
            config = "compact_objective_ir" if row["objective"] == "IR" else "compact_objective_ir_im_is"
            extra = {field: median(float(r[source_field]) for r in optional)
                     for field, source_field in (("median_variables", "n_vars"),
                                                 ("median_clauses", "n_total_clauses"),
                                                 ("median_literals", "n_total_literals"))}
            extra["paired_median_clause_reduction_percent"] = median(
                100 * (1 - float(r["n_total_clauses"]) /
                       float(grouped[(config, r["instance_sha256"])][0]["n_total_clauses"]))
                for r in optional)
            expected.append(extra)
    assert len(lines) == len(expected) == 17
    for line, row in zip(lines, expected):
        cells = line.split("&")[7:]
        assert [number(cell) for cell in cells[:3]] == [float(row[f]) for f in
                ("median_variables", "median_clauses", "median_literals")]
        if row["paired_median_clause_reduction_percent"]:
            assert number(cells[3].rstrip("\\%")) == float(f"{float(row['paired_median_clause_reduction_percent']):.2f}")
    labels = re.findall(r"\\label\{([^}]+)\}", both)
    assert len(labels) == len(set(labels)), "duplicate labels"
    for label in labels:
        if label.startswith(("tab:", "fig:")):
            assert re.search(r"\\(?:ref|eqref)\{" + re.escape(label) + r"\}", both), label
    for forbidden in (r"\bcommon optima\b", r"journal model", r"RIVF encoding", r"cross-generation comparison",
                      r"one validated run from each cell", r"18 of 19"):
        assert not re.search(forbidden, both, re.I), forbidden
    if backup is not None:
        before = backup / "JAIR_Example_Template"
        assert (before / "reproducibility-checklist.tex").read_bytes() == (manuscript / "reproducibility-checklist.tex").read_bytes()
        # Compare the objective catalogue by value; ignore float-placement options.
        spec = importlib.util.spec_from_file_location(
            "jair_layout_audit", manuscript / "figures/audit_layout.py")
        layout_audit = importlib.util.module_from_spec(spec)
        original_path = list(sys.path)
        try:
            sys.path.insert(0, str(manuscript / "figures"))
            spec.loader.exec_module(layout_audit)
        finally:
            sys.path[:] = original_path
        for name in ("JAIR_Example_Template.tex", "appendix-supporting-material.tex"):
            old, new = (before / name).read_text(), (manuscript / name).read_text()
            layout_audit.check_formal_content(old, new)
    assert json.loads((analysis / "conference_consistency_report.json").read_text())["valid"]
    log = (manuscript / "JAIR_Example_Template.log").read_text()
    assert not re.search(r"Overfull|undefined|multiply defined", log, re.I)
    return {"valid": True, "matched_rows": len(rows), "instance_configuration_groups": len(grouped),
            "runtime_table_values_checked": len(summaries) * 6,
            "formula_table_values_checked": len(formulas) * 4,
            "primary_ablation_rows_checked": len(expected),
            "runtime_highlights_checked": checked_bold, "raw_timeouts_retained": sum(r["status"] == "TIMEOUT" for r in rows),
            "primary_runtime_highlights_checked": primary_runtime["metric_highlights"],
            "primary_opt_highlights_checked": primary_runtime["opt_highlights"],
            "primary_outcome_totals_checked": primary_runtime["outcome_totals"],
            "quality_highlights_checked": quality_highlights,
            "quality_instances": {"All-126": 84, "Stress": 47, "Stress-high": 21},
            "conference_consistency": "PASS", "checklist_and_formal_blocks_unchanged": backup is not None,
            "native_tikz_figures": native_figures,
            "appendix_tables_checked": appendix_tables,
            "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "manuscript_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in (manuscript / "JAIR_Example_Template.tex",
                                            manuscript / "appendix-supporting-material.tex",
                                            manuscript / "generated-results-rows.tex")},
            "source_results_sha256": report["source_results_sha256"], "errors": []}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manuscript", type=Path, default=ROOT.parent / "JAIR_Example_Template")
    parser.add_argument("--analysis", type=Path, default=ROOT / "outputs/cross_generation_analysis")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.manuscript, args.analysis, args.backup)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
