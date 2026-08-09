from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from Excel_Results import RESULT_COLUMNS, safe_workbook_name, write_instance_workbook
from Main import configuration_metadata


SPREADSHEET_NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class ExcelResultsTests(unittest.TestCase):
    def test_configuration_names_are_stable_and_factor_complete(self) -> None:
        metadata = configuration_metadata(
            solver_name="maxsat",
            precedence_encoding="sparse_suffix",
            precedence_graph="distance_closure",
            encoding_variant="imp12+",
            domain_mode="reduced",
            maxsat_backend="uwrmaxsat",
            sat_backend="cadical",
        )
        self.assertEqual(
            metadata["configuration_label"],
            "R-SS-DC-ST-IRP-UW-IC12P",
        )
        self.assertEqual(
            metadata["configuration_id"],
            "cfg2__m-reduced__p-sparse_suffix__g-distance_closure__"
            "b-span_threshold__o-idle_range_pstar__s-uwrmaxsat__"
            "i-imp12plus__backend-uwrmaxsat",
        )
        self.assertEqual(metadata["optimization_engine"], "UWrMaxSAT")
        self.assertEqual(metadata["model_family_display_name"], "Compact")
        self.assertEqual(
            metadata["model_configuration_display_name"],
            "Compact/Reduced",
        )
        self.assertEqual(
            metadata["optimization_procedure_display_name"],
            "one-shot weighted MaxSAT",
        )
        self.assertEqual(
            [metadata[f"factor_{factor}"] for factor in "mfpgbosi"],
            [
                "Reduced",
                "Filter-E*",
                "SparseSuffix",
                "DistanceClosure-E*",
                "SpanThreshold",
                "IdleRangePstar",
                "UWrMaxSAT",
                "IC12+",
            ],
        )

    def test_display_labels_and_machine_keys_are_unique_for_every_cli_tuple(self) -> None:
        labels: set[str] = set()
        identifiers: set[str] = set()
        expected = 0
        for domain_mode in ("full", "reduced"):
            for precedence_encoding in ("pairwise", "sparse_suffix"):
                for precedence_graph in ("direct", "distance_closure"):
                    for encoding_variant in (
                        "basic",
                        "imp1",
                        "imp2",
                        "imp12",
                        "imp12+",
                    ):
                        engine_backends = (
                            ("maxsat", "uwrmaxsat", "cadical"),
                            ("maxsat", "rc2", "cadical"),
                            ("multiple", "uwrmaxsat", "cadical"),
                            ("multiple", "uwrmaxsat", "glucose"),
                            ("incremental", "uwrmaxsat", "cadical"),
                            ("incremental", "uwrmaxsat", "glucose"),
                        )
                        for solver_name, maxsat_backend, sat_backend in engine_backends:
                            metadata = configuration_metadata(
                                solver_name=solver_name,
                                precedence_encoding=precedence_encoding,
                                precedence_graph=precedence_graph,
                                encoding_variant=encoding_variant,
                                domain_mode=domain_mode,
                                maxsat_backend=maxsat_backend,
                                sat_backend=sat_backend,
                            )
                            expected += 1
                            labels.add(metadata["configuration_label"])
                            identifiers.add(metadata["configuration_id"])

        self.assertEqual(len(labels), expected)
        self.assertEqual(len(identifiers), expected)

    def test_writer_creates_one_valid_workbook_with_required_metrics(self) -> None:
        result = {
            "instance": "micro.prec15",
            "configuration_label": "R-SS-DC-ST-IRP-UW-IC12P",
            "configuration_id": "cfg1__micro",
            "domain_mode": "reduced",
            "precedence_encoding": "sparse_suffix",
            "precedence_graph": "distance_closure",
            "optimization_engine": "UWrMaxSAT",
            "solver_backend": "UWrMaxSAT",
            "encoding_variant": "imp12+",
            "idle_encoding": "span_threshold",
            "objective": "internal_idle_slot_range_pstar",
            "n_vars": 123,
            "n_primary_variables": 45,
            "n_auxiliary_variables": 78,
            "n_total_clauses": 457,
            "n_hard_clauses": 450,
            "n_soft_clauses": 7,
            "formula_scope": "test formula",
            "runtime_seconds": 1.25,
            "model_build_seconds": 0.25,
            "solve_and_validate_seconds": 1.0,
            "runtime_scope": "test runtime",
            "runtime_censored": False,
            "status": "OPTIMAL",
            "sat_result": "SAT",
            "best_value": 2,
            "proven_optimum": 2,
            "peak_memory_mb": 42.5,
            "validation_errors": "",
            "solver_message": "",
            "error_type": "",
            "error_message": "",
        }

        with tempfile.TemporaryDirectory(prefix="b2b_xlsx_test_") as temp:
            output = Path(temp) / "micro.prec15.xlsx"
            write_instance_workbook(output, "micro.prec15", [result])

            self.assertTrue(output.is_file())
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.testzip(), None)
                required_entries = {
                    "[Content_Types].xml",
                    "xl/workbook.xml",
                    "xl/styles.xml",
                    "xl/worksheets/sheet1.xml",
                    "xl/worksheets/sheet2.xml",
                }
                self.assertTrue(required_entries.issubset(archive.namelist()))
                sheet = ElementTree.fromstring(
                    archive.read("xl/worksheets/sheet1.xml")
                )

            rows = sheet.findall(".//x:sheetData/x:row", SPREADSHEET_NS)
            self.assertEqual(len(rows), 2)
            header_values = [
                cell.find("x:is/x:t", SPREADSHEET_NS).text
                for cell in rows[0].findall("x:c", SPREADSHEET_NS)
            ]
            self.assertEqual(
                header_values,
                [column.key for column in RESULT_COLUMNS],
            )

            cells_by_ref = {
                cell.attrib["r"]: cell
                for cell in rows[1].findall("x:c", SPREADSHEET_NS)
            }
            variable_column = next(
                index
                for index, column in enumerate(RESULT_COLUMNS, start=1)
                if column.key == "n_vars"
            )
            variable_cell = cells_by_ref[f"{self._column_name(variable_column)}2"]
            self.assertEqual(variable_cell.find("x:v", SPREADSHEET_NS).text, "123")
            required_metric_columns = {
                "instance_sha256",
                "configuration_key",
                "factor_m",
                "factor_f",
                "factor_i",
                "domain_filter_graph",
                "n_primary_variables",
                "n_auxiliary_variables",
                "n_total_literals",
                "max_hard_clause_length",
                "optimizer_added_clauses_peak",
                "input_parsing_seconds",
                "model_construction_seconds",
                "runner_command",
            }
            self.assertTrue(
                required_metric_columns.issubset(set(header_values))
            )

    def test_safe_workbook_name_preserves_normal_instance_names(self) -> None:
        self.assertEqual(
            safe_workbook_name("tic-12.original"),
            "tic-12.original.xlsx",
        )
        self.assertEqual(safe_workbook_name("bad/name"), "bad_name.xlsx")

    @staticmethod
    def _column_name(index: int) -> str:
        chars: list[str] = []
        while index:
            index, remainder = divmod(index - 1, 26)
            chars.append(chr(ord("A") + remainder))
        return "".join(reversed(chars))


if __name__ == "__main__":
    unittest.main()
