from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from Validate_Official_Run import (
    DATASET_SPECS,
    ORG_CONFIGURATION_ID,
    ORG_CONFIGURATION_LABEL,
    ORG_ENCODING_VARIANT,
    ORG_IMPLIED_PACKAGE_CODE,
    ORG_IMPLIED_PACKAGE_NAME,
    expected_main_cells,
    validate_output,
)
from Main import (
    benchmark_configurations,
    collect_instances,
    parse_args,
    selected_solvers,
)


class OfficialRunValidatorTests(unittest.TestCase):
    @staticmethod
    def _write_rows(path: Path, rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = sorted({key for row in rows for key in row})
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def test_canonical_official_matrix_has_expected_totals(self) -> None:
        self.assertEqual(
            [
                (spec.family, spec.path_count, spec.unique_content_count)
                for spec in DATASET_SPECS
            ],
            [
                ("original", 20, 20),
                ("forbidden", 40, 26),
                ("fixed", 40, 40),
                ("precedence", 40, 40),
            ],
        )
        self.assertEqual(
            [len(expected_main_cells(spec)) for spec in DATASET_SPECS],
            [6, 6, 6, 24],
        )
        self.assertEqual(
            sum(
                spec.unique_content_count * spec.configurations_per_instance
                for spec in DATASET_SPECS
            ),
            1476,
        )
        self.assertEqual(
            sum(
                spec.path_count * spec.configurations_per_instance
                for spec in DATASET_SPECS
            ),
            1560,
        )
        self.assertEqual(sum(spec.path_count for spec in DATASET_SPECS), 140)
        self.assertEqual(
            sum(spec.unique_content_count for spec in DATASET_SPECS),
            126,
        )

    def test_official_runner_uses_manifest_family_filters(self) -> None:
        script = (PROJECT_ROOT / "run_official_ic12p.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('--manifest "$MANIFEST"', script)
        self.assertIn('--family "$family"', script)
        self.assertNotIn('--data-dir "$NOVES_DIR"', script)
        self.assertNotIn("--keep-path-aliases", script)
        self.assertIn("--solver sat_all", script)
        self.assertNotIn("--solver all", script)
        self.assertIn("Validate_Official_Run.py", script)

    def test_main_expands_the_manifest_to_the_official_matrix(self) -> None:
        args = parse_args(
            [
                "--solver",
                "sat_all",
                "--domain-mode",
                "both",
                "--encoding-variant",
                "imp12+",
            ]
        )
        solvers = selected_solvers(args.solver)
        total = 0
        for spec in DATASET_SPECS:
            instances = collect_instances(
                None,
                None,
                str(PROJECT_ROOT / "instances_manifest.csv"),
                spec.family,
            )
            self.assertEqual(len(instances), spec.unique_content_count)
            self.assertEqual(
                sum(instance.source_alias_count for instance in instances),
                spec.path_count,
            )
            self.assertEqual(
                len({instance.content_id for instance in instances}),
                spec.unique_content_count,
            )
            counts = {
                len(benchmark_configurations(args, instance, solvers))
                for instance in instances
            }
            self.assertEqual(counts, {spec.configurations_per_instance})
            total += sum(
                len(benchmark_configurations(args, instance, solvers))
                for instance in instances
            )
        self.assertEqual(total, 1476)

    def test_validator_accepts_a_complete_canonical_matrix(self) -> None:
        with tempfile.TemporaryDirectory(prefix="b2b_official_matrix_") as temp:
            output = Path(temp)
            for spec in DATASET_SPECS:
                main_rows = []
                org_rows = []
                for index in range(spec.unique_content_count):
                    content_id = f"{spec.family}-content-{index:03d}"
                    instance_name = f"{spec.family}-path-{index:03d}"
                    alias_count = (
                        2
                        if spec.family == "forbidden" and index < 14
                        else 1
                    )
                    aliases = " | ".join(
                        f"noves/{spec.family}-source-{index:03d}-{alias}.dzn"
                        for alias in range(alias_count)
                    )
                    for domain, engine, encoding, graph in sorted(
                        expected_main_cells(spec)
                    ):
                        main_rows.append(
                            {
                                "instance": instance_name,
                                "instance_content_id": content_id,
                                "instance_family": spec.family,
                                "source_alias_paths": aliases,
                                "domain_mode": domain,
                                "optimization_engine": engine,
                                "precedence_encoding": encoding,
                                "precedence_graph": graph,
                                "idle_encoding": "span_threshold",
                                "objective_code": "IRP",
                                "encoding_variant": "imp12+",
                                "factor_i": "IC12+",
                                "implied_constraints_code": "IC12P",
                                "status": "OPTIMAL",
                            }
                        )
                    org_rows.append(
                        {
                            "instance": instance_name,
                            "instance_content_id": content_id,
                            "instance_family": spec.family,
                            "source_alias_paths": aliases,
                            "configuration_label": ORG_CONFIGURATION_LABEL,
                            "configuration_id": ORG_CONFIGURATION_ID,
                            "encoding_variant": ORG_ENCODING_VARIANT,
                            "factor_i": ORG_IMPLIED_PACKAGE_NAME,
                            "implied_constraints_code": (
                                ORG_IMPLIED_PACKAGE_CODE
                            ),
                            "precedence_mode": "traditional",
                            "precedence_configuration": "pairwise+direct",
                            "status": "OPTIMAL",
                            "objective_value": "0",
                            "best_value": "0",
                        }
                    )
                self._write_rows(
                    output
                    / "main"
                    / f"{spec.output_name}_detailed.csv",
                    main_rows,
                )
                self._write_rows(
                    output / "org" / f"{spec.output_name}_org_new.csv",
                    org_rows,
                )
            self.assertEqual(validate_output(output), [])


if __name__ == "__main__":
    unittest.main()
