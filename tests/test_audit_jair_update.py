from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from Audit_JAIR_Update import audit_appendix_tables, audit_primary_runtime, number, read_csv
from Reproduce_JAIR_Review import extended_quality


class ManuscriptAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manuscript = PROJECT.parent / "JAIR_Example_Template"
        cls.analysis = PROJECT / "outputs/cross_generation_analysis"
        cls.tex = (cls.manuscript / "generated-results-rows.tex").read_text()
        cls.appendix = (cls.manuscript / "appendix-supporting-material.tex").read_text()
        cls.runtime = read_csv(cls.analysis / "benchmark_runtime_summary.csv")
        cls.evidence = {
            "extended": extended_quality(),
            "stress": read_csv(cls.analysis / "stress_quality_pairs.csv"),
            "ablations": read_csv(cls.analysis / "ablation_summary.csv"),
            "formulas": read_csv(cls.analysis / "benchmark_formula_summary.csv"),
        }

    def replace_once(self, text, before, after):
        self.assertEqual(text.count(before), 1, before)
        return text.replace(before, after)

    def test_current_paper_values_and_highlights_pass(self):
        runtime = audit_primary_runtime(self.tex, self.runtime)
        self.assertEqual(runtime["outcome_totals"], 18)
        self.assertEqual(runtime["opt_highlights"], 18)
        tables = audit_appendix_tables(self.appendix, **self.evidence)
        self.assertEqual(len(tables), 4)
        self.assertEqual(tables["tab:app-extended-quality"]["instances"], [62, 83])
        self.assertEqual(tables["tab:app-stress-quality"]["instances"], [47, 21])

    def test_ablation_footnote_is_distinct_from_the_numeric_value(self):
        self.assertEqual(number(r"3.93$^\dagger$ "), 3.93)
        with self.assertRaises(ValueError):
            number(r"3.93$^\unknown$")

    def test_wrong_outcomes_are_rejected_even_when_the_sum_is_valid(self):
        mutated = self.replace_once(self.tex, r"\textbf{56}/0/4", r"\textbf{55}/0/5")
        with self.assertRaises(AssertionError):
            audit_primary_runtime(mutated, self.runtime)

    def test_missing_or_extra_opt_highlights_are_rejected(self):
        changes = [
            (r"\textbf{56}/0/4", "56/0/4"),
            (r"\textsc{BG-d2} & 87/39/0", r"\textsc{BG-d2} & \textbf{87}/39/0"),
        ]
        for before, after in changes:
            with self.subTest(before=before):
                mutated = self.replace_once(self.tex, before, after)
                with self.assertRaises(AssertionError):
                    audit_primary_runtime(mutated, self.runtime)

    def test_unsat_and_timeout_highlights_are_rejected(self):
        changes = [
            (r"\textsc{BG-d2} & 87/39/0", r"\textsc{BG-d2} & 87/\textbf{39}/0"),
            (r"\textbf{56}/0/4", r"\textbf{56}/0/\textbf{4}"),
        ]
        for before, after in changes:
            with self.subTest(before=before):
                mutated = self.replace_once(self.tex, before, after)
                with self.assertRaises(AssertionError):
                    audit_primary_runtime(mutated, self.runtime)

    def test_wrong_par2_rss_or_metric_highlights_are_rejected(self):
        changes = [
            ("2{,}186.1 & 149.3", "2{,}186.2 & 149.3"),
            ("2{,}186.1 & 149.3", "2{,}186.1 & 149.4"),
            ("2{,}186.1 & 149.3", r"\textbf{2{,}186.1} & 149.3"),
        ]
        for before, after in changes:
            with self.subTest(before=before):
                mutated = self.replace_once(self.tex, before, after)
                with self.assertRaises(AssertionError):
                    audit_primary_runtime(mutated, self.runtime)

    def test_exact_metric_ties_require_both_highlights(self):
        summaries = copy.deepcopy(self.runtime)
        pair = [r for r in summaries if r["dataset"] == "All-126"
                and r["objective"] == "BG-d2" and r["encoding"] in {"Direct", "Compact-Objective"}]
        for row in pair:
            row["mean_PAR2_seconds"] = "3.949"
        mutated = self.replace_once(
            self.tex, r"\textsc{BG-d2} & 87/39/0 & 9.3",
            r"\textsc{BG-d2} & 87/39/0 & \textbf{3.9}")
        audit_primary_runtime(mutated, summaries)
        missing = self.replace_once(
            mutated, r"\textsc{BG-d2} & 87/39/0 & \textbf{3.9}",
            r"\textsc{BG-d2} & 87/39/0 & 3.9")
        with self.assertRaises(AssertionError):
            audit_primary_runtime(missing, summaries)

    def test_wrong_quality_values_extrema_or_highlights_are_rejected(self):
        changes = [
            (r"\textbf{1.0} & 2.0 & 2.0 & 5.0", r"\textbf{1.0} & 2.0 & 2.0 & 6.0"),
            ("15 & 68 & 0 & $-0.520$", "14 & 69 & 0 & $-0.520$"),
            ("15 & 68 & 0 & $-0.520$", "15 & 68 & 0 & $-0.519$"),
            (r"\textbf{1.0} & 2.0 & 2.0 & 5.0", "1.0 & 2.0 & 2.0 & 5.0"),
        ]
        for before, after in changes:
            with self.subTest(before=before):
                mutated = self.replace_once(self.appendix, before, after)
                with self.assertRaises(AssertionError):
                    audit_appendix_tables(mutated, **self.evidence)

    def test_wrong_stress_counts_instances_or_measure_are_rejected(self):
        changes = [
            (r"$I_\Sigma$ & 22 & 25 & 0", r"$I_\Sigma$ & 21 & 26 & 0"),
            (r"Stress & 47 & $\Delta_I$", r"Stress & 46 & $\Delta_I$"),
            (r"Stress & 47 & $\Delta_I$", r"Stress & 47 & $I_{\max}$"),
        ]
        for before, after in changes:
            with self.subTest(before=before):
                mutated = self.replace_once(self.appendix, before, after)
                with self.assertRaises(AssertionError):
                    audit_appendix_tables(mutated, **self.evidence)

    def test_wrong_objective_counts_reductions_or_highlights_are_rejected(self):
        changes = [
            (r"\textbf{149{,}982} & 5.92", r"\textbf{149{,}982} & 5.93"),
            (r"\textsc{IS} & DT & 27{,}735 & 67{,}220",
             r"\textsc{IS} & DT & 27{,}735 & 67{,}221"),
            (r"\textbf{149{,}982} & 5.92", "149{,}982 & 5.92"),
        ]
        for before, after in changes:
            with self.subTest(before=before):
                mutated = self.replace_once(self.appendix, before, after)
                with self.assertRaises(AssertionError):
                    audit_appendix_tables(mutated, **self.evidence)

    def test_wrong_formula_counts_or_encoding_labels_are_rejected(self):
        changes = [
            (r"\textsc{Direct} & 79{,}877", r"\textsc{Direct} & 79{,}878"),
            (r"\textbf{153{,}675}", "153{,}675"),
            (r"\textsc{Direct} & 79{,}877", r"\textsc{Compact-Objective} & 79{,}877"),
        ]
        for before, after in changes:
            with self.subTest(before=before):
                mutated = self.replace_once(self.appendix, before, after)
                with self.assertRaises(AssertionError):
                    audit_appendix_tables(mutated, **self.evidence)

    def test_missing_current_table_is_rejected(self):
        mutated = self.replace_once(
            self.appendix, r"\label{tab:app-formula-summary}", r"\label{tab:old-formula-summary}")
        with self.assertRaises(AssertionError):
            audit_appendix_tables(mutated, **self.evidence)


if __name__ == "__main__":
    unittest.main()
