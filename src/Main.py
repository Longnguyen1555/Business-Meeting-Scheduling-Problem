from __future__ import annotations

import argparse
import csv
import importlib.util
import multiprocessing as mp
import os
import platform
import queue as queue_module
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pysat import __version__ as pysat_version

try:
    import psutil
except ImportError:  # Memory remains optional for benchmark portability.
    psutil = None

from IncrementalSAT_Solver import B2BIncrementalSATSolver
from B2B_Instance import (
    VALID_COLLISION_AMO_ENCODINGS,
    VALID_COMPACT_ENCODINGS,
    VALID_OBJECTIVE_MODES,
    read_instance,
)
from CPLEX_CP_Solver import B2BCPLEXCPSolver
from CPLEX_MIP_Solver import B2BCPLEXMIPSolver
from Dataset_Manifest import (
    DATASET_ARCHIVE_URL,
    DATASET_SOURCE_PAGE,
    base_lineage_id,
    classify_instance_name,
    file_sha256,
)
from MaxSAT_Solver import (
    B2BMaxSATSolver,
    UWRMAXSAT_NOT_FOUND_MESSAGE,
    executable_sha256,
    resolve_uwrmaxsat_binary,
)
from Multiple_SAT import B2BMultipleSATSolver
from Gurobi_MIP_Solver import B2BGurobiMIPSolver
from SAT_Backend import require_sat_backend
from Excel_Results import (
    FORMULA_SCOPE,
    RESULT_COLUMNS,
    RUNTIME_SCOPE,
    safe_workbook_name,
    write_instance_workbook,
)


VARIANTS = ["basic", "imp1", "imp2", "imp12", "imp12+"]
AGGREGATE_VARIANTS = [*VARIANTS, "n/a"]
SAT_SOLVERS = ["incremental", "multiple", "maxsat"]
EXACT_SOLVERS = ["gurobi_mip", "cplex_mip", "cplex_cp"]
SOLVERS = [*SAT_SOLVERS, *EXACT_SOLVERS]
PRECEDENCE_MODES = ["traditional", "staircase"]
PRECEDENCE_ENCODINGS = ["pairwise", "sparse_suffix"]
PRECEDENCE_GRAPHS = ["direct", "distance_closure"]
DOMAIN_FILTER_GRAPHS = ["direct", "distance_closure"]
DOMAIN_MODES = ["full", "reduced"]
OBJECTIVE_MODES = sorted(VALID_OBJECTIVE_MODES)
COMPACT_ENCODINGS = sorted(VALID_COMPACT_ENCODINGS)
COLLISION_AMO_ENCODINGS = sorted(VALID_COLLISION_AMO_ENCODINGS)
MAXSAT_BACKENDS = ["uwrmaxsat", "rc2"]
SAT_BACKENDS = ["cadical", "glucose"]
SAT_BACKEND_CODES = {"cadical": "CD", "glucose": "GL"}
MEMORY_SAMPLE_INTERVAL_SECONDS = 0.05
QUEUE_GRACE_SECONDS = 1.0
MAXSAT_REPORTING_MARGIN_SECONDS = 0.25
DOMAIN_CODES = {"full": "F", "reduced": "R"}
PRECEDENCE_ENCODING_CODES = {"pairwise": "PW", "sparse_suffix": "SS"}
PRECEDENCE_GRAPH_CODES = {"direct": "DE", "distance_closure": "DC"}
# Compact labels use ASCII-safe codes; factor_i keeps the publication-facing
# display name. In particular, IC12P is the compact code for display name
# IC12+ and CLI variant imp12+.
VARIANT_CODES = {
    "basic": "IC0",
    "imp1": "IC1",
    "imp2": "IC2",
    "imp12": "IC12",
    "imp12+": "IC12P",
}
VARIANT_FACTOR_NAMES = {
    "basic": "Base",
    "imp1": "IC1",
    "imp2": "IC2",
    "imp12": "IC12",
    "imp12+": "IC12+",
}
OBJECTIVE_CODES = {
    "ir": "IRP",
    "bg_d2": "BGD2",
    "ir_is": "IRIS",
    "ir_im_is": "IRIMIS",
    "bg_ir_is": "BGIRIS",
    "is": "IS",
    "isq": "ISQ",
    "im_is": "IMIS",
}
OBJECTIVE_NAMES = {
    "ir": "IdleRangePstar",
    "bg_d2": "BreakGroupsD2",
    "ir_is": "IdleRangeThenIdleSum",
    "ir_im_is": "IdleRangeThenMaximumIdleThenIdleSum",
    "bg_ir_is": "BreakGroupsThenIdleRangeThenIdleSum",
    "is": "IdleSum",
    "isq": "SquaredIdleSum",
    "im_is": "MaximumIdleThenIdleSum",
}
OBJECTIVE_KEYS = {
    "ir": "idle_range_pstar",
    "bg_d2": "break_groups_d2",
    "ir_is": "idle_range_then_idle_sum",
    "ir_im_is": "idle_range_maximum_idle_sum",
    "bg_ir_is": "break_groups_idle_range_idle_sum",
    "is": "idle_sum",
    "isq": "squared_idle_sum",
    "im_is": "maximum_idle_then_idle_sum",
}
COMPACT_ENCODING_CODES = {
    "reference": "REF",
    "certified_bg": "A",
    "demand_driven": "C",
    "shared_counter": "B",
    "direct_range_soft": "D",
    "optimized": "ABCD",
}
COMPACT_ENCODING_NAMES = {
    "reference": "Reference",
    "certified_bg": "CertifiedBG",
    "demand_driven": "DemandDriven",
    "shared_counter": "SharedCounter",
    "direct_range_soft": "DirectRangeSoft",
    "optimized": "OptimizedABCD",
}
COLLISION_AMO_CODES = {
    "pairwise": "PW",
    "adaptive_commander": "AC",
}
COLLISION_AMO_NAMES = {
    "pairwise": "Pairwise",
    "adaptive_commander": "AdaptiveCommander",
}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "instances_manifest.csv"


