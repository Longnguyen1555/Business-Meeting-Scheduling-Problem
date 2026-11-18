from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from Validate_Filter_E_Run import (
    DATASET_SPECS,
    EXPECTED_CELLS,
    precedence_manifest_rows,
    validate_manifests,
    validate_output,
)


class FilterEAllPrecedenceRunTests(unittest.TestCase):
    @staticmethod
    def _write_rows(path: Path, rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = sorted({key for row in rows for key in row})
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _complete_rows(spec_index: int) -> list[dict[str, str]]:
        spec = DATASET_SPECS[spec_index]
        rows: list[dict[str, str]] = []
        for manifest_row in precedence_manifest_rows(spec):
            for cell_index, (
                engine,
                precedence_encoding,
                precedence_graph,
            ) in enumerate(sorted(EXPECTED_CELLS)):
                engine_metadata = {
                    "IncrementalSAT": (
                        "incremental",
                        "CaDiCaL",
                        "IS-CD",
                        "cadical",
                    ),
                    "NonIncrementalSAT": (
                        "multiple",
                        "CaDiCaL",
                        "NIS-CD",
                        "cadical",
                    ),
                    "UWrMaxSAT": (
                        "maxsat",
                        "UWrMaxSAT",
                        "UW",
                        "uwrmaxsat",
                    ),
                }[engine]
                solver, backend, engine_code, backend_code = engine_metadata
                factor_p, encoding_code = {
                    "pairwise": ("Pairwise", "PW"),
                    "sparse_suffix": ("SparseSuffix", "SS"),
                }[precedence_encoding]
                factor_g, graph_code = {
                    "direct": ("Direct-E", "DE"),
                    "distance_closure": ("DistanceClosure-E*", "DC"),
                }[precedence_graph]
                configuration_id = "__".join(
                    (
                        "cfg4",
                        "m-reduced",
                        "f-direct",
                        f"p-{precedence_encoding}",
                        f"g-{precedence_graph}",
                        "b-span_threshold",
                        "o-idle_range_pstar",
                        f"s-{engine.lower()}",
                        "i-imp12plus",
                        f"backend-{backend_code}",
                    )
                )
                rows.append(
                    {
                        "instance": manifest_row["canonical_instance"],
                        "instance_content_id": manifest_row["content_id"],
                        "instance_family": "precedence",
                        "instance_variant": manifest_row["variant"],
                        "instance_sha256": manifest_row["sha256"],
                        "source_alias_count": manifest_row["source_alias_count"],
                        "source_alias_paths": manifest_row["source_alias_paths"],
                        "repository_alias_count": (
                            manifest_row["repository_alias_count"]
                        ),
                        "repository_alias_paths": (
                            manifest_row["repository_alias_paths"]
                        ),
                        "dataset_source_page": (
                            manifest_row["dataset_source_page"]
                        ),
                        "dataset_archive_url": (
                            manifest_row["dataset_archive_url"]
                        ),
                        "dataset_archive_sha256": (
                            manifest_row["dataset_archive_sha256"]
                        ),
                        "configuration_label": (
                            f"R-FE-{encoding_code}-{graph_code}-ST-IRP-"
                            f"{engine_code}-IC12P"
                        ),
                        "configuration_id": configuration_id,
                        "configuration_key": configuration_id,
                        "domain_mode": "reduced",
                        "domain_filter_graph": "direct",
                        "factor_m": "Reduced",
                        "factor_f": "Filter-E",
                        "factor_p": factor_p,
                        "factor_g": factor_g,
                        "factor_b": "SpanThreshold",
                        "factor_o": "IdleRangePstar",
                        "factor_s": engine,
                        "optimization_engine": engine,
                        "solver": solver,
                        "solver_backend": backend,
                        "solver_version": (
                            "binary-sha256:" + "a" * 64
                            if engine == "UWrMaxSAT"
                            else "1.5.3"
                        ),
                        "solver_binary": (
                            "/solver/uwrmaxsat"
                            if engine == "UWrMaxSAT"
                            else ""
                        ),
                        "solver_binary_sha256": (
                            "a" * 64 if engine == "UWrMaxSAT" else ""
                        ),
                        "solver_command": (
                            "/solver/uwrmaxsat -m instance.wcnf"
                            if engine == "UWrMaxSAT"
                            else ""
                        ),
                        "precedence_encoding": precedence_encoding,
                        "precedence_graph": precedence_graph,
                        "encoding_variant": "imp12+",
                        "factor_i": "IC12+",
                        "implied_constraints_code": "IC12P",
                        "idle_encoding": "span_threshold",
                        "objective_code": "IRP",
                        "status": "TIMEOUT" if cell_index == 0 else "OPTIMAL",
                        "runtime_censored": (
                            "True" if cell_index == 0 else "False"
                        ),
                        "best_value": "" if cell_index == 0 else "2",
                        "proven_optimum": "" if cell_index == 0 else "2",
                        "objective_value": "" if cell_index == 0 else "2",
                        "idle_range_pstar": "" if cell_index == 0 else "2",
                        "reduced_schedule_candidates": "100",
                        "active_schedule_candidates": "100",
                        "precedence_direct_edges": (
                            manifest_row["n_direct_precedence_edges"]
                        ),
                        "precedence_closure_edges": (
                            str(
                                int(manifest_row["n_direct_precedence_edges"])
                                + 10
                            )
                        ),
                        "precedence_relation_edges": (
                            manifest_row["n_direct_precedence_edges"]
                            if precedence_graph == "direct"
                            else str(
                                int(manifest_row["n_direct_precedence_edges"])
                                + 10
                            )
                        ),
                        "run_started_utc": "2026-07-28T00:00:00+00:00",
                        "timeout_seconds": "7200.0",
                        "git_commit": "a" * 40,
                        "git_dirty": "False",
                        "runner_command": (
                            "python src/Main.py --domain-mode reduced "
                            "--domain-filter-graph direct "
                            "--precedence-encoding both "
                            "--precedence-graph both"
                        ),
                        "runtime_scope": "wall clock",
                        "validation_errors": "",
                        "error_type": "",
                        "error_message": "",
                    }
                )
        return rows

    def test_manifests_cover_all_140_precedence_instances(self) -> None:
        self.assertEqual(validate_manifests(), [])
        self.assertEqual(
            [
                (
                    spec.output_name,
                    spec.instance_count,
                    spec.configurations_per_instance,
                    spec.run_count,
                )
                for spec in DATASET_SPECS
            ],
            [
                ("official", 40, 12, 480),
                ("stress", 60, 12, 720),
                ("stress-high", 40, 12, 480),
            ],
        )
        self.assertEqual(sum(spec.instance_count for spec in DATASET_SPECS), 140)
        self.assertEqual(sum(spec.run_count for spec in DATASET_SPECS), 1680)
        self.assertEqual(
            [
                Counter(
                    row["variant"] for row in precedence_manifest_rows(spec)
                )
                for spec in DATASET_SPECS
            ],
            [
                Counter({"prec15": 20, "prec25": 20}),
                Counter({"prec30": 20, "prec35": 20, "prec40": 20}),
                Counter({"prec50": 20, "prec60": 20}),
            ],
        )

    def test_runner_requests_only_the_full_filter_e_factorial(self) -> None:
        script = (
            PROJECT_ROOT / "run_filter_e_all_precedence.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('"official|$ROOT/instances_manifest.csv|40|480"', script)
        self.assertIn(
            '"stress|$ROOT/data_precedence_stress/instances_manifest.csv|60|720"',
            script,
        )
        self.assertIn(
            '"stress-high|$ROOT/data_precedence_stress_high/'
            'instances_manifest.csv|40|480"',
            script,
        )
        self.assertIn("--solver sat_all", script)
        self.assertIn("--domain-mode reduced", script)
        self.assertIn("--domain-filter-graph direct", script)
        self.assertIn("--precedence-encoding both", script)
        self.assertIn("--precedence-graph both", script)
        self.assertNotIn("--domain-filter-graph both", script)

    def test_validator_accepts_complete_matrix_including_timeouts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="b2b_filter_e_run_") as temp:
            output = Path(temp)
            for index, spec in enumerate(DATASET_SPECS):
                self._write_rows(
                    output / "main" / f"{spec.output_name}_detailed.csv",
                    self._complete_rows(index),
                )
            self.assertEqual(validate_output(output), [])

    def test_validator_rejects_filter_e_star_leakage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="b2b_filter_e_run_") as temp:
            output = Path(temp)
            for index, spec in enumerate(DATASET_SPECS):
                rows = self._complete_rows(index)
                if index == 0:
                    rows[0]["domain_filter_graph"] = "distance_closure"
                    rows[0]["factor_f"] = "Filter-E*"
                self._write_rows(
                    output / "main" / f"{spec.output_name}_detailed.csv",
                    rows,
                )
            errors = validate_output(output)
            self.assertTrue(
                any("domain_filter_graph is not direct" in error for error in errors)
            )
            self.assertTrue(
                any("factor_f is not Filter-E" in error for error in errors)
            )


if __name__ == "__main__":
    unittest.main()
