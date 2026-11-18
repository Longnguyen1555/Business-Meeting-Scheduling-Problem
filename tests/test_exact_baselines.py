from __future__ import annotations

import sys
import csv
import tempfile
import unittest
from itertools import product
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from B2B_Instance import (
    B2BInstance,
    compute_solution_stats,
    validate_schedule_assignment,
)
from CPLEX_CP_Solver import B2BCPLEXCPSolver
from CPLEX_MIP_Solver import B2BCPLEXMIPSolver
from Exact_Model_Common import load_exact_context
from Gurobi_MIP_Solver import B2BGurobiMIPSolver
from MIP_SpanRange import build_mip_span_range, derive_mip_values
from Main import (
    InstanceSpec,
    _formula_metadata,
    benchmark_configurations,
    configuration_metadata,
    parse_args,
    require_solver_environment,
    selected_solvers,
    write_aggregate_csv,
)
from MaxSAT_Solver import B2BMaxSATSolver


def _fixed_pstar_instance() -> B2BInstance:
    return B2BInstance(
        n_business=4,
        n_meetings=4,
        n_tables=2,
        n_total_slots=5,
        n_morning_slots=2,
        requested=[
            (0, 1, 3),
            (0, 2, 3),
            (1, 2, 3),
            (3, 0, 3),
        ],
        meetings_by_business=[
            [0, 1, 3],
            [0, 2],
            [1, 2],
            [3],
        ],
        n_meetings_business=[3, 2, 2, 1],
        forbidden=[set() for _ in range(4)],
        fixed=[0, 4, 2, 1],
        precedences=[set() for _ in range(4)],
        instance_name="exact-fixed-pstar",
    )


def _precedence_chain_instance() -> B2BInstance:
    return B2BInstance(
        n_business=6,
        n_meetings=3,
        n_tables=2,
        n_total_slots=5,
        n_morning_slots=2,
        requested=[(0, 1, 3), (2, 3, 3), (4, 5, 3)],
        meetings_by_business=[[0], [0], [1], [1], [2], [2]],
        n_meetings_business=[1] * 6,
        forbidden=[set() for _ in range(6)],
        fixed=[None, None, None],
        precedences=[set(), {0}, {1}],
        instance_name="exact-precedence-chain",
    )


class ExactMIPSpecificationTests(unittest.TestCase):
    def test_zero_based_idle_identity_and_sparse_occupancy_are_exact(self) -> None:
        inst = _fixed_pstar_instance()
        context = load_exact_context(inst)
        spec, artifacts = build_mip_span_range(context)
        assignment = [0, 4, 2, 1]
        values = derive_mip_values(context, spec, assignment)

        self.assertEqual(spec.constraint_violations(values), [])
        stats = compute_solution_stats(inst, assignment)
        self.assertEqual(stats.participant_internal_idle_slots, [2, 1, 1, 0])
        self.assertEqual(stats.idle_range, 1)
        self.assertEqual(values[spec.idle_range], 1)

        # Only active (p,t) pairs receive y variables: 3+2+2 instead of 3*H.
        self.assertEqual(len(spec.y), 7)
        self.assertLess(len(spec.y), len(context.objective_participants) * 5)
        self.assertNotIn((1, 4), spec.y)
        self.assertEqual(artifacts.n_binary_variables, len(spec.x) + len(spec.y) + 30)
        self.assertEqual(artifacts.n_nonzeros, spec.n_nonzeros)

    def test_gurobi_and_cplex_mip_constructors_share_identical_ir(self) -> None:
        inst = _fixed_pstar_instance()
        gurobi = B2BGurobiMIPSolver(inst)
        cplex = B2BCPLEXMIPSolver(inst)

        self.assertEqual(gurobi.spec.variables, cplex.spec.variables)
        self.assertEqual(gurobi.spec.constraints, cplex.spec.constraints)
        self.assertEqual(gurobi.spec.objective, cplex.spec.objective)
        self.assertEqual(
            gurobi.artifacts.formulation_name,
            cplex.artifacts.formulation_name,
        )

    def test_distance_closure_uses_longest_chain_distance(self) -> None:
        context = load_exact_context(_precedence_chain_instance())
        spec, _ = build_mip_span_range(context)
        closure_constraint = next(
            constraint
            for constraint in spec.constraints
            if constraint.name == "precedence_0_2"
        )
        self.assertEqual(closure_constraint.rhs, -2)

    def test_mip_objective_matches_existing_maxsat_on_fixed_schedule(self) -> None:
        inst = _fixed_pstar_instance()
        context = load_exact_context(inst)
        spec, _ = build_mip_span_range(context)
        values = derive_mip_values(context, spec, [0, 4, 2, 1])
        maxsat = B2BMaxSATSolver(
            inst,
            precedence_mode="traditional",
            encoding_variant="basic",
            backend="rc2",
        ).solve()

        self.assertEqual(maxsat["status"], "OPTIMAL")
        self.assertEqual(maxsat["objective_value"], values[spec.idle_range])

    def test_ir_feasibility_matches_independent_validator_exhaustively(self) -> None:
        inst = B2BInstance(
            n_business=4,
            n_meetings=3,
            n_tables=1,
            n_total_slots=4,
            n_morning_slots=2,
            requested=[(0, 1, 3), (0, 2, 3), (1, 3, 3)],
            meetings_by_business=[[0, 1], [0, 2], [1], [2]],
            n_meetings_business=[2, 2, 1, 1],
            forbidden=[set() for _ in range(4)],
            fixed=[None, None, None],
            precedences=[set(), {0}, set()],
            instance_name="exact-exhaustive",
        )
        context = load_exact_context(inst)
        spec, _ = build_mip_span_range(context)
        for candidate in product(*context.domains):
            assignment = list(candidate)
            mip_feasible = not spec.constraint_violations(
                derive_mip_values(context, spec, assignment)
            )
            independently_feasible = not validate_schedule_assignment(
                inst,
                assignment,
                graph=context.graph,
            )
            self.assertEqual(
                mip_feasible,
                independently_feasible,
                assignment,
            )