@dataclass(frozen=True)
class InstanceSpec:
    path: Path
    instance_name: str
    content_id: str
    sha256: str
    family: str
    variant: str
    has_precedence: bool
    source_alias_count: int
    source_alias_paths: str
    base_lineage_id: str = ""
    repository_alias_count: int = 1
    repository_alias_paths: str = ""
    dataset_source_page: str = DATASET_SOURCE_PAGE
    dataset_archive_url: str = DATASET_ARCHIVE_URL
    dataset_archive_sha256: str = ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark the B2B Boolean models and journal objective family "
            "over Full and/or Reduced meeting-slot domains."
        )
    )
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument("--instance", help="single .dzn instance")
    input_group.add_argument(
        "--data-dir",
        help="directory containing .dzn instances; contents are SHA-256 deduplicated",
    )
    input_group.add_argument(
        "--manifest",
        help=(
            "canonical instances_manifest.csv; defaults to the repository "
            "manifest when no other input is selected"
        ),
    )
    parser.add_argument(
        "--family",
        choices=["all", "original", "forbidden", "fixed", "precedence"],
        default="all",
        help="filter a manifest or data directory by benchmark family",
    )
    parser.add_argument(
        "--keep-path-aliases",
        action="store_true",
        help=(
            "with --data-dir, run every selected .dzn path even when multiple "
            "paths have identical SHA-256 content"
        ),
    )
    parser.add_argument(
        "--solver",
        choices=[*SOLVERS, "sat_all", "exact_all", "all"],
        default="sat_all",
        help=(
            "sat_all preserves the original SAT/MaxSAT benchmark; exact_all "
            "runs Gurobi MIP, CPLEX MIP, and CPLEX CP; all runs both groups"
        ),
    )
    parser.add_argument(
        "--maxsat-backend",
        choices=MAXSAT_BACKENDS,
        default="uwrmaxsat",
        help="UWrMaxSAT is required by default; RC2 is development-only",
    )
    parser.add_argument(
        "--uwrmaxsat-bin",
        help="pinned UWrMaxSAT executable; otherwise use UWRMAXSAT_BIN/local/PATH",
    )
    parser.add_argument(
        "--uwrmaxsat-sha256",
        help="optional expected SHA-256 of the pinned UWrMaxSAT executable",
    )
    parser.add_argument(
        "--sat-backend",
        choices=SAT_BACKENDS,
        default="cadical",
        help="CaDiCaL 1.5.3 is required by default; Glucose is development-only",
    )
    parser.add_argument(
        "--precedence-mode",
        choices=[*PRECEDENCE_MODES, "both"],
        help="deprecated composite alias; use the independent P/G flags",
    )
    parser.add_argument(
        "--precedence-encoding",
        choices=[*PRECEDENCE_ENCODINGS, "both"],
        help="P factor; defaults to both",
    )
    parser.add_argument(
        "--precedence-graph",
        choices=[*PRECEDENCE_GRAPHS, "both"],
        help="G factor; defaults to both",
    )
    parser.add_argument(
        "--domain-filter-graph",
        choices=[*DOMAIN_FILTER_GRAPHS, "both"],
        default="distance_closure",
        help=(
            "F factor for Reduced-domain preprocessing: direct=Filter-E, "
            "distance_closure=Filter-E* (default), both=both; Full is emitted "
            "once because filtering does not change its variables"
        ),
    )
    parser.add_argument(
        "--encoding-variant",
        choices=[*VARIANTS, "all"],
        default="imp12+",
        help="production defaults to IC12+; use all only for diagnostic ablation",
    )
    parser.add_argument(
        "--domain-mode",
        choices=[*DOMAIN_MODES, "both"],
        default="both",
        help="Full=MxT variables; Reduced=exact preprocessing fixpoint",
    )
    parser.add_argument(
        "--objective-mode",
        choices=OBJECTIVE_MODES,
        default="ir",
        help=(
            "ir preserves conference behavior; bg_d2, ir_is, ir_im_is, "
            "bg_ir_is, is, isq and im_is are journal Boolean-objective modes"
        ),
    )
    parser.add_argument(
        "--compact-encoding",
        choices=[*COMPACT_ENCODINGS, "all"],
        default="reference",
        help=(
            "objective-encoding ablation: reference, A=certified_bg, "
            "B=shared_counter, C=demand_driven, D=direct_range_soft, "
            "optimized=ABCD; D and optimized idle-range modes are "
            "MaxSAT-only"
        ),
    )
    parser.add_argument(
        "--collision-amo",
        choices=[*COLLISION_AMO_ENCODINGS, "all"],
        default="pairwise",
        help=(
            "participant-collision AMO factor: pairwise preserves the "
            "reference model; adaptive_commander uses pairwise below six "
            "literals and group-size-four commander AMO from six onward"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=7200.0,
        help="wall-clock timeout per run in seconds",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=1,
        help="worker/thread limit passed identically to commercial exact solvers",
    )
    parser.add_argument(
        "--random-seed",
        type=int,
        default=0,
        help="random seed passed identically to commercial exact solvers",
    )
    parser.add_argument(
        "--csv",
        default="benchmark_results.csv",
        help="aggregated benchmark CSV path",
    )
    parser.add_argument(
        "--long-csv",
        help="detailed CSV path; defaults to <csv-stem>_detailed.csv",
    )
    parser.add_argument(
        "--excel-dir",
        help=(
            "directory for one <instance>.xlsx workbook per instance; "
            "defaults to <csv-parent>/excel"
        ),
    )
    parser.add_argument(
        "--no-excel",
        action="store_true",
        help="skip per-instance workbooks (recommended for cell-level campaigns)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.threads <= 0:
        parser.error("--threads must be positive")
    if args.precedence_mode is not None and (
        args.precedence_encoding is not None or args.precedence_graph is not None
    ):
        parser.error(
            "--precedence-mode cannot be combined with independent P/G flags"
        )
    if args.keep_path_aliases and args.data_dir is None:
        parser.error("--keep-path-aliases requires --data-dir")
    if args.objective_mode != "ir" and args.solver in {
        *EXACT_SOLVERS,
        "exact_all",
        "all",
    }:
        parser.error(
            "commercial exact baselines currently support only "
            "--objective-mode ir"
        )
    if (
        args.solver in {*EXACT_SOLVERS, "exact_all"}
        and args.compact_encoding not in {"reference", "all"}
    ):
        parser.error("commercial exact baselines have no compact-encoding factor")
    if (
        args.solver in {*EXACT_SOLVERS, "exact_all"}
        and args.collision_amo not in {"pairwise", "all"}
    ):
        parser.error("commercial exact baselines have no collision-AMO factor")
    if (
        args.objective_mode in {"ir", "ir_is", "ir_im_is", "bg_ir_is"}
        and args.compact_encoding in {"direct_range_soft", "optimized"}
        and args.solver in {"incremental", "multiple"}
    ):
        parser.error(
            f"--compact-encoding {args.compact_encoding} is MaxSAT-only for "
            f"--objective-mode {args.objective_mode}"
        )
    if args.objective_mode == "isq" and args.solver != "maxsat":
        parser.error("--objective-mode isq requires --solver maxsat (weighted penalties)")
    return args


def selected(choice: str, values: list[str], all_name: str = "all") -> list[str]:
    return values if choice == all_name else [choice]


def selected_solvers(choice: str) -> list[str]:
    if choice == "all":
        return list(SOLVERS)
    if choice == "sat_all":
        return list(SAT_SOLVERS)
    if choice == "exact_all":
        return list(EXACT_SOLVERS)
    return [choice]


@dataclass(frozen=True)
class RunConfiguration:
    solver_name: str
    precedence_encoding: str
    precedence_graph: str
    domain_filter_graph: str
    encoding_variant: str
    domain_mode: str
    objective_mode: str = "ir"
    compact_encoding: str = "reference"
    collision_amo: str = "pairwise"


def precedence_configurations(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Return controlled P×G cells or the requested deprecated composites."""

    if args.precedence_mode is not None:
        legacy = {
            "traditional": ("pairwise", "direct"),
            "staircase": ("sparse_suffix", "distance_closure"),
        }
        modes = selected(args.precedence_mode, PRECEDENCE_MODES, "both")
        return [legacy[mode] for mode in modes]

    encodings = selected(
        args.precedence_encoding or "both",
        PRECEDENCE_ENCODINGS,
        "both",
    )
    graphs = selected(
        args.precedence_graph or "both",
        PRECEDENCE_GRAPHS,
        "both",
    )
    return [
        (precedence_encoding, precedence_graph)
        for precedence_encoding in encodings
        for precedence_graph in graphs
    ]


def legacy_precedence_mode(
    precedence_encoding: str,
    precedence_graph: str,
) -> str:
    if precedence_encoding in {"native_linear", "native_cp"}:
        return "exact_native"
    if (precedence_encoding, precedence_graph) == ("pairwise", "direct"):
        return "traditional"
    if (precedence_encoding, precedence_graph) == (
        "sparse_suffix",
        "distance_closure",
    ):
        return "staircase"
    return "factorial"


def configuration_metadata(
    *,
    solver_name: str,
    precedence_encoding: str,
    precedence_graph: str,
    encoding_variant: str,
    domain_mode: str,
    maxsat_backend: str,
    sat_backend: str,
    domain_filter_graph: str = "distance_closure",
    objective_mode: str = "ir",
    compact_encoding: str = "reference",
    collision_amo: str = "pairwise",
) -> dict[str, str]:
    """Return stable human and machine identifiers for one factor tuple."""

    if compact_encoding not in VALID_COMPACT_ENCODINGS:
        raise ValueError(f"Unknown compact_encoding={compact_encoding!r}")
    if collision_amo not in VALID_COLLISION_AMO_ENCODINGS:
        raise ValueError(f"Unknown collision_amo={collision_amo!r}")
    if solver_name in EXACT_SOLVERS:
        if compact_encoding != "reference":
            raise ValueError(
                f"{solver_name} has no compact_encoding={compact_encoding!r}"
            )
        if collision_amo != "pairwise":
            raise ValueError(
                f"{solver_name} has no collision_amo={collision_amo!r}"
            )
        if objective_mode != "ir":
            raise ValueError(
                f"{solver_name} does not implement objective_mode="
                f"{objective_mode!r}"
            )
        exact_configurations = {
            "gurobi_mip": (
                "GRB-MIP",
                "GurobiMIP",
                "native_linear",
                "PrefixSuffixSpan",
                "prefix_suffix_span_range",
            ),
            "cplex_mip": (
                "CPX-MIP",
                "CPLEXMIP",
                "native_linear",
                "PrefixSuffixSpan",
                "prefix_suffix_span_range",
            ),
            "cplex_cp": (
                "CPO-CP",
                "CPLEXCP",
                "native_cp",
                "NativeMinMaxSpan",
                "native_min_max_span_range",
            ),
        }
        (
            engine_code,
            optimization_engine,
            native_encoding,
            span_factor,
            idle_encoding,
        ) = exact_configurations[solver_name]
        label = f"R-DC-IRP-{engine_code}"
        identifier = "__".join(
            (
                "cfg3",
                "m-reduced",
                f"p-{native_encoding}",
                "g-distance_closure",
                f"b-{idle_encoding}",
                "o-idle_range_pstar",
                f"s-{solver_name}",
                "i-na",
            )
        )
        return {
            "configuration_label": label,
            "configuration_id": identifier,
            "configuration_key": identifier,
            "optimization_engine": optimization_engine,
            "idle_encoding": idle_encoding,
            "domain_filter_graph": "distance_closure",
            "objective_code": "IRP",
            "compact_encoding": "reference",
            "collision_amo": "pairwise",
            "implied_constraints_code": "NA",
            "factor_m": "Reduced",
            "factor_f": "Filter-E*",
            "factor_p": (
                "NativeLinear"
                if native_encoding == "native_linear"
                else "NativeCP"
            ),
            "factor_g": "DistanceClosure-E*",
            "factor_b": span_factor,
            "factor_o": "IdleRangePstar",
            "factor_s": optimization_engine,
            "factor_i": "N/A",
            "factor_c": "Reference",
            "factor_amo": "Pairwise",
        }

    if solver_name == "maxsat":
        engine_code = "UW" if maxsat_backend == "uwrmaxsat" else "RC2"
        optimization_engine = (
            "UWrMaxSAT" if maxsat_backend == "uwrmaxsat" else "RC2"
        )
        backend_code = maxsat_backend
    elif solver_name == "multiple":
        engine_code = f"NIS-{SAT_BACKEND_CODES[sat_backend]}"
        optimization_engine = "NonIncrementalSAT"
        backend_code = sat_backend
    elif solver_name == "incremental":
        engine_code = f"IS-{SAT_BACKEND_CODES[sat_backend]}"
        optimization_engine = "IncrementalSAT"
        backend_code = sat_backend
    else:
        raise ValueError(f"Unknown solver: {solver_name}")

    if objective_mode not in VALID_OBJECTIVE_MODES:
        raise ValueError(f"Unknown objective_mode={objective_mode!r}")
    domain_code = DOMAIN_CODES[domain_mode]
    encoding_code = PRECEDENCE_ENCODING_CODES[precedence_encoding]
    graph_code = PRECEDENCE_GRAPH_CODES[precedence_graph]
    implied_code = VARIANT_CODES[encoding_variant]
    label_parts = [domain_code]
    if domain_filter_graph == "direct":
        label_parts.append("FE")
    label_parts.extend(
        (
            encoding_code,
            graph_code,
            "ST",
            OBJECTIVE_CODES[objective_mode],
            engine_code,
            implied_code,
        )
    )
    label = "-".join(label_parts)
    if compact_encoding != "reference":
        label = f"{label}-C{COMPACT_ENCODING_CODES[compact_encoding]}"
    if collision_amo != "pairwise":
        label = f"{label}-FAMO{COLLISION_AMO_CODES[collision_amo]}"
    optimization_procedure_display_name = {
        "maxsat": "one-shot weighted MaxSAT",
        "multiple": "restart-based multi-phase SAT",
        "incremental": (
            "incremental multi-phase SAT with persistent solver state"
        ),
    }[solver_name]
    domain_display_name = "Full" if domain_mode == "full" else "Reduced"
    identifier_parts = [
        (
            "cfg2" if domain_filter_graph == "distance_closure" else "cfg4"
        )
        if objective_mode == "ir"
        else "cfg5",
        f"m-{domain_mode}",
    ]
    if domain_filter_graph == "direct":
        identifier_parts.append("f-direct")
    identifier_parts.extend(
        (
            f"p-{precedence_encoding}",
            f"g-{precedence_graph}",
            "b-span_threshold",
            f"o-{OBJECTIVE_KEYS[objective_mode]}",
            f"s-{optimization_engine.lower()}",
            f"i-{encoding_variant.replace('+', 'plus')}",
            f"backend-{backend_code.lower()}",
        )
    )
    if compact_encoding != "reference":
        identifier_parts.append(f"c-{compact_encoding}")
    if collision_amo != "pairwise":
        identifier_parts.append(f"amo-{collision_amo}")
    identifier = "__".join(identifier_parts)
    return {
        "configuration_label": label,
        "configuration_id": identifier,
        "configuration_key": identifier,
        "model_family_display_name": "Compact",
        "model_configuration_display_name": (
            f"Compact/{domain_display_name}"
        ),
        "implementation_provenance": "project_implementation",
        "optimization_procedure_display_name": (
            optimization_procedure_display_name
        ),
        "optimization_engine": optimization_engine,
        "idle_encoding": "span_threshold",
        "domain_filter_graph": domain_filter_graph,
        "objective_code": OBJECTIVE_CODES[objective_mode],
        "compact_encoding": compact_encoding,
        "collision_amo": collision_amo,
        "implied_constraints_code": implied_code,
        "factor_m": "Full" if domain_mode == "full" else "Reduced",
        "factor_f": (
            "Filter-E"
            if domain_filter_graph == "direct"
            else "Filter-E*"
        ),
        "factor_p": (
            "Pairwise"
            if precedence_encoding == "pairwise"
            else "SparseSuffix"
        ),
        "factor_g": (
            "Direct-E"
            if precedence_graph == "direct"
            else "DistanceClosure-E*"
        ),
        "factor_b": "SpanThreshold",
        "factor_o": OBJECTIVE_NAMES[objective_mode],
        "factor_s": optimization_engine,
        "factor_i": VARIANT_FACTOR_NAMES[encoding_variant],
        "factor_c": COMPACT_ENCODING_NAMES[compact_encoding],
        "factor_amo": COLLISION_AMO_NAMES[collision_amo],
    }


def _instance_spec_from_paths(paths: list[Path]) -> InstanceSpec:
    canonical = sorted(paths)[0]
    digest = file_sha256(canonical)
    family_values = sorted({classify_instance_name(path.name)[0] for path in paths})
    variant_values = sorted({classify_instance_name(path.name)[1] for path in paths})
    parsed = read_instance(canonical)
    aliases = " | ".join(path.as_posix() for path in sorted(paths))
    return InstanceSpec(
        path=canonical,
        instance_name=canonical.stem,
        content_id=f"b2b-{digest[:16]}",
        base_lineage_id=base_lineage_id(canonical.name),
        sha256=digest,
        family="|".join(family_values),
        variant="|".join(variant_values),
        has_precedence=any(parsed.precedences),
        source_alias_count=len(paths),
        source_alias_paths=aliases,
        repository_alias_count=len(paths),
        repository_alias_paths=aliases,
    )


def _manifest_instances(path: Path, family: str) -> list[InstanceSpec]:
    if not path.is_file():
        raise FileNotFoundError(path)
    instances: list[InstanceSpec] = []
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            row_families = row.get("family", "unknown").split("|")
            if family != "all" and family not in row_families:
                continue
            run_path = Path(row["canonical_run_path"])
            if not run_path.is_absolute():
                run_path = path.parent / run_path
            if not run_path.is_file():
                raise FileNotFoundError(
                    f"Manifest run path does not exist: {run_path}"
                )
            instances.append(
                InstanceSpec(
                    path=run_path,
                    instance_name=row["canonical_instance"],
                    content_id=row["content_id"],
                    base_lineage_id=(
                        row.get("base_lineage_id")
                        or base_lineage_id(row["canonical_instance"])
                    ),
                    sha256=row["sha256"],
                    family=row["family"],
                    variant=row["variant"],
                    has_precedence=int(row["n_direct_precedence_edges"]) > 0,
                    source_alias_count=int(row["source_alias_count"]),
                    source_alias_paths=row["source_alias_paths"],
                    repository_alias_count=int(row["repository_alias_count"]),
                    repository_alias_paths=row["repository_alias_paths"],
                    dataset_source_page=row["dataset_source_page"],
                    dataset_archive_url=row["dataset_archive_url"],
                    dataset_archive_sha256=row["dataset_archive_sha256"],
                )
            )
    if not instances:
        raise FileNotFoundError(
            f"No canonical instances for family={family!r} in {path}"
        )
    return instances


def collect_instances(
    instance: str | None,
    data_dir: str | None,
    manifest: str | None = None,
    family: str = "all",
    keep_path_aliases: bool = False,
) -> list[InstanceSpec]:
    if instance:
        path = Path(instance)
        if not path.is_file():
            raise FileNotFoundError(path)
        spec = _instance_spec_from_paths([path])
        if family != "all" and family not in spec.family.split("|"):
            raise FileNotFoundError(
                f"Instance {path} does not belong to family={family!r}"
            )
        return [spec]

    if manifest:
        return _manifest_instances(Path(manifest), family)

    if data_dir is None:
        return _manifest_instances(DEFAULT_MANIFEST, family)

    directory = Path(data_dir)
    if not directory.is_dir():
        raise FileNotFoundError(directory)
    paths = sorted(directory.glob("*.dzn"))
    if family != "all":
        paths = [
            path
            for path in paths
            if classify_instance_name(path.name)[0] == family
        ]
    if not paths:
        raise FileNotFoundError(f"No .dzn files found in {directory}")
    if keep_path_aliases:
        return [_instance_spec_from_paths([path]) for path in paths]
    by_hash: dict[str, list[Path]] = {}
    for path in paths:
        by_hash.setdefault(file_sha256(path), []).append(path)
    return [
        _instance_spec_from_paths(group)
        for _, group in sorted(by_hash.items())
    ]


def instance_precedence_configurations(
    args: argparse.Namespace,
    instance: InstanceSpec,
) -> list[tuple[str, str]]:
    explicit = any(
        value is not None
        for value in (
            args.precedence_mode,
            args.precedence_encoding,
            args.precedence_graph,
        )
    )
    if not explicit and not instance.has_precedence:
        return [("pairwise", "direct")]
    return precedence_configurations(args)


def instance_domain_filter_configurations(
    args: argparse.Namespace,
    instance: InstanceSpec,
    domain_mode: str,
) -> list[str]:
    """Return meaningful F cells without duplicating unchanged Full models."""

    if domain_mode == "full" or not instance.has_precedence:
        return ["distance_closure"]
    return selected(
        getattr(args, "domain_filter_graph", "distance_closure"),
        DOMAIN_FILTER_GRAPHS,
        "both",
    )


def solver_compact_configurations(
    args: argparse.Namespace,
    solver_name: str,
) -> list[str]:
    """Return non-duplicating, backend-compatible compact ablation cells."""

    if solver_name in EXACT_SOLVERS:
        return ["reference"]
    candidates = selected(args.compact_encoding, COMPACT_ENCODINGS)
    if args.objective_mode == "bg_d2":
        # D is an idle-range MaxSAT refinement and has no effect on BG-d2.
        return [value for value in candidates if value != "direct_range_soft"]
    if solver_name != "maxsat" and args.objective_mode in {
        "ir",
        "ir_is",
        "ir_im_is",
        "bg_ir_is",
    }:
        # These presets activate non-unit soft clauses for idle range, which
        # the cardinality-bound SAT procedures intentionally do not consume.
        return [
            value
            for value in candidates
            if value not in {"direct_range_soft", "optimized"}
        ]
    return candidates


def solver_collision_amo_configurations(
    args: argparse.Namespace,
    solver_name: str,
) -> list[str]:
    """Return collision-AMO cells supported by the selected formulation."""

    if solver_name in EXACT_SOLVERS:
        return ["pairwise"]
    return selected(
        getattr(args, "collision_amo", "pairwise"),
        COLLISION_AMO_ENCODINGS,
    )


def benchmark_configurations(
    args: argparse.Namespace,
    instance: InstanceSpec,
    solvers: list[str],
) -> list[RunConfiguration]:
    """Expand SAT factors and add each exact baseline exactly once."""

    configurations: list[RunConfiguration] = []
    selected_sat_solvers = [
        solver for solver in solvers if solver in SAT_SOLVERS
    ]
    if selected_sat_solvers:
        variants = selected(args.encoding_variant, VARIANTS)
        domain_modes = selected(args.domain_mode, DOMAIN_MODES, "both")
        precedence_cells = instance_precedence_configurations(args, instance)
        configurations.extend(
            RunConfiguration(
                solver_name=solver,
                precedence_encoding=precedence_encoding,
                precedence_graph=precedence_graph,
                domain_filter_graph=domain_filter_graph,
                encoding_variant=variant,
                domain_mode=domain_mode,
                objective_mode=args.objective_mode,
                compact_encoding=compact_encoding,
                collision_amo=collision_amo,
            )
            for domain_mode in domain_modes
            for domain_filter_graph in instance_domain_filter_configurations(
                args,
                instance,
                domain_mode,
            )
            for precedence_encoding, precedence_graph in precedence_cells
            for solver in selected_sat_solvers
            for compact_encoding in solver_compact_configurations(args, solver)
            for collision_amo in solver_collision_amo_configurations(args, solver)
            for variant in variants
        )

    for solver in solvers:
        if solver not in EXACT_SOLVERS:
            continue
        configurations.append(
            RunConfiguration(
                solver_name=solver,
                precedence_encoding=(
                    "native_cp" if solver == "cplex_cp" else "native_linear"
                ),
                precedence_graph="distance_closure",
                domain_filter_graph="distance_closure",
                encoding_variant="n/a",
                domain_mode="reduced",
                objective_mode="ir",
                compact_encoding="reference",
                collision_amo="pairwise",
            )
        )
    return configurations


def require_solver_environment(
    solvers: list[str],
    *,
    maxsat_backend: str,
    uwrmaxsat_bin: str | None,
    uwrmaxsat_sha256: str | None,
    sat_backend: str,
) -> None:
    """Validate all requested production backends before the first run."""

    if "maxsat" in solvers and maxsat_backend == "uwrmaxsat":
        binary = resolve_uwrmaxsat_binary(uwrmaxsat_bin)
        if binary is None:
            raise FileNotFoundError(UWRMAXSAT_NOT_FOUND_MESSAGE)
        actual_sha256 = executable_sha256(binary)
        expected_sha256 = (uwrmaxsat_sha256 or "").strip().lower()
        if expected_sha256 and (
            len(expected_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_sha256)
        ):
            raise ValueError(
                "--uwrmaxsat-sha256 must be a 64-character hex digest"
            )
        if expected_sha256 and actual_sha256 != expected_sha256:
            raise RuntimeError(
                "UWrMaxSAT executable SHA-256 mismatch: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )

    if any(solver in {"incremental", "multiple"} for solver in solvers):
        require_sat_backend(sat_backend)

    optional_modules = {
        "gurobi_mip": (
            "gurobipy",
            "Gurobi MIP requires 'gurobipy' and a valid Gurobi license",
        ),
        "cplex_mip": (
            "docplex.mp.model",
            "CPLEX MIP requires 'docplex' and a CPLEX runtime/license",
        ),
        "cplex_cp": (
            "docplex.cp.model",
            "CPLEX CP requires 'docplex' and a CP Optimizer runtime/license",
        ),
    }
    for solver in solvers:
        if solver not in optional_modules:
            continue
        module_name, message = optional_modules[solver]
        try:
            available = importlib.util.find_spec(module_name) is not None
        except (ImportError, ModuleNotFoundError, AttributeError):
            available = False
        if not available:
            raise RuntimeError(
                f"{message}. The runner never substitutes another solver."
            )


def status_to_sat_result(status: str | None) -> str:
    normalized = (status or "ERROR").upper()
    if normalized in {
        "OPTIMAL",
        "FEASIBLE",
        "SAT",
        "SATISFIABLE",
        "OPTIMUM FOUND",
    }:
        return "SAT"
    if normalized in {"UNSAT", "UNSATISFIABLE"}:
        return "UNSAT"
    if normalized == "TIMEOUT":
        return "TIMEOUT"
    return "ERROR"


def serialize_list(values: list[int] | tuple[int, ...] | None) -> str:
    return "" if values is None else ",".join(str(value) for value in values)


def serialize_assignment(values: list[int] | None) -> str:
    if values is None:
        return ""
    return ",".join(str(value + 1) if value >= 0 else "-" for value in values)


def serialize_schedule(meetings_per_slot: list[list[int]] | None) -> str:
    if meetings_per_slot is None:
        return ""
    return " | ".join(
        f"{slot}:" + " ".join(f"M{meeting + 1}" for meeting in meetings)
        for slot, meetings in enumerate(meetings_per_slot, start=1)
    )


def _solver_class(solver_name: str):
    if solver_name == "incremental":
        return B2BIncrementalSATSolver
    if solver_name == "multiple":
        return B2BMultipleSATSolver
    if solver_name == "maxsat":
        return B2BMaxSATSolver
    if solver_name == "gurobi_mip":
        return B2BGurobiMIPSolver
    if solver_name == "cplex_mip":
        return B2BCPLEXMIPSolver
    if solver_name == "cplex_cp":
        return B2BCPLEXCPSolver
    raise ValueError(f"Unknown solver: {solver_name}")


def _formula_metadata(
    solver_name: str,
    solver_object: Any,
    *,
    input_parsing_seconds: float,
    model_construction_seconds: float,
    model_build_seconds: float,
) -> dict[str, Any]:
    artifacts = solver_object.artifacts
    if getattr(artifacts, "formalism", "CNF") != "CNF":
        return {
            "message_type": "metadata",
            "formalism": artifacts.formalism,
            "model_family": artifacts.model_family,
            "formulation_name": artifacts.formulation_name,
            "objective": artifacts.objective_name,
            "objective_participant_count": len(
                artifacts.objective_participants
            ),
            "objective_participants": serialize_list(
                tuple(
                    participant + 1
                    for participant in artifacts.objective_participants
                )
            ),
            "objective_encoding": artifacts.objective_encoding,
            "precedence_mode": artifacts.precedence_mode,
            "precedence_encoding": artifacts.precedence_encoding,
            "precedence_graph": artifacts.precedence_graph,
            "precedence_configuration": (
                artifacts.precedence_configuration
            ),
            "domain_mode": artifacts.domain_mode,
            "domain_filter_graph": getattr(
                artifacts,
                "domain_filter_graph",
                "distance_closure",
            ),
            "domain_filter_iterations": getattr(
                artifacts,
                "domain_filter_iterations",
                None,
            ),
            "domain_filter_seconds": getattr(
                artifacts,
                "domain_filter_seconds",
                None,
            ),
            "full_schedule_candidates": (
                artifacts.full_schedule_candidates
            ),
            "unary_eligible_schedule_candidates": (
                artifacts.unary_eligible_schedule_candidates
            ),
            "initial_schedule_candidates": (
                artifacts.initial_schedule_candidates
            ),
            "reduced_schedule_candidates": (
                artifacts.reduced_schedule_candidates
            ),
            "active_schedule_candidates": (
                artifacts.active_schedule_candidates
            ),
            "unary_removed_schedule_candidates": (
                artifacts.unary_removed_schedule_candidates
            ),
            "preprocessing_removed_schedule_candidates": (
                artifacts.preprocessing_removed_schedule_candidates
            ),
            "removed_schedule_candidates": (
                artifacts.removed_schedule_candidates
            ),
            "n_vars": artifacts.n_vars,
            "n_primary_variables": artifacts.n_primary_variables,
            "n_auxiliary_variables": artifacts.n_auxiliary_variables,
            "n_binary_variables": artifacts.n_binary_variables,
            "n_integer_variables": artifacts.n_integer_variables,
            "n_continuous_variables": artifacts.n_continuous_variables,
            "n_linear_constraints": artifacts.n_linear_constraints,
            "n_global_constraints": artifacts.n_global_constraints,
            "n_nonzeros": artifacts.n_nonzeros,
            "n_hard_clauses": None,
            "n_soft_clauses": None,
            "n_total_clauses": None,
            "n_hard_literals": None,
            "n_soft_literals": None,
            "n_total_literals": None,
            "n_objective_lits": 0,
            "formula_scope": FORMULA_SCOPE,
            "input_parsing_seconds": round(input_parsing_seconds, 6),
            "model_construction_seconds": round(
                model_construction_seconds,
                6,
            ),
            "model_build_seconds": round(model_build_seconds, 6),
            "precedence_direct_edges": (
                artifacts.precedence_direct_edges
            ),
            "precedence_closure_edges": (
                artifacts.precedence_transitive_edges
            ),
            "precedence_max_distance": (
                artifacts.precedence_max_distance
            ),
            "precedence_relation_edges": (
                artifacts.precedence_relation_edges
            ),
            "enabled_constraints": " | ".join(
                artifacts.enabled_constraints
            ),
            "solver_backend": getattr(
                solver_object,
                "solver_backend",
                "",
            ),
            "solver_version": getattr(
                solver_object,
                "solver_version",
                "",
            ),
            "solver_binary": getattr(
                solver_object,
                "solver_binary",
                "",
            ),
            "solver_command": getattr(
                solver_object,
                "solver_command",
                "",
            ),
            "maxsat_backend_preference": "",
            "sat_backend_preference": "",
            "threads": getattr(solver_object, "threads", None),
            "random_seed": getattr(
                solver_object,
                "random_seed",
                None,
            ),
        }

    objective_literal_count = sum(
        len(tier.maxsat_clauses) for tier in artifacts.objective_tiers
    )
    soft_clauses = (
        [
            clause
            for tier in artifacts.objective_tiers
            for clause in tier.maxsat_clauses
        ]
        if solver_name == "maxsat"
        else []
    )
    n_soft = len(soft_clauses)
    n_soft_literals = sum(len(clause) for clause in soft_clauses)
    soft_weight_sum = (
        sum(
            sum(tier.maxsat_weights) * tier.scalar_weight
            for tier in artifacts.objective_tiers
        )
        if solver_name == "maxsat"
        else 0
    )
    return {
        "message_type": "metadata",
        "objective": artifacts.objective_name,
        "objective_mode": artifacts.objective_mode,
        "compact_encoding": artifacts.compact_encoding,
        "compact_encoding_features": serialize_list(
            artifacts.compact_encoding_features
        ),
        "zero_break_certificate_participant": (
            None
            if artifacts.zero_break_certificate_participant is None
            else artifacts.zero_break_certificate_participant + 1
        ),
        "zero_break_certificate_reason": (
            artifacts.zero_break_certificate_reason
        ),
        "zero_break_branch": artifacts.zero_break_branch,
        "occupancy_alias_count": artifacts.occupancy_alias_count,
        "prefix_alias_count": artifacts.prefix_alias_count,
        "suffix_alias_count": artifacts.suffix_alias_count,
        "first_alias_count": artifacts.first_alias_count,
        "shared_counter_state_count": artifacts.shared_counter_state_count,
        "direct_range_soft_clause_count": (
            artifacts.direct_range_soft_clause_count
        ),
        "collision_amo": artifacts.collision_amo_encoding,
        "collision_amo_cutoff": artifacts.collision_amo_cutoff,
        "collision_amo_commander_group_size": (
            artifacts.collision_amo_commander_group_size
        ),
        "collision_amo_pairwise_group_count": (
            artifacts.collision_amo_pairwise_group_count
        ),
        "collision_amo_commander_group_count": (
            artifacts.collision_amo_commander_group_count
        ),
        "collision_amo_commander_variable_count": (
            artifacts.collision_amo_commander_variable_count
        ),
        "collision_amo_clause_count": artifacts.collision_amo_clause_count,
        "collision_amo_max_group_size": (
            artifacts.collision_amo_max_group_size
        ),
        "objective_tier_weights": serialize_list(
            tuple(tier.scalar_weight for tier in artifacts.objective_tiers)
        ),
        "objective_participant_count": len(artifacts.objective_participants),
        "objective_participants": serialize_list(
            tuple(participant + 1 for participant in artifacts.objective_participants)
        ),
        "precedence_mode": artifacts.precedence_mode,
        "precedence_encoding": artifacts.precedence_encoding,
        "precedence_graph": artifacts.precedence_graph,
        "precedence_configuration": artifacts.precedence_configuration,
        "domain_mode": artifacts.domain_mode,
        "domain_filter_graph": artifacts.domain_filter_graph,
        "domain_filter_iterations": artifacts.domain_filter_iterations,
        "domain_filter_seconds": round(artifacts.domain_filter_seconds, 6),
        "full_schedule_candidates": artifacts.full_schedule_candidates,
        "unary_eligible_schedule_candidates": (
            artifacts.unary_eligible_schedule_candidates
        ),
        "initial_schedule_candidates": artifacts.initial_schedule_candidates,
        "reduced_schedule_candidates": artifacts.reduced_schedule_candidates,
        "active_schedule_candidates": artifacts.active_schedule_candidates,
        "unary_removed_schedule_candidates": (
            artifacts.unary_removed_schedule_candidates
        ),
        "preprocessing_removed_schedule_candidates": (
            artifacts.preprocessing_removed_schedule_candidates
        ),
        # Backward-compatible alias: removals made by exact preprocessing.
        "removed_schedule_candidates": artifacts.removed_schedule_candidates,
        "n_vars": artifacts.n_vars,
        "n_primary_variables": artifacts.n_primary_variables,
        "n_auxiliary_variables": artifacts.n_auxiliary_variables,
        "n_hard_clauses": artifacts.n_clauses,
        "n_soft_clauses": n_soft,
        "n_total_clauses": artifacts.n_clauses + n_soft,
        "n_hard_literals": artifacts.n_hard_literals,
        "n_soft_literals": n_soft_literals,
        "n_total_literals": artifacts.n_hard_literals + n_soft_literals,
        "max_hard_clause_length": artifacts.max_hard_clause_length,
        "max_soft_clause_length": max(
            (len(clause) for clause in soft_clauses),
            default=0,
        ),
        "n_unit_hard_clauses": artifacts.n_unit_hard_clauses,
        "n_binary_hard_clauses": artifacts.n_binary_hard_clauses,
        "n_ternary_hard_clauses": artifacts.n_ternary_hard_clauses,
        "n_long_hard_clauses": artifacts.n_long_hard_clauses,
        "soft_clause_weight": (
            artifacts.objective_tiers[0].scalar_weight
            if n_soft and len(artifacts.objective_tiers) == 1
            and not artifacts.objective_tiers[0].penalty_weights
            else None
        ),
        "max_soft_weight_bits": max(
            (int(tier.scalar_weight * weight).bit_length()
             for tier in artifacts.objective_tiers for weight in tier.maxsat_weights),
            default=0,
        ) if solver_name == "maxsat" else 0,
        "soft_weight_sum": soft_weight_sum,
        "formula_scope": FORMULA_SCOPE,
        "input_parsing_seconds": round(input_parsing_seconds, 6),
        "model_construction_seconds": round(model_construction_seconds, 6),
        "model_build_seconds": round(model_build_seconds, 6),
        "n_objective_lits": objective_literal_count,
        "precedence_direct_edges": artifacts.precedence_direct_edges,
        "precedence_closure_edges": artifacts.precedence_transitive_edges,
        "precedence_max_distance": artifacts.precedence_max_distance,
        "precedence_relation_edges": artifacts.precedence_relation_edges,
        "precedence_pairwise_clauses": artifacts.precedence_pairwise_clauses,
        "precedence_sparse_link_clauses": (
            artifacts.precedence_sparse_link_clauses
        ),
        "precedence_unique_suffix_cuts": (
            artifacts.precedence_unique_suffix_cuts
        ),
        "enabled_constraints": " | ".join(artifacts.enabled_constraints),
        "solver_backend": getattr(solver_object, "solver_backend", ""),
        "solver_version": getattr(solver_object, "solver_version", ""),
        "solver_binary": getattr(solver_object, "solver_binary", ""),
        "solver_command": getattr(solver_object, "solver_command", ""),
        "maxsat_backend_preference": getattr(solver_object, "backend", ""),
        "sat_backend_preference": getattr(solver_object, "solver_name", ""),
        "resolved_uwrmaxsat_bin": str(
            getattr(solver_object, "resolved_uwrmaxsat_bin", "") or ""
        ),
        "solver_binary_sha256": getattr(
            solver_object, "uwrmaxsat_binary_sha256", ""
        ),
    }


def _result_payload(
    result: dict[str, Any],
    *,
    solver_name: str,
    precedence_encoding: str,
    precedence_graph: str,
    domain_filter_graph: str,
    encoding_variant: str,
    domain_mode: str,
    runtime_seconds: float,
    model_build_seconds: float,
    solve_and_validate_seconds: float,
) -> dict[str, Any]:
    stats = result.get("stats")
    status = result.get("status", "ERROR")
    runtime_censored = str(status).upper() == "TIMEOUT"
    payload = {
        "message_type": "result",
        "status": status,
        "solver": solver_name,
        "solver_backend": result.get("solver_backend", result.get("solver", "")),
        "solver_binary": result.get("solver_binary", ""),
        "solver_binary_sha256": result.get("solver_binary_sha256", ""),
        "solver_version": result.get("solver_version", ""),
        "solver_command": result.get("solver_command", ""),
        "solver_message": result.get("solver_message", ""),
        "maxsat_backend_preference": result.get("maxsat_backend_preference", ""),
        "sat_backend_preference": result.get("sat_backend_preference", ""),
        "precedence_mode": result.get(
            "precedence_mode",
            legacy_precedence_mode(precedence_encoding, precedence_graph),
        ),
        "precedence_encoding": precedence_encoding,
        "precedence_graph": precedence_graph,
        "precedence_configuration": (
            f"{precedence_encoding}+{precedence_graph}"
        ),
        "domain_filter_graph": domain_filter_graph,
        "encoding_variant": encoding_variant,
        "domain_mode": domain_mode,
        "runtime_seconds": round(runtime_seconds, 6),
        "model_build_seconds": round(model_build_seconds, 6),
        "solve_and_validate_seconds": round(solve_and_validate_seconds, 6),
        "runtime_scope": RUNTIME_SCOPE,
        "runtime_censored": runtime_censored,
        "formula_scope": FORMULA_SCOPE,
        "formalism": result.get("formalism"),
        "model_family": result.get("model_family"),
        "formulation_name": result.get("formulation_name"),
        "objective": result.get("objective", "internal_idle_slot_range_pstar"),
        "objective_mode": result.get("objective_mode", "ir"),
        "compact_encoding": result.get("compact_encoding", "reference"),
        "compact_encoding_features": serialize_list(
            result.get("compact_encoding_features")
        ),
        "zero_break_certificate_participant": result.get(
            "zero_break_certificate_participant"
        ),
        "zero_break_certificate_reason": result.get(
            "zero_break_certificate_reason", ""
        ),
        "zero_break_branch": result.get("zero_break_branch", "not_applicable"),
        "occupancy_alias_count": result.get("occupancy_alias_count", 0),
        "prefix_alias_count": result.get("prefix_alias_count", 0),
        "suffix_alias_count": result.get("suffix_alias_count", 0),
        "first_alias_count": result.get("first_alias_count", 0),
        "shared_counter_state_count": result.get(
            "shared_counter_state_count", 0
        ),
        "direct_range_soft_clause_count": result.get(
            "direct_range_soft_clause_count", 0
        ),
        "collision_amo": result.get(
            "collision_amo_encoding",
            result.get("collision_amo", "pairwise"),
        ),
        "collision_amo_cutoff": result.get("collision_amo_cutoff", 6),
        "collision_amo_commander_group_size": result.get(
            "collision_amo_commander_group_size", 4
        ),
        "collision_amo_pairwise_group_count": result.get(
            "collision_amo_pairwise_group_count", 0
        ),
        "collision_amo_commander_group_count": result.get(
            "collision_amo_commander_group_count", 0
        ),
        "collision_amo_commander_variable_count": result.get(
            "collision_amo_commander_variable_count", 0
        ),
        "collision_amo_clause_count": result.get(
            "collision_amo_clause_count", 0
        ),
        "collision_amo_max_group_size": result.get(
            "collision_amo_max_group_size", 0
        ),
        "objective_vector": serialize_list(result.get("objective_vector")),
        "objective_value": result.get("objective_value"),
        "best_value": result.get("objective_value"),
        "best_bound": result.get("best_bound"),
        "optimality_gap": result.get("optimality_gap"),
        "proven_optimum": result.get("proven_optimum"),
        "proven_objective_vector": serialize_list(
            result.get("proven_objective_vector")
        ),
        "primary_objective_value": result.get("primary_objective_value"),
        "secondary_objective_value": result.get("secondary_objective_value"),
        "tertiary_objective_value": result.get("tertiary_objective_value"),
        "lexicographic_scalar_cost": result.get("lexicographic_scalar_cost"),
        "objective_tier_weights": serialize_list(
            result.get("objective_tier_weights")
        ),
        "objective_participant_count": result.get(
            "objective_participant_count"
        ),
        "objective_participants": serialize_list(
            result.get("objective_participants")
        ),
        "idle_range_pstar": None if stats is None else stats.idle_range,
        "all_participant_idle_range": (
            None if stats is None else stats.all_participant_idle_range
        ),
        "total_internal_idle_slots": (
            None if stats is None else stats.total_internal_idle_slots
        ),
        "maximum_internal_idle_slots": (
            None if stats is None else stats.maximum_internal_idle_slots
        ),
        "squared_internal_idle_slots": (
            None if stats is None else stats.squared_internal_idle_slots
        ),
        "total_break_groups": (
            None if stats is None else stats.total_break_groups
        ),
        "break_group_range": (
            None if stats is None else stats.break_group_range
        ),
        "participant_break_groups": (
            ""
            if stats is None
            else serialize_list(stats.participant_break_groups)
        ),
        "participant_internal_idle_slots": (
            ""
            if stats is None
            else serialize_list(stats.participant_internal_idle_slots)
        ),
        "busy_participants_per_slot": (
            "" if stats is None else serialize_list(stats.busy_participants_per_slot)
        ),
        "assignment": serialize_assignment(result.get("assignment")),
        "schedule_by_slot": (
            "" if stats is None else serialize_schedule(stats.meetings_per_slot)
        ),
        "validation_errors": "; ".join(result.get("validation_errors", [])),
        "error_type": "",
        "error_message": "",
        "n_optimizer_calls": result.get("n_optimizer_calls"),
        "n_bound_encodings": result.get("n_bound_encodings"),
        "optimizer_added_variables_peak": result.get(
            "optimizer_added_variables_peak"
        ),
        "optimizer_added_clauses_peak": result.get(
            "optimizer_added_clauses_peak"
        ),
        "optimizer_added_literals_peak": result.get(
            "optimizer_added_literals_peak"
        ),
        "optimizer_added_clauses_cumulative": result.get(
            "optimizer_added_clauses_cumulative"
        ),
        "objective_phase_seconds": serialize_list(
            result.get("objective_phase_seconds")
        ),
        "objective_phase_calls": serialize_list(
            result.get("objective_phase_calls")
        ),
        "branch_and_bound_nodes": result.get("branch_and_bound_nodes"),
        "cp_branches": result.get("cp_branches"),
        "cp_fails": result.get("cp_fails"),
        "backend_model_construction_seconds": result.get(
            "backend_model_construction_seconds"
        ),
    }
    payload["sat_result"] = status_to_sat_result(payload["status"])
    return payload


def _worker(
    solver_name: str,
    instance_path: str,
    precedence_encoding: str,
    precedence_graph: str,
    domain_filter_graph: str,
    encoding_variant: str,
    domain_mode: str,
    objective_mode: str,
    compact_encoding: str,
    collision_amo: str,
    maxsat_backend: str,
    uwrmaxsat_bin: str | None,
    uwrmaxsat_sha256: str | None,
    sat_backend: str,
    solver_timeout: float,
    threads: int,
    random_seed: int,
    verbose: bool,
    output: mp.Queue[Any],
) -> None:
    started = time.perf_counter()
    try:
        instance = read_instance(instance_path)
        input_ready = time.perf_counter()
        if solver_name in EXACT_SOLVERS:
            solver_kwargs: dict[str, Any] = {
                "instance_or_path": instance,
                "domain_mode": "reduced",
                "solver_timeout": solver_timeout,
                "threads": threads,
                "random_seed": random_seed,
            }
        else:
            solver_kwargs = {
                "instance_or_path": instance,
                "precedence_encoding": precedence_encoding,
                "precedence_graph": precedence_graph,
                "encoding_variant": encoding_variant,
                "domain_mode": domain_mode,
                "domain_filter_graph": domain_filter_graph,
                "objective_mode": objective_mode,
                "compact_encoding": compact_encoding,
                "collision_amo_encoding": collision_amo,
            }
        if solver_name == "maxsat":
            solver_kwargs.update(
                backend=maxsat_backend,
                uwrmaxsat_bin=uwrmaxsat_bin,
                uwrmaxsat_sha256=uwrmaxsat_sha256,
                uwrmaxsat_timeout=solver_timeout,
            )
        elif solver_name in {"incremental", "multiple"}:
            solver_kwargs["solver_name"] = sat_backend
        solver_object = _solver_class(solver_name)(**solver_kwargs)
        model_ready = time.perf_counter()
        input_parsing_seconds = input_ready - started
        model_construction_seconds = model_ready - input_ready
        model_build_seconds = model_ready - started
        if solver_name == "maxsat" and maxsat_backend == "uwrmaxsat":
            solver_object.uwrmaxsat_timeout = max(
                0.001,
                solver_timeout
                - model_build_seconds
                - MAXSAT_REPORTING_MARGIN_SECONDS,
            )
        elif solver_name in EXACT_SOLVERS:
            solver_object.solver_timeout = max(
                0.001,
                solver_timeout
                - model_build_seconds
                - MAXSAT_REPORTING_MARGIN_SECONDS,
            )
        output.put(
            _formula_metadata(
                solver_name,
                solver_object,
                input_parsing_seconds=input_parsing_seconds,
                model_construction_seconds=model_construction_seconds,
                model_build_seconds=model_build_seconds,
            )
        )
        if solver_name in {"incremental", "multiple"}:
            result = solver_object.solve(
                verbose=verbose,
                incumbent_callback=lambda value: output.put(
                    {
                        "message_type": "incumbent",
                        "best_value": (
                            int(value[0])
                            if isinstance(value, tuple)
                            else int(value)
                        ),
                        "objective_vector": (
                            tuple(int(item) for item in value)
                            if isinstance(value, tuple)
                            else (int(value),)
                        ),
                    }
                ),
            )
        else:
            result = solver_object.solve(verbose=verbose)
        finished = time.perf_counter()
        output.put(
            _result_payload(
                result,
                solver_name=solver_name,
                precedence_encoding=precedence_encoding,
                precedence_graph=precedence_graph,
                domain_filter_graph=domain_filter_graph,
                encoding_variant=encoding_variant,
                domain_mode=domain_mode,
                runtime_seconds=finished - started,
                model_build_seconds=model_build_seconds,
                solve_and_validate_seconds=finished - model_ready,
            )
        )
    except BaseException as exc:
        output.put(
            {
                "message_type": "result",
                "status": "ERROR",
                "sat_result": "ERROR",
                "solver": solver_name,
                "precedence_mode": legacy_precedence_mode(
                    precedence_encoding, precedence_graph
                ),
                "precedence_encoding": precedence_encoding,
                "precedence_graph": precedence_graph,
                "precedence_configuration": (
                    f"{precedence_encoding}+{precedence_graph}"
                ),
                "domain_filter_graph": domain_filter_graph,
                "encoding_variant": encoding_variant,
                "domain_mode": domain_mode,
                "objective_mode": objective_mode,
                "compact_encoding": compact_encoding,
                "collision_amo": collision_amo,
                "runtime_seconds": round(time.perf_counter() - started, 6),
                "runtime_scope": RUNTIME_SCOPE,
                "runtime_censored": False,
                "formula_scope": FORMULA_SCOPE,
                "objective": OBJECTIVE_KEYS.get(
                    objective_mode,
                    "unknown_objective",
                ),
                "best_value": None,
                "maxsat_backend_preference": (
                    maxsat_backend if solver_name == "maxsat" else ""
                ),
                "sat_backend_preference": (
                    sat_backend
                    if solver_name in {"incremental", "multiple"}
                    else ""
                ),
                "validation_errors": "",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
        )


def _process_tree_rss_bytes(pid: int) -> int | None:
    if psutil is None:
        return None
    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except (psutil.Error, OSError):
        return None
    total = 0
    for process in processes:
        try:
            total += process.memory_info().rss
        except (psutil.Error, OSError):
            continue
    return total


def _terminate_process_tree(process: mp.Process, grace_seconds: float = 5.0) -> None:
    """Terminate the worker and any external solver children it launched."""

    if process.pid is None:
        return

    if psutil is None:
        process.terminate()
        process.join(grace_seconds)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(grace_seconds)
        return

    try:
        root = psutil.Process(process.pid)
        children = root.children(recursive=True)
    except (psutil.Error, OSError):
        children = []

    for child in children:
        try:
            child.terminate()
        except (psutil.Error, OSError):
            continue

    process.terminate()
    process.join(grace_seconds)

    _, alive_children = psutil.wait_procs(children, timeout=grace_seconds)
    for child in alive_children:
        try:
            child.kill()
        except (psutil.Error, OSError):
            continue

    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(grace_seconds)


def _drain_queue(
    output: mp.Queue[Any],
    metadata: dict[str, Any],
    result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    while True:
        try:
            message = output.get_nowait()
        except queue_module.Empty:
            return result
        if message.get("message_type") == "metadata":
            metadata.update(
                {key: value for key, value in message.items() if key != "message_type"}
            )
        elif message.get("message_type") == "result":
            result = message
        elif message.get("message_type") == "incumbent":
            candidate = message.get("best_value")
            current = metadata.get("incumbent_best_value")
            if candidate is not None and (current is None or candidate < current):
                metadata["incumbent_best_value"] = candidate
            vector = message.get("objective_vector")
            current_vector = metadata.get("incumbent_objective_vector")
            if vector is not None and (
                current_vector is None or tuple(vector) < tuple(current_vector)
            ):
                metadata["incumbent_objective_vector"] = tuple(vector)


def _terminal_payload(
    status: str,
    *,
    solver_name: str,
    precedence_encoding: str,
    precedence_graph: str,
    domain_filter_graph: str,
    encoding_variant: str,
    domain_mode: str,
    maxsat_backend: str,
    sat_backend: str,
    runtime_seconds: float,
    objective_mode: str,
    compact_encoding: str,
    collision_amo: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "sat_result": status_to_sat_result(status),
        "solver": solver_name,
        "precedence_mode": legacy_precedence_mode(
            precedence_encoding, precedence_graph
        ),
        "precedence_encoding": precedence_encoding,
        "precedence_graph": precedence_graph,
        "precedence_configuration": (
            f"{precedence_encoding}+{precedence_graph}"
        ),
        "domain_filter_graph": domain_filter_graph,
        "encoding_variant": encoding_variant,
        "domain_mode": domain_mode,
        "objective_mode": objective_mode,
        "compact_encoding": compact_encoding,
        "collision_amo": collision_amo,
        "runtime_seconds": round(runtime_seconds, 6),
        "runtime_scope": (
            "configured wall-clock cutoff measured by the controller; "
            "the worker returned no completed validated result"
        ),
        "runtime_censored": status == "TIMEOUT",
        "formula_scope": FORMULA_SCOPE,
        "objective": OBJECTIVE_KEYS.get(objective_mode, "unknown_objective"),
        "best_value": None,
        "maxsat_backend_preference": (
            maxsat_backend if solver_name == "maxsat" else ""
        ),
        "sat_backend_preference": (
            sat_backend
            if solver_name in {"incremental", "multiple"}
            else ""
        ),
        "validation_errors": "",
        "error_type": "",
        "error_message": "",
    }


def run_with_timeout(
    solver_name: str,
    instance_path: Path,
    precedence_encoding: str,
    precedence_graph: str,
    domain_filter_graph: str,
    encoding_variant: str,
    domain_mode: str,
    maxsat_backend: str,
    uwrmaxsat_bin: str | None,
    uwrmaxsat_sha256: str | None,
    sat_backend: str,
    timeout_seconds: float,
    verbose: bool,
    threads: int = 1,
    random_seed: int = 0,
    objective_mode: str = "ir",
    compact_encoding: str = "reference",
    collision_amo: str = "pairwise",
) -> dict[str, Any]:
    context = mp.get_context("spawn")
    output: mp.Queue[Any] = context.Queue()
    process = context.Process(
        target=_worker,
        args=(
            solver_name,
            str(instance_path),
            precedence_encoding,
            precedence_graph,
            domain_filter_graph,
            encoding_variant,
            domain_mode,
            objective_mode,
            compact_encoding,
            collision_amo,
            maxsat_backend,
            uwrmaxsat_bin,
            uwrmaxsat_sha256,
            sat_backend,
            timeout_seconds,
            threads,
            random_seed,
            verbose,
            output,
        ),
    )
    started = time.perf_counter()
    deadline = started + timeout_seconds
    metadata: dict[str, Any] = {}
    result: dict[str, Any] | None = None
    peak_rss_bytes: int | None = None
    process.start()

    while process.is_alive() and time.perf_counter() < deadline:
        current_rss = _process_tree_rss_bytes(process.pid)
        if current_rss is not None:
            peak_rss_bytes = max(peak_rss_bytes or 0, current_rss)
        result = _drain_queue(output, metadata, result)
        process.join(
            min(
                MEMORY_SAMPLE_INTERVAL_SECONDS,
                max(0.0, deadline - time.perf_counter()),
            )
        )

    if process.is_alive():
        _terminate_process_tree(process)
        # Capture a final incumbent/metadata message emitted just before the
        # hard controller cutoff. A completed result is intentionally ignored
        # here because the process exceeded the configured wall-clock limit.
        _drain_queue(output, metadata, result)
        result = _terminal_payload(
            "TIMEOUT",
            solver_name=solver_name,
            precedence_encoding=precedence_encoding,
            precedence_graph=precedence_graph,
            domain_filter_graph=domain_filter_graph,
            encoding_variant=encoding_variant,
            domain_mode=domain_mode,
            maxsat_backend=maxsat_backend,
            sat_backend=sat_backend,
            runtime_seconds=time.perf_counter() - started,
            objective_mode=objective_mode,
            compact_encoding=compact_encoding,
            collision_amo=collision_amo,
        )
    else:
        process.join()
        grace_deadline = time.perf_counter() + QUEUE_GRACE_SECONDS
        while result is None and time.perf_counter() < grace_deadline:
            result = _drain_queue(output, metadata, result)
            if result is None:
                time.sleep(0.01)
        if result is None:
            result = _terminal_payload(
                "ERROR",
                solver_name=solver_name,
                precedence_encoding=precedence_encoding,
                precedence_graph=precedence_graph,
                domain_filter_graph=domain_filter_graph,
                encoding_variant=encoding_variant,
                domain_mode=domain_mode,
                maxsat_backend=maxsat_backend,
                sat_backend=sat_backend,
                runtime_seconds=time.perf_counter() - started,
                objective_mode=objective_mode,
                compact_encoding=compact_encoding,
                collision_amo=collision_amo,
            )
            result["error_type"] = "NoWorkerPayload"
            result["error_message"] = "Worker returned no result"

    for key, value in metadata.items():
        result.setdefault(key, value)
    if result.get("best_value") is None and metadata.get(
        "incumbent_best_value"
    ) is not None:
        result["best_value"] = metadata["incumbent_best_value"]
    if result.get("objective_vector") is None and metadata.get(
        "incumbent_objective_vector"
    ) is not None:
        result["objective_vector"] = tuple(
            metadata["incumbent_objective_vector"]
        )
    if result.get("runtime_censored") and result.get("model_build_seconds") is not None:
        result.setdefault(
            "solve_and_validate_seconds",
            round(
                max(
                    0.0,
                    float(result["runtime_seconds"])
                    - float(result["model_build_seconds"]),
                ),
                6,
            ),
        )
    result.update(
        configuration_metadata(
            solver_name=solver_name,
            precedence_encoding=precedence_encoding,
            precedence_graph=precedence_graph,
            domain_filter_graph=domain_filter_graph,
            encoding_variant=encoding_variant,
            domain_mode=domain_mode,
            maxsat_backend=maxsat_backend,
            sat_backend=sat_backend,
            objective_mode=objective_mode,
            compact_encoding=compact_encoding,
            collision_amo=collision_amo,
        )
    )
    result.setdefault("formula_scope", FORMULA_SCOPE)
    result.setdefault("runtime_scope", RUNTIME_SCOPE)
    result.setdefault("runtime_censored", False)
    result.setdefault("best_value", result.get("objective_value"))
    result["peak_memory_mb"] = (
        None
        if peak_rss_bytes is None
        else round(peak_rss_bytes / (1024 * 1024), 3)
    )
    result["memory_metric"] = "peak_process_tree_rss_mb"
    result["sat_result"] = status_to_sat_result(result.get("status"))
    output.close()
    output.join_thread()
    return result


def _detailed_csv_path(csv_path: str) -> Path:
    path = Path(csv_path)
    suffix = path.suffix or ".csv"
    return path.with_name(f"{path.stem}_detailed{suffix}")


def _excel_output_dir(excel_dir: str | None, csv_path: str) -> Path:
    if excel_dir:
        return Path(excel_dir)
    return Path(csv_path).parent / "excel"


def write_instance_excel(
    output_dir: Path,
    instance_name: str,
    results: list[dict[str, Any]],
) -> Path:
    return write_instance_workbook(
        output_dir / safe_workbook_name(instance_name),
        instance_name,
        results,
    )


def write_detailed_csv(path: Path, results: list[dict[str, Any]]) -> None:
    preferred_fields = [column.key for column in RESULT_COLUMNS]
    preferred_fields.extend(
        key
        for key in [
            "solver",
            "maxsat_backend_preference",
            "sat_backend_preference",
            "resolved_uwrmaxsat_bin",
            "precedence_mode",
            "precedence_configuration",
            "objective_value",
            "objective_participant_count",
            "objective_participants",
            "initial_schedule_candidates",
            "unary_removed_schedule_candidates",
            "preprocessing_removed_schedule_candidates",
            "removed_schedule_candidates",
            "participant_internal_idle_slots",
            "busy_participants_per_slot",
            "assignment",
            "schedule_by_slot",
            "enabled_constraints",
        ]
        if key not in preferred_fields
    )
    extra_fields = sorted(
        {
            key
            for result in results
            for key in result
            if key not in preferred_fields
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[*preferred_fields, *extra_fields],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(results)


def format_table_cell(result: dict[str, Any]) -> str:
    status = result.get("sat_result")
    if status == "TIMEOUT":
        return "TO"
    if status == "UNSAT":
        return "UNSAT"
    if status == "ERROR":
        return "ERR"
    runtime = result.get("runtime_seconds")
    objective = result.get("objective_vector")
    if objective is None:
        objective = result.get("objective_value")
    if objective is None:
        objective = result.get("idle_range_pstar")
    return f"{float(runtime):.1f} {objective}"


def write_aggregate_csv(path: Path, results: list[dict[str, Any]]) -> None:
    grouped: dict[
        tuple[str, str, str, str, str, str, str, str, str],
        dict[str, Any],
    ] = {}
    for result in results:
        key = (
            result["instance"],
            result["precedence_encoding"],
            result["precedence_graph"],
            result.get("domain_filter_graph", "distance_closure"),
            result["solver"],
            result["domain_mode"],
            result.get("objective_mode", "ir"),
            result.get("compact_encoding", "reference"),
            result.get("collision_amo", "pairwise"),
        )
        row = grouped.setdefault(
            key,
            {
                "instance": result["instance"],
                "precedence_encoding": result["precedence_encoding"],
                "precedence_graph": result["precedence_graph"],
                "precedence_configuration": result["precedence_configuration"],
                "domain_filter_graph": result.get(
                    "domain_filter_graph",
                    "distance_closure",
                ),
                "solver": result["solver"],
                "objective": result.get("objective", "IdleRange(P*)"),
                "objective_mode": result.get("objective_mode", "ir"),
                "compact_encoding": result.get(
                    "compact_encoding", "reference"
                ),
                "collision_amo": result.get("collision_amo", "pairwise"),
                "domain_mode": result["domain_mode"],
            },
        )
        row[result["encoding_variant"]] = format_table_cell(result)

    rows = list(grouped.values())
    for row in rows:
        for variant in AGGREGATE_VARIANTS:
            row.setdefault(variant, "-")
    rows.sort(
        key=lambda row: (
            row["instance"],
            row["domain_mode"],
            row["domain_filter_graph"],
            row["precedence_encoding"],
            row["precedence_graph"],
            row["solver"],
            row["objective_mode"],
            row["compact_encoding"],
            row["collision_amo"],
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "instance",
        "precedence_encoding",
        "precedence_graph",
        "precedence_configuration",
        "domain_filter_graph",
        "solver",
        "objective",
        "objective_mode",
        "compact_encoding",
        "collision_amo",
        "domain_mode",
        *AGGREGATE_VARIANTS,
    ]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _git_metadata() -> tuple[str, bool | None]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
        return commit, dirty
    except (OSError, subprocess.SubprocessError):
        return "", None


def _cpu_model() -> str:
    value = platform.processor().strip()
    if value:
        return value
    try:
        with Path("/proc/cpuinfo").open(encoding="utf-8") as stream:
            for line in stream:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def experiment_metadata(
    args: argparse.Namespace,
    argv: list[str] | None,
    runner_path: str | Path | None = None,
) -> dict[str, Any]:
    commit, dirty = _git_metadata()
    physical_cores = psutil.cpu_count(logical=False) if psutil is not None else None
    total_memory_mb = (
        round(psutil.virtual_memory().total / (1024 * 1024), 3)
        if psutil is not None
        else None
    )
    command_args = sys.argv[1:] if argv is None else argv
    return {
        "run_started_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "runner_command": shlex.join(
            [
                sys.executable,
                str(Path(runner_path or __file__).resolve()),
                *command_args,
            ]
        ),
        "git_commit": commit,
        "git_dirty": dirty,
        "python_version": platform.python_version(),
        "pysat_version": pysat_version,
        "hostname": platform.node(),
        "platform": platform.platform(),
        "cpu_model": _cpu_model(),
        "physical_cpu_cores": physical_cores,
        "logical_cpu_cores": os.cpu_count(),
        "system_memory_mb": total_memory_mb,
        "timeout_seconds": args.timeout,
        "memory_limit_mb": None,
        "threads": getattr(args, "threads", 1),
        "random_seed": getattr(args, "random_seed", 0),
    }


def instance_result_metadata(instance: InstanceSpec) -> dict[str, Any]:
    return {
        "instance": instance.instance_name,
        "instance_content_id": instance.content_id,
        "base_lineage_id": (
            instance.base_lineage_id
            or base_lineage_id(instance.instance_name)
        ),
        "instance_sha256": instance.sha256,
        "instance_family": instance.family,
        "instance_variant": instance.variant,
        "instance_path": str(instance.path),
        "source_alias_count": instance.source_alias_count,
        "source_alias_paths": instance.source_alias_paths,
        "repository_alias_count": instance.repository_alias_count,
        "repository_alias_paths": instance.repository_alias_paths,
        "dataset_source_page": instance.dataset_source_page,
        "dataset_archive_url": instance.dataset_archive_url,
        "dataset_archive_sha256": instance.dataset_archive_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        instances = collect_instances(
            args.instance,
            args.data_dir,
            args.manifest,
            args.family,
            args.keep_path_aliases,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 2

    solvers = selected_solvers(args.solver)
    try:
        require_solver_environment(
            solvers,
            maxsat_backend=args.maxsat_backend,
            uwrmaxsat_bin=args.uwrmaxsat_bin,
            uwrmaxsat_sha256=args.uwrmaxsat_sha256,
            sat_backend=args.sat_backend,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    instance_configurations = [
        (instance, benchmark_configurations(args, instance, solvers))
        for instance in instances
    ]
    total_runs = sum(
        len(configurations) for _, configurations in instance_configurations
    )
    results: list[dict[str, Any]] = []
    excel_output_dir = _excel_output_dir(args.excel_dir, args.csv)
    current_run = 0
    experiment = experiment_metadata(args, argv)

    print(
        f"B2B benchmark: {total_runs} run(s), "
        f"objective_mode={args.objective_mode}, "
        f"compact_encoding={args.compact_encoding}, "
        f"collision_amo={args.collision_amo}"
    )
    print(
        f"Selected input paths: {len(instances)} "
        f"(family={args.family}, "
        f"content_deduplicated={not args.keep_path_aliases})"
    )
    if any(solver in SAT_SOLVERS for solver in solvers):
        domain_modes = selected(args.domain_mode, DOMAIN_MODES, "both")
        print(f"SAT/MaxSAT domain mode(s): {', '.join(domain_modes)}")
        print(
            "Reduced-domain filter graph(s): "
            + ", ".join(
                selected(
                    args.domain_filter_graph,
                    DOMAIN_FILTER_GRAPHS,
                    "both",
                )
            )
        )
    if any(solver in EXACT_SOLVERS for solver in solvers):
        print(
            "Exact baselines: Reduced + DistanceClosure; "
            f"threads={args.threads}, seed={args.random_seed}"
        )
    if "maxsat" in solvers:
        print(f"MaxSAT backend: {args.maxsat_backend}")
    if any(solver in {"incremental", "multiple"} for solver in solvers):
        print(f"SAT backend: {args.sat_backend}")
    for instance, configurations in instance_configurations:
        print(f"Instance {instance.instance_name}: {len(configurations)} run(s)")
        instance_results: list[dict[str, Any]] = []
        for configuration in configurations:
            current_run += 1
            print(
                f"[{current_run}/{total_runs}] {instance.instance_name} | "
                f"{configuration.domain_mode} | "
                f"{configuration.solver_name} | "
                f"F={configuration.domain_filter_graph} | "
                f"P={configuration.precedence_encoding} | "
                f"G={configuration.precedence_graph} | "
                f"{configuration.encoding_variant} | "
                f"objective={configuration.objective_mode} | "
                f"compact={configuration.compact_encoding} | "
                f"collision_amo={configuration.collision_amo}",
                flush=True,
            )
            result = run_with_timeout(
                configuration.solver_name,
                instance.path,
                configuration.precedence_encoding,
                configuration.precedence_graph,
                configuration.domain_filter_graph,
                configuration.encoding_variant,
                configuration.domain_mode,
                args.maxsat_backend,
                args.uwrmaxsat_bin,
                args.uwrmaxsat_sha256,
                args.sat_backend,
                args.timeout,
                args.verbose,
                args.threads,
                args.random_seed,
                configuration.objective_mode,
                configuration.compact_encoding,
                configuration.collision_amo,
            )
            result = {
                **instance_result_metadata(instance),
                **experiment,
                **result,
            }
            results.append(result)
            instance_results.append(result)
            workbook_path = None
            if not args.no_excel:
                workbook_path = write_instance_excel(
                    excel_output_dir,
                    instance.instance_name,
                    instance_results,
                )
            print(
                f"    {result['sat_result']} | "
                f"objective={result.get('objective_vector')} | "
                f"time={result.get('runtime_seconds')}s | "
                f"config={result.get('configuration_label')}",
                flush=True,
            )
        if not args.no_excel:
            print(f"    Excel: {workbook_path}", flush=True)

    detailed_path = Path(args.long_csv) if args.long_csv else _detailed_csv_path(args.csv)
    aggregate_path = Path(args.csv)
    write_detailed_csv(detailed_path, results)
    write_aggregate_csv(aggregate_path, results)
    print(f"Detailed CSV: {detailed_path}")
    print(f"Aggregate CSV: {aggregate_path}")
    if not args.no_excel:
        print(f"Per-instance Excel directory: {excel_output_dir}")

    errors = sum(result["sat_result"] == "ERROR" for result in results)
    timeouts = sum(result["sat_result"] == "TIMEOUT" for result in results)
    print(f"Completed with errors={errors}, timeouts={timeouts}.")
    return 2 if errors else 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
