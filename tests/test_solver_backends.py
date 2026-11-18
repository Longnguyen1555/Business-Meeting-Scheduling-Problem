from __future__ import annotations

import os
import queue
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pysat.examples.rc2 import RC2


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import SAT_Backend
from IncrementalSAT_Solver import B2BIncrementalSATSolver
from MaxSAT_Solver import B2BMaxSATSolver, resolve_uwrmaxsat_binary
from Main import _drain_queue, parse_args, require_solver_environment
from Multiple_SAT import B2BMultipleSATSolver


INSTANCE = PROJECT_ROOT / "data_table03_origin" / "tic-12.original.dzn"


class MaxSATBackendTests(unittest.TestCase):
    def test_default_requires_uwrmaxsat_and_never_uses_rc2(self) -> None:
        with patch.dict(
            os.environ,
            {"B2B_MAXSAT_BACKEND": "uwrmaxsat"},
            clear=False,
        ):
            with patch(
                "MaxSAT_Solver.resolve_uwrmaxsat_binary",
                return_value=None,
            ):
                with patch.object(
                    B2BMaxSATSolver,
                    "_solve_with_rc2",
                ) as rc2:
                    with self.assertRaises(FileNotFoundError):
                        B2BMaxSATSolver(INSTANCE)
                    rc2.assert_not_called()

    def test_auto_backend_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            B2BMaxSATSolver(INSTANCE, backend="auto")

    def test_rc2_remains_explicit_development_backend(self) -> None:
        result = B2BMaxSATSolver(INSTANCE, backend="rc2").solve()

        self.assertEqual(result["status"], "OPTIMAL")
        self.assertEqual(result["solver_backend"], "RC2")
        self.assertEqual(result["maxsat_backend_preference"], "rc2")

    def test_default_dispatches_to_resolved_uwrmaxsat(self) -> None:
        sentinel = {"status": "SENTINEL"}
        with tempfile.TemporaryDirectory(prefix="uwr_backend_test_") as temp:
            binary = Path(temp) / "uwrmaxsat"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            solver = B2BMaxSATSolver(INSTANCE, uwrmaxsat_bin=binary)
            with patch.object(
                solver,
                "_solve_with_uwrmaxsat",
                return_value=sentinel,
            ) as uwr:
                self.assertIs(solver.solve(), sentinel)
                uwr.assert_called_once_with(binary.resolve(), False)

    def test_uwrmaxsat_output_is_decoded_and_records_exact_binary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="uwr_integration_test_") as temp:
            binary = Path(temp) / "uwrmaxsat"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            solver = B2BMaxSATSolver(INSTANCE, uwrmaxsat_bin=binary)

            with RC2(solver._build_wcnf()) as reference:
                model = reference.compute()
                cost = int(reference.cost)
            assert model is not None
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=30,
                stdout=(
                    "s OPTIMUM FOUND\n"
                    f"o {cost}\n"
                    f"v {' '.join(map(str, model))} 0\n"
                ),
                stderr="",
            )

            with patch("MaxSAT_Solver.subprocess.run", return_value=completed):
                result = solver.solve()

            self.assertEqual(result["status"], "OPTIMAL")
            self.assertEqual(result["solver_backend"], "UWrMaxSAT")
            self.assertEqual(result["solver_cost"], cost)
            self.assertEqual(result["validation_errors"], [])
            self.assertEqual(result["solver_binary"], str(binary.resolve()))
            self.assertEqual(len(result["solver_binary_sha256"]), 64)
            self.assertIn(str(binary.resolve()), result["solver_command"])

    def test_uwrmaxsat_timeout_preserves_the_best_returned_cost(self) -> None:
        with tempfile.TemporaryDirectory(prefix="uwr_timeout_test_") as temp:
            binary = Path(temp) / "uwrmaxsat"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            solver = B2BMaxSATSolver(INSTANCE, uwrmaxsat_bin=binary)
            timeout = subprocess.TimeoutExpired(
                cmd=[str(binary)],
                timeout=1.0,
                output="o 7\n",
                stderr="",
            )

            with patch("MaxSAT_Solver.subprocess.run", side_effect=timeout):
                result = solver.solve()

            self.assertEqual(result["status"], "TIMEOUT")
            self.assertEqual(result["objective_value"], 7)
            self.assertEqual(result["solver_cost"], 7)
            self.assertIsNone(result["proven_optimum"])
            self.assertIn("best returned cost=7", result["solver_message"])

    def test_explicit_missing_path_is_not_replaced_from_path(self) -> None:
        with patch("MaxSAT_Solver.shutil.which", return_value="/other/uwrmaxsat"):
            self.assertIsNone(resolve_uwrmaxsat_binary("/missing/uwrmaxsat"))

    def test_sha256_pin_mismatch_fails_before_solving(self) -> None:
        with tempfile.TemporaryDirectory(prefix="uwr_hash_test_") as temp:
            binary = Path(temp) / "uwrmaxsat"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            with self.assertRaises(RuntimeError):
                B2BMaxSATSolver(
                    INSTANCE,
                    uwrmaxsat_bin=binary,
                    uwrmaxsat_sha256="0" * 64,
                )


