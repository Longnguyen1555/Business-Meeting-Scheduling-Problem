from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from Main import (
    InstanceSpec,
    VARIANT_CODES,
    VARIANT_FACTOR_NAMES,
    benchmark_configurations,
    configuration_metadata,
    parse_args,
    precedence_configurations,
    selected_solvers,
    write_aggregate_csv,
)


class MainPrecedenceFactorialTests(unittest.TestCase):
    def test_imp12_plus_machine_code_and_display_name_are_explicit(self) -> None:
        self.assertEqual(VARIANT_CODES["imp12+"], "IC12P")
        self.assertEqual(VARIANT_FACTOR_NAMES["imp12+"], "IC12+")

    def test_default_and_explicit_cli_generate_the_expected_cells(self) -> None:
        default_args = parse_args([])
        self.assertEqual(default_args.maxsat_backend, "uwrmaxsat")
        self.assertEqual(default_args.sat_backend, "cadical")
        self.assertEqual(default_args.objective_mode, "ir")
        self.assertEqual(
            default_args.domain_filter_graph,
            "distance_closure",
        )
        self.assertEqual(
            precedence_configurations(default_args),
            [
                ("pairwise", "direct"),
                ("pairwise", "distance_closure"),
                ("sparse_suffix", "direct"),
                ("sparse_suffix", "distance_closure"),
            ],
        )

        legacy_args = parse_args(["--precedence-mode", "both"])
        self.assertEqual(
            precedence_configurations(legacy_args),
            [
                ("pairwise", "direct"),
                ("sparse_suffix", "distance_closure"),
            ],
        )

        one_row = parse_args(
            [
                "--precedence-encoding",
                "sparse_suffix",
                "--precedence-graph",
                "direct",
            ]
        )
        self.assertEqual(
            precedence_configurations(one_row),
            [("sparse_suffix", "direct")],
        )

    def test_filter_e_expands_only_meaningful_reduced_cells(self) -> None:
        instance = InstanceSpec(
            path=Path("micro.dzn"),
            instance_name="micro",
            content_id="micro-id",
            sha256="0" * 64,
            family="precedence",
            variant="precedence",
            has_precedence=True,
            source_alias_count=1,
            source_alias_paths="micro.dzn",
        )
        args = parse_args(
            [
                "--solver",
                "maxsat",
                "--domain-mode",
                "both",
                "--domain-filter-graph",
                "both",
            ]
        )
        configurations = benchmark_configurations(
            args,
            instance,
            selected_solvers(args.solver),
        )

        full = [
            config for config in configurations if config.domain_mode == "full"
        ]
        reduced = [
            config
            for config in configurations
            if config.domain_mode == "reduced"
        ]
        self.assertEqual(len(full), 4)
        self.assertEqual(
            {config.domain_filter_graph for config in full},
            {"distance_closure"},
        )
        self.assertEqual(len(reduced), 8)
        self.assertEqual(
            {config.domain_filter_graph for config in reduced},
            {"direct", "distance_closure"},
        )

    def test_filter_e_has_a_distinct_id_while_filter_e_star_stays_stable(self) -> None:
        common = {
            "solver_name": "maxsat",
            "precedence_encoding": "sparse_suffix",
            "precedence_graph": "distance_closure",
            "encoding_variant": "imp12+",
            "domain_mode": "reduced",
            "maxsat_backend": "uwrmaxsat",
            "sat_backend": "cadical",
        }
        closure = configuration_metadata(**common)
        direct = configuration_metadata(
            **common,
            domain_filter_graph="direct",
        )

        self.assertEqual(
            closure["configuration_label"],
            "R-SS-DC-ST-IRP-UW-IC12P",
        )
        self.assertTrue(closure["configuration_id"].startswith("cfg2__"))
        self.assertEqual(closure["factor_f"], "Filter-E*")
        self.assertEqual(
            direct["configuration_label"],
            "R-FE-SS-DC-ST-IRP-UW-IC12P",
        )
        self.assertTrue(direct["configuration_id"].startswith("cfg4__"))
        self.assertIn("__f-direct__", direct["configuration_id"])
        self.assertEqual(direct["factor_f"], "Filter-E")

    def test_objective_modes_have_distinct_configuration_ids(self) -> None:
        common = {
            "solver_name": "maxsat",
            "precedence_encoding": "sparse_suffix",
            "precedence_graph": "distance_closure",
            "encoding_variant": "imp12+",
            "domain_mode": "reduced",
            "maxsat_backend": "uwrmaxsat",
            "sat_backend": "cadical",
        }
        metadata = {
            objective_mode: configuration_metadata(
                **common,
                objective_mode=objective_mode,
            )
            for objective_mode in ("ir", "bg_d2", "ir_is", "bg_ir_is")
        }

        self.assertEqual(
            len({row["configuration_id"] for row in metadata.values()}),
            4,
        )
        self.assertEqual(
            metadata["ir"]["configuration_label"],
            "R-SS-DC-ST-IRP-UW-IC12P",
        )
        self.assertIn(
            "o-break_groups_d2",
            metadata["bg_d2"]["configuration_id"],
        )
        self.assertEqual(
            metadata["ir_is"]["factor_o"],
            "IdleRangeThenIdleSum",
        )

    def test_non_ir_modes_reject_unimplemented_exact_baselines(self) -> None:
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(
                    [
                        "--solver",
                        "exact_all",
                        "--objective-mode",
                        "ir_is",
                    ]
                )

        with self.assertRaises(ValueError):
            configuration_metadata(
                solver_name="gurobi_mip",
                precedence_encoding="native_linear",
                precedence_graph="distance_closure",
                encoding_variant="n/a",
                domain_mode="reduced",
                maxsat_backend="uwrmaxsat",
                sat_backend="cadical",
                objective_mode="bg_d2",
            )

    def test_legacy_and_independent_flags_cannot_be_mixed(self) -> None:
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(
                    [
                        "--precedence-mode",
                        "traditional",
                        "--precedence-graph",
                        "direct",
                    ]
                )

    def test_aggregate_csv_keeps_f_p_g_factorial_rows_separate(self) -> None:
        results = []
        for domain_filter_graph in ("direct", "distance_closure"):
            for precedence_encoding in ("pairwise", "sparse_suffix"):
                for precedence_graph in ("direct", "distance_closure"):
                    results.append(
                        {
                            "instance": "micro",
                            "domain_filter_graph": domain_filter_graph,
                            "precedence_encoding": precedence_encoding,
                            "precedence_graph": precedence_graph,
                            "precedence_configuration": (
                                f"{precedence_encoding}+{precedence_graph}"
                            ),
                            "solver": "maxsat",
                            "domain_mode": "reduced",
                            "encoding_variant": "imp12+",
                            "sat_result": "SAT",
                            "runtime_seconds": 0.1,
                            "idle_range_pstar": 0,
                        }
                    )

        with tempfile.TemporaryDirectory(prefix="b2b_factorial_test_") as temp:
            output = Path(temp) / "aggregate.csv"
            write_aggregate_csv(output, results)
            with output.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))

        self.assertEqual(len(rows), 8)
        self.assertEqual(
            {
                (
                    row["domain_filter_graph"],
                    row["precedence_encoding"],
                    row["precedence_graph"],
                )
                for row in rows
            },
            {
                (domain_filter_graph, precedence_encoding, precedence_graph)
                for domain_filter_graph in ("direct", "distance_closure")
                for precedence_encoding in ("pairwise", "sparse_suffix")
                for precedence_graph in ("direct", "distance_closure")
            },
        )
        self.assertNotIn("staircase", rows[0])


if __name__ == "__main__":
    unittest.main()
