"""Independent schedule enumeration for the All-126 waiting study."""
from itertools import product
from pathlib import Path
import sys

import pytest
from pysat.solvers import Solver

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from B2B_Instance import B2BInstance, B2BSATModel, validate_schedule_assignment
from Journal_Metrics import evaluate_journal_schedule
from MaxSAT_Solver import B2BMaxSATSolver
from Multiple_SAT import B2BMultipleSATSolver
from IncrementalSAT_Solver import B2BIncrementalSATSolver
from test_journal_objectives import (
    _fixed_positive_instance, _lexicographic_tie_instance,
    _generated_objective_instance, _fairness_cap_infeasible_instance,
)


def single_participant_instance():
    return B2BInstance(
        n_business=3, n_meetings=2, n_tables=1,
        n_total_slots=4, n_morning_slots=2,
        requested=[(0, 1, 3), (0, 2, 3)],
        meetings_by_business=[[0, 1], [0], [1]],
        n_meetings_business=[2, 1, 1], forbidden=[set(), set(), set()],
        fixed=[0, 3], precedences=[set(), set()], instance_name="singleton-pstar",
    )


def empty_pstar_instance():
    return B2BInstance(
        n_business=2, n_meetings=1, n_tables=1,
        n_total_slots=2, n_morning_slots=1,
        requested=[(0, 1, 3)], meetings_by_business=[[0], [0]],
        n_meetings_business=[1, 1], forbidden=[set(), set()],
        fixed=[None], precedences=[set()], instance_name="empty-pstar",
    )


@pytest.mark.parametrize("mode", ["is", "isq", "im_is"])
@pytest.mark.parametrize("factory", [
    _fixed_positive_instance, _lexicographic_tie_instance,
    single_participant_instance, empty_pstar_instance,
    _fairness_cap_infeasible_instance,
])
def test_every_feasible_assignment_has_exact_cost(mode, factory):
    inst = factory()
    for compact in ("reference", "demand_driven", "optimized"):
        model = B2BSATModel(inst, objective_mode=mode, compact_encoding=compact,
                            collision_amo_encoding="adaptive_commander")
        artifacts = model.build_base_cnf()
        # IS, ISQ and IM-IS must not build minimum/range states.
        assert artifacts.direct_range_soft_clause_count == 0
        assert not model._direct_range_soft_clauses.get("idle_slots")
        with Solver(name="g3", bootstrap_with=artifacts.cnf.clauses) as solver:
            for assignment in product(range(inst.n_total_slots), repeat=inst.n_meetings):
                if validate_schedule_assignment(inst, list(assignment)):
                    continue
                assumptions = [model.x(m, slot) for m, slot in enumerate(assignment)]
                assert solver.solve(assumptions=assumptions)
                sat_model = solver.get_model()
                metrics = evaluate_journal_schedule(inst, assignment, objective_mode=mode)
                assert model.encoded_objective_vector(sat_model) == metrics.objective_vector
                assert not model.objective_consistency_errors(sat_model, model.compute_stats(list(assignment)))
                wcnf = model.build_wcnf()
                positive = set(lit for lit in sat_model if lit > 0)
                cost = sum(weight for clause, weight in zip(wcnf.soft, wcnf.wght)
                           if not any((lit in positive) if lit > 0 else (-lit not in positive)
                                      for lit in clause))
                assert cost == model.encoded_objective_value(sat_model)
                if mode == "im_is":
                    assert artifacts.objective_tiers[0].scalar_weight > artifacts.objective_tiers[1].upper_bound


@pytest.mark.parametrize("mode", ["is", "isq", "im_is", "ir_im_is"])
def test_optima_match_brute_force_across_domains_and_presets(mode):
    instances = [_fixed_positive_instance(), single_participant_instance(), empty_pstar_instance()]
    instances += [_generated_objective_instance(seed) for seed in range(15)]
    for inst in instances:
        values = [evaluate_journal_schedule(inst, a, objective_mode=mode).objective_vector
                  for a in product(range(inst.n_total_slots), repeat=inst.n_meetings)
                  if not validate_schedule_assignment(inst, list(a))]
        expected = min(values) if values else None
        for domain in ("full", "reduced"):
            for compact in ("reference", "demand_driven", "optimized"):
                result = B2BMaxSATSolver(inst, backend="rc2", objective_mode=mode,
                    domain_mode=domain, compact_encoding=compact,
                    collision_amo_encoding="adaptive_commander").solve()
                assert result["status"] == ("OPTIMAL" if expected is not None else "UNSAT")
                assert result["proven_objective_vector"] == expected
                assert not result["validation_errors"]


@pytest.mark.parametrize("solver_class", [B2BMultipleSATSolver, B2BIncrementalSATSolver])
def test_sat_maximum_then_sum_and_weighted_rejection(solver_class):
    result = solver_class(single_participant_instance(), objective_mode="im_is",
                          compact_encoding="optimized").solve()
    assert result["proven_objective_vector"] == (2, 2)
    for inst in (single_participant_instance(), empty_pstar_instance()):
        with pytest.raises(ValueError, match="MaxSAT-only"):
            solver_class(inst, objective_mode="isq")


