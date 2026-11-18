"""Reproduce the reported results from recorded runs, without a solver run."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from statistics import median
import subprocess
import sys

from Analyze_Objective_Choice import summarize
from Cross_Generation_Analysis import (
    canonical_schedule_row, population_idle_sd, read_rows,
)

ROOT = Path(__file__).resolve().parents[1]


def selected(rows, configuration):
    groups = defaultdict(list)
    for row in rows:
        if row["planned_configuration_id"] == configuration and row["status"] == "OPTIMAL":
            groups[row["instance_sha256"]].append(row)
    return {key: canonical_schedule_row(group) for key, group in groups.items()}


def extended_quality(output: Path | None = None):
    journal = ROOT / "outputs/journal"
    waiting = read_rows([journal / "all126-first/shards-0-1-2-of-4/normalized/detailed.csv"])
    bg = read_rows([journal / "bg-dinf-quality/normalized/detailed.csv"])
    summary = summarize(waiting, bg)
    assert summary["common_optimal_instance_count"] == 62
    expected = {
        "break_groups": [1, 2, 2, 5, 16],
        "total_idle": [3, 2, 2, 3.5, 5],
        "squared_idle": [3, 1.5, 1.5, 3.5, 4.5],
        "maximum_then_total": [3, 1.5, 1.5, 3.5, 4.5],
        "range_then_total": [4, 1.5, 1.5, 8, 16],
    }
    fields = ("total_break_groups", "idle_range_pstar", "maximum_internal_idle_slots",
              "total_internal_idle_slots", "squared_internal_idle_slots")
    for name, values in expected.items():
        assert [summary["medians"][name][f] for f in fields] == values
    main = read_rows([journal / "main-objectives-merged/normalized/detailed.csv"])
    final = read_rows([journal / "final-tier-compact/normalized/detailed.csv"])
    left = selected(final, "compact_cdf_ir_im_isq")
    right = selected(main, "compact_cdf_ir_im_is")
    common = sorted(left.keys() & right.keys())
    assert len(common) == 83
    comparisons = {}
    for field, counts in (("squared_internal_idle_slots", (15, 68, 0)),
                          ("population_sd_idle_slots", (15, 68, 0)),
                          ("total_internal_idle_slots", (0, 69, 14))):
        measure = population_idle_sd if field == "population_sd_idle_slots" else lambda r: float(r[field])
        differences = [measure(left[key]) - measure(right[key]) for key in common]
        observed = tuple(sum(test(d) for d in differences) for test in
                         (lambda d: d < -1e-9, lambda d: abs(d) <= 1e-9, lambda d: d > 1e-9))
        assert observed == counts, (field, observed)
        comparisons[field] = {"lower_equal_higher": observed, "minimum": min(differences),
                              "maximum": max(differences), "median": median(differences)}
    assert comparisons["squared_internal_idle_slots"]["minimum"] == -143
    assert comparisons["total_internal_idle_slots"]["maximum"] == 12
    assert round(comparisons["population_sd_idle_slots"]["minimum"], 3) == -0.520
    result = {"valid": True, "five_objective_results": summary,
              "final_tier_instances": 83, "final_tier_comparisons": comparisons,
              "selected_repetition": "lowest-numbered OPTIMAL repetition"}
    if output is not None:
        (output / "extended_quality_validation.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def run(command, log):
    print("Running:", " ".join(str(x) for x in command), flush=True)
    with log.open("w") as stream:
        subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs/reproduced_review")
    parser.add_argument("--reference-latex", type=Path,
                        default=ROOT / "outputs/cross_generation_analysis/generated-results-rows.tex",
                        help="reference LaTeX result rows for byte-for-byte comparison")
    parser.add_argument("--skip-formula-rebuild", action="store_true",
                        help="quick check of matched runs and quality; omit the separate formula rebuild")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    reference_latex = args.reference_latex.resolve()
    if not reference_latex.is_file():
        parser.error(f"reference LaTeX file not found: {reference_latex}")
    if output == reference_latex or output in reference_latex.parents:
        parser.error("keep the reference LaTeX file outside the output directory")
    expected_dir = ROOT / "outputs/cross_generation_analysis"
    if output == expected_dir.resolve() or output in expected_dir.resolve().parents:
        raise ValueError("use a separate output directory, preserving the reference summaries")
    output.mkdir(parents=True, exist_ok=True)
    generated = output / "generated-results-rows.tex"
    if not args.skip_formula_rebuild:
        run([sys.executable, "src/Cross_Generation_Analysis.py", "--output-dir", output,
             "--latex-output", generated], output / "formula_rebuild.log")
    run([sys.executable, "src/Cross_Generation_Analysis.py", "--matched-results",
         "outputs/journal/matched-encoding-v2-complete/normalized/detailed.csv",
         "--output-dir", output, "--latex-output", generated], output / "matched_analysis.log")
    report = json.loads((output / "analysis_report.json").read_text())
    reference_report = json.loads((expected_dir / "analysis_report.json").read_text())
    assert report["runtime_environment_sensitivity"] == reference_report["runtime_environment_sensitivity"]
    sensitivity = report["runtime_environment_sensitivity"]["comparisons"]
    assert len(sensitivity) == 9
    stress_runtime = next(row for row in sensitivity
                          if (row["dataset"], row["objective"]) == ("Stress", "IR-IM-IS"))
    assert (stress_runtime["all_optimal_instances"], stress_runtime["metadata_matched_instances"],
            stress_runtime["metadata_matched_run_pairs"]) == (47, 47, 47)
    assert abs(stress_runtime["pooled_median_runtime_ratio"] - 0.5466412819685957) < 1e-12
    assert abs(stress_runtime["metadata_matched_median_runtime_ratio"] - 1.7586548649932403) < 1e-12
    priority = read_rows([output / "objective_priority_summary.csv"])
    assert report["objective_priority_comparison"]["instances"] == 68
    control = [r for r in priority if r["left_objective"] == "IR-IM-IS" and r["right_objective"] == "IR-IS"]
    assert len(control) == 3
    assert all((int(r["lower"]), int(r["equal"]), int(r["higher"])) == (0, 68, 0) for r in control)
    checked = []
    for name in report["source_data_sha256"]:
        assert (output / name).read_bytes() == (expected_dir / name).read_bytes(), name
        checked.append(name)
    assert generated.read_bytes() == reference_latex.read_bytes()
    extended_quality(output)
    result = {"valid": True, "solver_runs_started": 0,
              "full_formula_rebuild": not args.skip_formula_rebuild,
              "identical_csv_files": checked, "generated_latex_identical": True,
              "quality_instances": {"All-126": 84, "Stress": 47, "Stress-high": 21},
              "extended_quality_instances": [62, 83],
              "priority_quality_instances": 68,
              "runtime_environment_sensitivity_identical": True,
              "runtime_environment_comparisons": len(sensitivity),
              "runtime_environment_matched_run_pairs": sum(row["metadata_matched_run_pairs"] for row in sensitivity),
              "matched_raw_sha256": report["source_results_sha256"],
              "analysis_script_sha256": hashlib.sha256((ROOT / "src/Cross_Generation_Analysis.py").read_bytes()).hexdigest()}
    (output / "reproduction_verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