class ExactCPModelTests(unittest.TestCase):
    def test_cp_uses_meeting_times_and_global_constraints(self) -> None:
        solver = B2BCPLEXCPSolver(_fixed_pstar_instance())
        spec = solver.cp_spec

        self.assertEqual(len(spec.time_domains), 4)
        self.assertEqual(len(spec.all_different_groups), 3)
        self.assertEqual(spec.capacity_values, (0, 1, 2, 3, 4))
        self.assertEqual(solver.artifacts.n_integer_variables, 4)
        self.assertEqual(
            solver.artifacts.n_global_constraints,
            len(spec.all_different_groups) + 5,
        )
        self.assertEqual(
            solver.artifacts.objective_encoding,
            "native_min_max_span_range",
        )

    def test_cp_precedence_spec_contains_distance_two_closure(self) -> None:
        solver = B2BCPLEXCPSolver(_precedence_chain_instance())
        self.assertIn((0, 2, 2), solver.cp_spec.precedence_relations)


class ExactRunnerConfigurationTests(unittest.TestCase):
    def test_exact_solvers_are_not_multiplied_by_sat_factors(self) -> None:
        args = parse_args(
            [
                "--solver",
                "all",
                "--encoding-variant",
                "all",
                "--domain-mode",
                "both",
            ]
        )
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
        configurations = benchmark_configurations(
            args,
            instance,
            selected_solvers(args.solver),
        )

        for solver_name in ("gurobi_mip", "cplex_mip", "cplex_cp"):
            exact_cells = [
                configuration
                for configuration in configurations
                if configuration.solver_name == solver_name
            ]
            self.assertEqual(len(exact_cells), 1)
            self.assertEqual(exact_cells[0].domain_mode, "reduced")
            self.assertEqual(
                exact_cells[0].precedence_graph,
                "distance_closure",
            )
            self.assertEqual(exact_cells[0].encoding_variant, "n/a")

    def test_exact_configuration_identity_is_stable(self) -> None:
        metadata = configuration_metadata(
            solver_name="gurobi_mip",
            precedence_encoding="native_linear",
            precedence_graph="distance_closure",
            encoding_variant="n/a",
            domain_mode="reduced",
            maxsat_backend="uwrmaxsat",
            sat_backend="cadical",
        )
        self.assertEqual(metadata["configuration_label"], "R-DC-IRP-GRB-MIP")
        self.assertEqual(metadata["factor_i"], "N/A")
        self.assertEqual(metadata["idle_encoding"], "prefix_suffix_span_range")

    def test_exact_formula_metadata_uses_formalism_specific_counts(self) -> None:
        solver = B2BGurobiMIPSolver(_fixed_pstar_instance())
        metadata = _formula_metadata(
            "gurobi_mip",
            solver,
            input_parsing_seconds=0.01,
            model_construction_seconds=0.02,
            model_build_seconds=0.03,
        )
        self.assertEqual(metadata["formalism"], "MIP")
        self.assertGreater(metadata["n_linear_constraints"], 0)
        self.assertGreater(metadata["n_nonzeros"], 0)
        self.assertIsNone(metadata["n_hard_clauses"])

    def test_aggregate_csv_has_a_dedicated_exact_column(self) -> None:
        result = {
            "instance": "micro",
            "precedence_encoding": "native_linear",
            "precedence_graph": "distance_closure",
            "precedence_configuration": "native_linear+distance_closure",
            "solver": "gurobi_mip",
            "domain_mode": "reduced",
            "encoding_variant": "n/a",
            "sat_result": "SAT",
            "runtime_seconds": 0.25,
            "idle_range_pstar": 1,
        }
        with tempfile.TemporaryDirectory(prefix="b2b_exact_aggregate_") as temp:
            path = Path(temp) / "aggregate.csv"
            write_aggregate_csv(path, [result])
            with path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["n/a"], "0.2 1")

    def test_preflight_fails_instead_of_substituting_a_solver(self) -> None:
        with patch("Main.importlib.util.find_spec", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "never substitutes"):
                require_solver_environment(
                    ["gurobi_mip"],
                    maxsat_backend="uwrmaxsat",
                    uwrmaxsat_bin=None,
                    uwrmaxsat_sha256=None,
                    sat_backend="cadical",
                )


if __name__ == "__main__":
    unittest.main()