@pytest.mark.parametrize(
    "factory",
    [
        _fixed_positive_instance,
        _lexicographic_tie_instance,
        single_participant_instance,
        empty_pstar_instance,
        _fairness_cap_infeasible_instance,
    ],
)
def test_ir_im_is_has_exact_three_tier_cost_for_every_assignment(factory):
    inst = factory()
    for compact in ("reference", "demand_driven", "optimized"):
        model = B2BSATModel(
            inst,
            objective_mode="ir_im_is",
            compact_encoding=compact,
            collision_amo_encoding="adaptive_commander",
        )
        artifacts = model.build_base_cnf()
        range_tier, maximum_tier, sum_tier = artifacts.objective_tiers
        assert maximum_tier.scalar_weight > sum_tier.upper_bound
        assert range_tier.scalar_weight > (
            maximum_tier.upper_bound * maximum_tier.scalar_weight
            + sum_tier.upper_bound
        )
        with Solver(name="g3", bootstrap_with=artifacts.cnf.clauses) as solver:
            for assignment in product(
                range(inst.n_total_slots), repeat=inst.n_meetings
            ):
                if validate_schedule_assignment(inst, list(assignment)):
                    continue
                assumptions = [
                    model.x(meeting, slot)
                    for meeting, slot in enumerate(assignment)
                ]
                assert solver.solve(assumptions=assumptions)
                sat_model = solver.get_model()
                metrics = evaluate_journal_schedule(
                    inst,
                    assignment,
                    objective_mode="ir_im_is",
                )
                assert (
                    model.encoded_objective_vector(sat_model)
                    == metrics.objective_vector
                )
                assert not model.objective_consistency_errors(
                    sat_model,
                    model.compute_stats(list(assignment)),
                )


@pytest.mark.parametrize(
    "solver_class", [B2BMultipleSATSolver, B2BIncrementalSATSolver]
)
def test_sat_idle_range_maximum_then_sum(solver_class):
    result = solver_class(
        single_participant_instance(),
        objective_mode="ir_im_is",
        compact_encoding="demand_driven",
    ).solve()
    assert result["proven_objective_vector"] == (0, 2, 2)
    with pytest.raises(ValueError, match="MaxSAT-only"):
        solver_class(
            single_participant_instance(),
            objective_mode="ir_im_is",
            compact_encoding="optimized",
        )


def test_all126_matrix_has_13_distinct_cells_per_input():
    import json
    from collections import Counter
    from Journal_Experiment import read_config, resolve_datasets, build_plan
    root = Path(__file__).resolve().parents[1]
    config = read_config(root / "journal_configs/all126_first.json")
    plan = build_plan(config, resolve_datasets(config))
    assert plan["job_count"] == 1638
    assert set(Counter(j["instance_content_id"] for j in plan["jobs"]).values()) == {13}
    assert {j["repetition"] for j in plan["jobs"]} == {1}
    assert Counter(j["experiment_block"] for j in plan["jobs"]) == {
        "t1_bg_fidelity_ablation": 882, "t2_waiting_priorities": 504,
        "t3_imis_ablation_additional": 252,
    }
    cells = set()
    for job in plan["jobs"]:
        effective = {k: v for k, v in job["configuration"].items() if k != "id"}
        key = (job["instance_content_id"], json.dumps(effective, sort_keys=True))
        assert key not in cells
        cells.add(key)
    config["expected_job_count"] = 1
    with pytest.raises(ValueError, match="expects"):
        build_plan(config, resolve_datasets(config))
    smoke = read_config(root / "journal_configs/all126_first_smoke.json")
    assert build_plan(smoke, resolve_datasets(smoke))["job_count"] == 156


def test_validator_rejects_wrong_squared_and_maximum_costs():
    from Validate_Journal_Run import waiting_metric_errors
    row = dict(objective_mode="im_is", participant_internal_idle_slots="0,2,1",
        total_internal_idle_slots=3, maximum_internal_idle_slots=2, squared_internal_idle_slots=5,
        objective_vector="2,3", proven_objective_vector="2,3", objective_tier_weights="10,1",
        lexicographic_scalar_cost=23)
    assert waiting_metric_errors(row) == []
    assert waiting_metric_errors(dict(row, objective_vector="0,3"))
    assert waiting_metric_errors(dict(row, squared_internal_idle_slots=3))
    assert waiting_metric_errors(dict(row, lexicographic_scalar_cost=5))

    ir_im_is_row = dict(
        row,
        objective_mode="ir_im_is",
        objective_participants="2,3",
        idle_range_pstar=1,
        objective_vector="1,2,3",
        proven_objective_vector="1,2,3",
        objective_tier_weights="20,4,1",
        lexicographic_scalar_cost=31,
    )
    assert waiting_metric_errors(ir_im_is_row) == []
    assert waiting_metric_errors(dict(ir_im_is_row, idle_range_pstar=2))
    assert waiting_metric_errors(dict(ir_im_is_row, objective_vector="0,2,3"))


def test_all13_cli_jobs_resume_and_validate(tmp_path):
    import json
    import subprocess
    from Journal_Experiment import read_config
    from Validate_Journal_Run import validate_campaign
    root = Path(__file__).resolve().parents[1]
    config = read_config(root / "journal_configs/all126_first.json")
    for key in ("blocks_from", "resolved_blocks_from", "resolved_blocks_sha256"):
        config.pop(key, None)
    config.update(campaign_name="test-all13-development", expected_job_count=13,
                  timeout_seconds=30, require_clean_worktree=False, required_machine={})
    config["datasets"][0]["instance_names"] = ["tic-12crafc.fixed020-Forb"]
    for block in config["blocks"]:
        for cell in block["configurations"]:
            cell["backend" if cell["executor"] == "org_bg_d2" else "maxsat_backend"] = "rc2"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    output = tmp_path / "campaign"
    command = [sys.executable, str(root / "src/Journal_Experiment.py"),
               "--config", str(path), "--output-dir", str(output), "--allow-dirty"]
    for extra, count in ((["--max-runs", "4"], 4), (["--resume"], 13), (["--resume"], 13)):
        completed = subprocess.run(command + extra, cwd=root, capture_output=True,
                                   text=True, timeout=60)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert len((output / "raw/results.jsonl").read_text().splitlines()) == count
    errors, report = validate_campaign(output, allow_dirty=True)
    assert not errors, errors
    assert report["valid"]