class SATBackendTests(unittest.TestCase):
    def test_cadical_failure_does_not_call_glucose(self) -> None:
        cadical = Mock(side_effect=OSError("CaDiCaL unavailable"))
        glucose = Mock()
        fake_solvers = SimpleNamespace(Cadical153=cadical, Glucose3=glucose)

        with patch.object(SAT_Backend, "import_module", return_value=fake_solvers):
            with self.assertRaisesRegex(RuntimeError, "fallback is disabled"):
                SAT_Backend.create_sat_solver([], "cadical")

        cadical.assert_called_once_with(bootstrap_with=[])
        glucose.assert_not_called()

    def test_unknown_sat_backend_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SAT_Backend.create_sat_solver([], "auto")

    def test_cadical_153_is_available_for_production_sat_tests(self) -> None:
        SAT_Backend.require_sat_backend("cadical")
        self.assertEqual(SAT_Backend.sat_backend_version("cadical"), "1.5.3")

    def test_sat_optimizers_report_incumbents_and_formula_overhead(self) -> None:
        for solver_class in (B2BMultipleSATSolver, B2BIncrementalSATSolver):
            incumbents: list[int] = []
            result = solver_class(INSTANCE).solve(
                incumbent_callback=incumbents.append
            )
            with self.subTest(solver=solver_class.__name__):
                self.assertEqual(result["status"], "OPTIMAL")
                self.assertTrue(incumbents)
                self.assertEqual(min(incumbents), result["objective_value"])
                self.assertGreaterEqual(result["n_optimizer_calls"], 1)
                self.assertGreaterEqual(result["n_bound_encodings"], 0)
                self.assertGreaterEqual(
                    result["optimizer_added_clauses_peak"],
                    0,
                )


class RunnerBackendTests(unittest.TestCase):
    def test_production_backend_defaults_are_strict(self) -> None:
        args = parse_args([])
        self.assertEqual(args.maxsat_backend, "uwrmaxsat")
        self.assertEqual(args.sat_backend, "cadical")

    def test_runner_preflight_stops_before_missing_uwrmaxsat(self) -> None:
        with patch("Main.resolve_uwrmaxsat_binary", return_value=None):
            with self.assertRaises(FileNotFoundError):
                require_solver_environment(
                    ["maxsat"],
                    maxsat_backend="uwrmaxsat",
                    uwrmaxsat_bin=None,
                    uwrmaxsat_sha256=None,
                    sat_backend="cadical",
                )

    def test_runner_retains_the_best_sat_incumbent_message(self) -> None:
        output: queue.Queue[dict[str, object]] = queue.Queue()
        output.put({"message_type": "incumbent", "best_value": 5})
        output.put({"message_type": "incumbent", "best_value": 2})
        output.put({"message_type": "incumbent", "best_value": 4})
        metadata: dict[str, object] = {}

        self.assertIsNone(_drain_queue(output, metadata, None))
        self.assertEqual(metadata["incumbent_best_value"], 2)


if __name__ == "__main__":
    unittest.main()
