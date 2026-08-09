from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import random
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from Dataset_Manifest import file_sha256
from Main import InstanceSpec, collect_instances, write_detailed_csv
from MaxSAT_Solver import executable_sha256, resolve_uwrmaxsat_binary

try:
    import psutil
except ImportError:  # pragma: no cover - production requirements include psutil.
    psutil = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
TERMINAL_STATUSES = {"OPTIMAL", "UNSAT", "TIMEOUT"}
SUPPORTED_EXECUTORS = {"main", "org_bg_d2", "org_ir"}
SUPPORTED_BOOLEAN_SOLVERS = {"maxsat", "multiple", "incremental"}


class CampaignInterrupted(Exception):
    """Raised after a termination signal so the active child can be reaped."""


def _boot_id() -> str:
    path = Path("/proc/sys/kernel/random/boot_id")
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return ""


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


def _cpu_governor() -> str:
    try:
        return Path(
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
        ).read_text(encoding="ascii").strip()
    except OSError:
        return ""


def current_machine_profile() -> dict[str, Any]:
    """Return the hardware fields that define a comparable production VM."""
    memory = psutil.virtual_memory() if psutil is not None else None
    swap = psutil.swap_memory() if psutil is not None else None
    return {
        "cpu_model": _cpu_model(),
        "physical_cpu_cores": (
            psutil.cpu_count(logical=False) if psutil is not None else None
        ),
        "logical_cpu_cores": os.cpu_count(),
        "system_memory_mb": (
            round(memory.total / (1024 * 1024), 3)
            if memory is not None
            else None
        ),
        "swap_memory_mb": (
            round(swap.total / (1024 * 1024), 3)
            if swap is not None
            else None
        ),
    }


def machine_profile_errors(
    required: dict[str, Any] | None,
    actual: dict[str, Any] | None = None,
) -> list[str]:
    """Compare an observed VM against a frozen experiment resource profile."""
    if not required:
        return []
    observed = dict(actual or current_machine_profile())
    errors: list[str] = []
    required_model = str(required.get("cpu_model_contains", "")).strip()
    actual_model = str(observed.get("cpu_model", "")).strip()
    if required_model and required_model.casefold() not in actual_model.casefold():
        errors.append(
            f"cpu_model={actual_model!r} does not contain {required_model!r}"
        )
    for field in ("physical_cpu_cores", "logical_cpu_cores"):
        if field not in required:
            continue
        if observed.get(field) is None:
            errors.append(f"{field} could not be measured")
        elif int(observed[field]) != int(required[field]):
            errors.append(
                f"{field}={observed[field]!r}, expected {required[field]!r}"
            )
    bounded_fields = (
        ("system_memory_mb", "system_memory_mb_min", "system_memory_mb_max"),
        ("swap_memory_mb", "swap_memory_mb_min", "swap_memory_mb_max"),
    )
    for actual_field, minimum_field, maximum_field in bounded_fields:
        if minimum_field not in required and maximum_field not in required:
            continue
        value = observed.get(actual_field)
        if value is None:
            errors.append(f"{actual_field} could not be measured")
            continue
        numeric = float(value)
        if minimum_field in required and numeric < float(required[minimum_field]):
            errors.append(
                f"{actual_field}={numeric}, below {required[minimum_field]}"
            )
        if maximum_field in required and numeric > float(required[maximum_field]):
            errors.append(
                f"{actual_field}={numeric}, above {required[maximum_field]}"
            )
    return errors


def _machine_snapshot() -> dict[str, Any]:
    try:
        load = os.getloadavg()
    except (AttributeError, OSError):  # pragma: no cover - Windows fallback.
        load = (None, None, None)
    available_memory_mb = (
        round(psutil.virtual_memory().available / (1024 * 1024), 3)
        if psutil is not None
        else None
    )
    return {
        "load_1m": load[0],
        "load_5m": load[1],
        "load_15m": load[2],
        "available_memory_mb": available_memory_mb,
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_seed(*parts: Any) -> int:
    digest = hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _relative_to_project(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def _resolve_project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def read_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        config = json.load(stream)
    blocks_from = config.get("blocks_from")
    if blocks_from:
        source_path = Path(str(blocks_from))
        if not source_path.is_absolute():
            source_path = path.parent / source_path
        with source_path.resolve().open(encoding="utf-8") as stream:
            source = json.load(stream)
        if not source.get("blocks"):
            raise ValueError(f"blocks_from contains no blocks: {source_path}")
        config["blocks"] = source["blocks"]
        overrides = {
            str(key): int(value)
            for key, value in config.get("block_repetitions", {}).items()
        }
        known_blocks = {block.get("id") for block in config["blocks"]}
        unknown_overrides = sorted(set(overrides) - known_blocks)
        if unknown_overrides:
            raise ValueError(
                f"repetition overrides reference unknown blocks: {unknown_overrides}"
            )
        for block in config["blocks"]:
            if block.get("id") in overrides:
                block["repetitions"] = overrides[block["id"]]
        dataset_overrides = config.get("block_datasets", {})
        unknown_dataset_overrides = sorted(
            set(dataset_overrides) - known_blocks
        )
        if unknown_dataset_overrides:
            raise ValueError(
                "dataset overrides reference unknown blocks: "
                f"{unknown_dataset_overrides}"
            )
        for block in config["blocks"]:
            if block.get("id") in dataset_overrides:
                block["datasets"] = list(dataset_overrides[block["id"]])
        config["resolved_blocks_from"] = _relative_to_project(source_path)
        config["resolved_blocks_sha256"] = file_sha256(source_path)
    if config.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported journal config schema: {config.get('schema_version')!r}"
        )
    if not config.get("campaign_name"):
        raise ValueError("campaign_name is required")
    if not config.get("datasets"):
        raise ValueError("at least one dataset is required")
    if not config.get("blocks"):
        raise ValueError("at least one experiment block is required")
    return config


def _filter_instances(
    instances: list[InstanceSpec],
    dataset: dict[str, Any],
) -> list[InstanceSpec]:
    names = set(dataset.get("instance_names", []))
    variants = set(dataset.get("variants", []))
    lineages = set(dataset.get("lineage_ids", []))
    selected = [
        instance
        for instance in instances
        if (not names or instance.instance_name in names)
        and (not variants or bool(variants.intersection(instance.variant.split("|"))))
        and (
            not lineages
            or (instance.base_lineage_id or "") in lineages
        )
    ]
    selected.sort(key=lambda instance: (instance.base_lineage_id, instance.instance_name))
    max_instances = dataset.get("max_instances")
    if max_instances is not None:
        if int(max_instances) <= 0:
            raise ValueError("max_instances must be positive")
        selected = selected[: int(max_instances)]
    if not selected:
        raise ValueError(f"dataset {dataset['id']!r} selects no instances")
    return selected


def resolve_datasets(config: dict[str, Any]) -> dict[str, list[InstanceSpec]]:
    resolved: dict[str, list[InstanceSpec]] = {}
    for dataset in config["datasets"]:
        dataset_id = dataset.get("id")
        if not dataset_id or dataset_id in resolved:
            raise ValueError(f"invalid or duplicate dataset id: {dataset_id!r}")
        manifest = _resolve_project_path(dataset["manifest"])
        instances = collect_instances(
            None,
            None,
            str(manifest),
            dataset.get("family", "all"),
        )
        resolved[dataset_id] = _filter_instances(instances, dataset)
    return resolved


def _validate_configuration(configuration: dict[str, Any]) -> None:
    config_id = configuration.get("id")
    executor = configuration.get("executor")
    if not config_id:
        raise ValueError("every configuration requires an id")
    if executor not in SUPPORTED_EXECUTORS:
        raise ValueError(f"unsupported executor for {config_id}: {executor!r}")
    if executor == "main":
        solver = configuration.get("solver")
        if solver not in SUPPORTED_BOOLEAN_SOLVERS:
            raise ValueError(
                f"journal main configuration {config_id!r} requires one "
                f"Boolean solver, got {solver!r}"
            )
        if configuration.get("objective_mode") not in {
            "ir",
            "bg_d2",
            "ir_is",
            "bg_ir_is",
        }:
            raise ValueError(f"invalid objective mode in {config_id!r}")
        for field in (
            "domain_mode",
            "precedence_encoding",
            "precedence_graph",
            "domain_filter_graph",
            "encoding_variant",
        ):
            if field not in configuration:
                raise ValueError(f"{config_id!r} is missing {field!r}")
    elif executor == "org_bg_d2":
        if configuration.get("objective_mode", "bg_d2") != "bg_d2":
            raise ValueError("org_bg_d2 executor only supports bg_d2")
    elif executor == "org_ir":
        if configuration.get("objective_mode", "ir") != "ir":
            raise ValueError("org_ir executor only supports ir")


def _balanced_instance_order(
    instances: Iterable[InstanceSpec],
    *,
    seed: int,
) -> list[InstanceSpec]:
    """Deterministically shuffle while interleaving benchmark families."""

    rng = random.Random(seed)
    groups: dict[str, list[InstanceSpec]] = {}
    for instance in instances:
        groups.setdefault(instance.family, []).append(instance)
    for values in groups.values():
        rng.shuffle(values)
    group_names = sorted(groups)
    rng.shuffle(group_names)
    ordered: list[InstanceSpec] = []
    while any(groups.values()):
        for group_name in group_names:
            if groups[group_name]:
                ordered.append(groups[group_name].pop())
        rng.shuffle(group_names)
    return ordered


def build_plan(
    config: dict[str, Any],
    datasets: dict[str, list[InstanceSpec]],
    *,
    only_blocks: set[str] | None = None,
) -> dict[str, Any]:
    global_seed = int(config.get("run_order_seed", 0))
    jobs: list[dict[str, Any]] = []
    seen_configurations: dict[str, str] = {}
    seen_blocks: set[str] = set()
    for block in config["blocks"]:
        block_id = block.get("id")
        if not block_id or block_id in seen_blocks:
            raise ValueError(f"invalid or duplicate block id: {block_id!r}")
        seen_blocks.add(block_id)
        if only_blocks and block_id not in only_blocks:
            continue
        repetitions = int(block.get("repetitions", 1))
        if repetitions <= 0:
            raise ValueError(f"block {block_id!r} has invalid repetitions")
        block_instances: dict[str, InstanceSpec] = {}
        for dataset_id in block.get("datasets", []):
            if dataset_id not in datasets:
                raise ValueError(
                    f"block {block_id!r} references unknown dataset {dataset_id!r}"
                )
            for instance in datasets[dataset_id]:
                block_instances.setdefault(instance.content_id, instance)
        if not block_instances:
            raise ValueError(f"block {block_id!r} selects no instances")
        configurations = block.get("configurations", [])
        if not configurations:
            raise ValueError(f"block {block_id!r} has no configurations")
        for configuration in configurations:
            _validate_configuration(configuration)
            config_id = configuration["id"]
            serialized = canonical_json(configuration)
            if (
                config_id in seen_configurations
                and seen_configurations[config_id] != serialized
            ):
                raise ValueError(
                    "a planned configuration id has divergent definitions: "
                    f"{config_id!r}"
                )
            seen_configurations[config_id] = serialized

        for repetition in range(1, repetitions + 1):
            instances = _balanced_instance_order(
                block_instances.values(),
                seed=_stable_seed(
                    global_seed,
                    block_id,
                    repetition,
                    "instances",
                ),
            )
            for instance in instances:
                ordered_configurations = [dict(value) for value in configurations]
                random.Random(
                    _stable_seed(
                        global_seed,
                        block_id,
                        repetition,
                        instance.content_id,
                        "configurations",
                    )
                ).shuffle(ordered_configurations)
                for configuration in ordered_configurations:
                    run_key = "::".join(
                        (
                            config["campaign_name"],
                            block_id,
                            instance.content_id,
                            configuration["id"],
                            f"rep-{repetition}",
                        )
                    )
                    jobs.append(
                        {
                            "run_order": len(jobs) + 1,
                            "run_key": run_key,
                            "experiment_block": block_id,
                            "repetition": repetition,
                            "instance": instance.instance_name,
                            "instance_content_id": instance.content_id,
                            "instance_sha256": instance.sha256,
                            "base_lineage_id": instance.base_lineage_id,
                            "instance_family": instance.family,
                            "instance_variant": instance.variant,
                            "instance_path": _relative_to_project(instance.path),
                            "planned_configuration_id": configuration["id"],
                            "configuration": configuration,
                        }
                    )
    if not jobs:
        raise ValueError("the selected block filter produced no jobs")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": config["campaign_name"],
        "run_order_seed": global_seed,
        "timeout_seconds": float(config.get("timeout_seconds", 7200.0)),
        "controller_grace_seconds": float(
            config.get("controller_grace_seconds", 120.0)
        ),
        "required_machine": dict(config.get("required_machine", {})),
        "job_count": len(jobs),
        "jobs": jobs,
    }
    payload["plan_sha256"] = sha256_text(canonical_json(payload))
    return payload


def _git_metadata() -> tuple[str, bool]:
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


def build_environment(
    config_path: Path,
    config: dict[str, Any],
    plan: dict[str, Any],
    *,
    uwrmaxsat_binary: Path | None,
    command: list[str],
) -> dict[str, Any]:
    commit, dirty = _git_metadata()
    manifest_hashes = {}
    for dataset in config["datasets"]:
        manifest = _resolve_project_path(dataset["manifest"])
        manifest_hashes[_relative_to_project(manifest)] = file_sha256(manifest)
    requirements = PROJECT_ROOT / "src" / "requirements.txt"
    environment = {
        "created_utc": utc_now(),
        "campaign_id": config["campaign_name"],
        "config_path": _relative_to_project(config_path),
        "config_sha256": file_sha256(config_path),
        "plan_sha256": plan["plan_sha256"],
        "git_commit": commit,
        "git_dirty": dirty,
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "hostname": platform.node(),
        "boot_id": _boot_id(),
        "cpu_governor": _cpu_governor(),
        "kernel_release": platform.release(),
        "required_machine": plan.get("required_machine", {}),
        "requirements_sha256": (
            file_sha256(requirements) if requirements.is_file() else ""
        ),
        "manifest_sha256": manifest_hashes,
        "uwrmaxsat_binary": str(uwrmaxsat_binary or ""),
        "uwrmaxsat_sha256": (
            executable_sha256(uwrmaxsat_binary)
            if uwrmaxsat_binary is not None
            else ""
        ),
        "runner_command": shlex.join(command),
    }
    environment.update(current_machine_profile())
    return environment


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"corrupt JSONL at {path}:{line_number}: {exc}"
                ) from exc
    return records


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(canonical_json(record) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def latest_attempts(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        run_key = record["run_key"]
        previous = latest.get(run_key)
        if previous is None or int(record["attempt"]) > int(previous["attempt"]):
            latest[run_key] = record
        elif int(record["attempt"]) == int(previous["attempt"]):
            raise ValueError(f"duplicate attempt for run_key={run_key!r}")
    return latest


def _read_single_csv_row(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 1:
        raise ValueError(f"expected one result row in {path}, found {len(rows)}")
    return dict(rows[0])


def _configuration_command(
    job: dict[str, Any],
    *,
    timeout: float,
    temp_dir: Path,
    uwrmaxsat_binary: Path | None,
    uwrmaxsat_sha256: str,
) -> tuple[list[str], Path]:
    configuration = job["configuration"]
    executor = configuration["executor"]
    instance_path = _resolve_project_path(job["instance_path"])
    detailed = temp_dir / "detailed.csv"
    if executor == "main":
        command = [
            sys.executable,
            "-u",
            str(PROJECT_ROOT / "src" / "Main.py"),
            "--instance",
            str(instance_path),
            "--solver",
            configuration["solver"],
            "--objective-mode",
            configuration["objective_mode"],
            "--domain-mode",
            configuration["domain_mode"],
            "--domain-filter-graph",
            configuration["domain_filter_graph"],
            "--precedence-encoding",
            configuration["precedence_encoding"],
            "--precedence-graph",
            configuration["precedence_graph"],
            "--encoding-variant",
            configuration["encoding_variant"],
            "--maxsat-backend",
            configuration.get("maxsat_backend", "uwrmaxsat"),
            "--sat-backend",
            configuration.get("sat_backend", "cadical"),
            "--timeout",
            str(timeout),
            "--threads",
            "1",
            "--random-seed",
            "0",
            "--csv",
            str(temp_dir / "aggregate.csv"),
            "--long-csv",
            str(detailed),
            "--no-excel",
        ]
        if configuration["solver"] == "maxsat" and configuration.get(
            "maxsat_backend", "uwrmaxsat"
        ) == "uwrmaxsat":
            if uwrmaxsat_binary is None:
                raise FileNotFoundError("UWrMaxSAT is required by this configuration")
            command.extend(
                [
                    "--uwrmaxsat-bin",
                    str(uwrmaxsat_binary),
                    "--uwrmaxsat-sha256",
                    uwrmaxsat_sha256,
                ]
            )
        return command, detailed
    if executor == "org_bg_d2":
        backend = configuration.get("backend", "uwrmaxsat")
        command = [
            sys.executable,
            "-u",
            str(PROJECT_ROOT / "src" / "ORG_BG_D2.py"),
            "--instance",
            str(instance_path),
            "--backend",
            backend,
            "--timeout",
            str(timeout),
            "--csv",
            str(detailed),
            "--no-excel",
        ]
        if backend == "uwrmaxsat":
            if uwrmaxsat_binary is None:
                raise FileNotFoundError("UWrMaxSAT is required by ORG BG-d2")
            command.extend(
                [
                    "--uwrmaxsat-bin",
                    str(uwrmaxsat_binary),
                    "--uwrmaxsat-sha256",
                    uwrmaxsat_sha256,
                ]
            )
        return command, detailed
    if executor == "org_ir":
        if uwrmaxsat_binary is None:
            raise FileNotFoundError("UWrMaxSAT is required by ORG IR")
        command = [
            sys.executable,
            "-u",
            str(PROJECT_ROOT / "src" / "ORG_new.py"),
            "--instance",
            str(instance_path),
            "--timeout",
            str(timeout),
            "--uwrmaxsat-bin",
            str(uwrmaxsat_binary),
            "--uwrmaxsat-sha256",
            uwrmaxsat_sha256,
            "--csv",
            str(detailed),
            "--excel-dir",
            str(temp_dir / "excel"),
        ]
        return command, detailed
    raise AssertionError(executor)


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":  # pragma: no cover - GCP production is Linux.
        process.terminate()
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":  # pragma: no cover
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_job(
    job: dict[str, Any],
    *,
    output_dir: Path,
    timeout: float,
    controller_grace: float,
    uwrmaxsat_binary: Path | None,
    uwrmaxsat_sha256: str,
    attempt: int,
    plan_sha256: str,
    campaign_id: str,
    run_order_seed: int,
) -> dict[str, Any]:
    log_name = (
        f"{job['run_order']:06d}_"
        f"{sha256_text(job['run_key'])[:12]}_attempt-{attempt}.log"
    )
    log_path = output_dir / "logs" / log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    work_root = output_dir / "work"
    work_root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    controller_timeout = False
    return_code: int | None = None
    row: dict[str, Any]
    before = _machine_snapshot()
    with tempfile.TemporaryDirectory(prefix="cell-", dir=work_root) as temporary:
        temp_dir = Path(temporary)
        command, detailed_path = _configuration_command(
            job,
            timeout=timeout,
            temp_dir=temp_dir,
            uwrmaxsat_binary=uwrmaxsat_binary,
            uwrmaxsat_sha256=uwrmaxsat_sha256,
        )
        with log_path.open("w", encoding="utf-8") as log_stream:
            log_stream.write(f"started_utc={utc_now()}\n")
            log_stream.write(f"command={shlex.join(command)}\n\n")
            log_stream.flush()
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=(os.name != "nt"),
            )
            try:
                return_code = process.wait(timeout=timeout + controller_grace)
            except subprocess.TimeoutExpired:
                controller_timeout = True
                _terminate_process_group(process)
                return_code = process.returncode
            except (CampaignInterrupted, KeyboardInterrupt):
                _terminate_process_group(process)
                log_stream.write("\ncell interrupted; no result row committed\n")
                log_stream.flush()
                raise
        if detailed_path.is_file():
            row = _read_single_csv_row(detailed_path)
        elif controller_timeout:
            row = {
                "instance": job["instance"],
                "instance_content_id": job["instance_content_id"],
                "instance_sha256": job["instance_sha256"],
                "base_lineage_id": job["base_lineage_id"],
                "instance_family": job["instance_family"],
                "instance_variant": job["instance_variant"],
                "instance_path": job["instance_path"],
                "status": "TIMEOUT",
                "sat_result": "TIMEOUT",
                "runtime_seconds": timeout,
                "runtime_censored": True,
                "objective_mode": job["configuration"].get("objective_mode", ""),
                "error_type": "ControllerTimeout",
                "error_message": (
                    "cell executor exceeded solver timeout plus controller grace"
                ),
            }
        else:
            row = {
                "instance": job["instance"],
                "instance_content_id": job["instance_content_id"],
                "instance_sha256": job["instance_sha256"],
                "base_lineage_id": job["base_lineage_id"],
                "instance_family": job["instance_family"],
                "instance_variant": job["instance_variant"],
                "instance_path": job["instance_path"],
                "status": "ERROR",
                "sat_result": "ERROR",
                "runtime_seconds": round(time.perf_counter() - started, 6),
                "runtime_censored": False,
                "objective_mode": job["configuration"].get("objective_mode", ""),
                "error_type": "ExecutorFailure",
                "error_message": (
                    f"executor returned {return_code} without a detailed CSV"
                ),
            }
    after = _machine_snapshot()
    row.update(
        {
            "campaign_id": campaign_id,
            "experiment_block": job["experiment_block"],
            "planned_configuration_id": job["planned_configuration_id"],
            "repetition": job["repetition"],
            "run_order": job["run_order"],
            "run_key": job["run_key"],
            "attempt": attempt,
            "run_order_seed": run_order_seed,
            "campaign_plan_sha256": plan_sha256,
            "campaign_executor_seconds": round(time.perf_counter() - started, 6),
            "campaign_log": log_path.relative_to(output_dir).as_posix(),
            "campaign_executor_returncode": return_code,
            "campaign_boot_id": _boot_id(),
            "campaign_load_1m_before": before["load_1m"],
            "campaign_load_5m_before": before["load_5m"],
            "campaign_load_15m_before": before["load_15m"],
            "campaign_available_memory_mb_before": before[
                "available_memory_mb"
            ],
            "campaign_load_1m_after": after["load_1m"],
            "campaign_available_memory_mb_after": after[
                "available_memory_mb"
            ],
        }
    )
    return {
        "run_key": job["run_key"],
        "attempt": attempt,
        "completed_utc": utc_now(),
        "row": row,
    }


def materialize_results(output_dir: Path) -> list[dict[str, Any]]:
    records = _read_jsonl(output_dir / "raw" / "results.jsonl")
    rows = [record["row"] for record in latest_attempts(records).values()]
    rows.sort(key=lambda row: int(row["run_order"]))
    normalized = output_dir / "normalized" / "detailed.csv"
    write_detailed_csv(normalized, rows)
    return rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run deterministic, append-only journal experiment campaigns."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--uwrmaxsat-bin")
    parser.add_argument(
        "--uwrmaxsat-sha256",
        help="required production pin; must match the resolved executable",
    )
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--max-runs", type=int)
    parser.add_argument("--only-block", action="append", default=[])
    args = parser.parse_args(argv)
    if args.max_runs is not None and args.max_runs <= 0:
        parser.error("--max-runs must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config).resolve()
    output_dir = Path(args.output_dir).resolve()
    config = read_config(config_path)
    datasets = resolve_datasets(config)
    plan = build_plan(
        config,
        datasets,
        only_blocks=set(args.only_block) or None,
    )
    existing_plan_path = output_dir / "plan.json"
    if existing_plan_path.is_file():
        with existing_plan_path.open(encoding="utf-8") as stream:
            existing_plan = json.load(stream)
        if existing_plan.get("plan_sha256") != plan["plan_sha256"]:
            raise SystemExit("ERROR: existing campaign plan hash does not match")
        if not args.resume and not args.plan_only:
            raise SystemExit("ERROR: output exists; use --resume for the same plan")
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_json(existing_plan_path, plan)

    print(
        f"campaign={plan['campaign_id']} jobs={plan['job_count']} "
        f"plan_sha256={plan['plan_sha256']}"
    )
    if args.plan_only:
        return 0

    commit, dirty = _git_metadata()
    if dirty and config.get("require_clean_worktree", True) and not args.allow_dirty:
        raise SystemExit(
            "ERROR: production campaign refuses a dirty worktree; commit the "
            "journal implementation or use --allow-dirty only for development"
        )

    profile_errors = machine_profile_errors(plan.get("required_machine"))
    if profile_errors:
        profile_id = plan.get("required_machine", {}).get("profile_id", "production")
        raise SystemExit(
            f"ERROR: machine does not match {profile_id!r}: "
            + "; ".join(profile_errors)
        )

    needs_uwr = any(
        (
            job["configuration"]["executor"] == "org_ir"
            or (
                job["configuration"]["executor"] == "org_bg_d2"
                and job["configuration"].get("backend", "uwrmaxsat")
                == "uwrmaxsat"
            )
            or (
                job["configuration"]["executor"] == "main"
                and job["configuration"].get("solver") == "maxsat"
                and job["configuration"].get("maxsat_backend", "uwrmaxsat")
                == "uwrmaxsat"
            )
        )
        for job in plan["jobs"]
    )
    binary = (
        resolve_uwrmaxsat_binary(args.uwrmaxsat_bin)
        if needs_uwr or args.uwrmaxsat_bin
        else None
    )
    if (needs_uwr or args.uwrmaxsat_bin) and binary is None:
        raise SystemExit("ERROR: campaign requires a pinned UWrMaxSAT executable")
    binary_sha256 = executable_sha256(binary) if binary is not None else ""
    cli_sha256 = str(args.uwrmaxsat_sha256 or "").strip().lower()
    config_sha256 = str(config.get("uwrmaxsat_sha256", "")).strip().lower()
    if cli_sha256 and not re.fullmatch(r"[0-9a-f]{64}", cli_sha256):
        raise SystemExit("ERROR: --uwrmaxsat-sha256 must be 64 lowercase hex digits")
    if cli_sha256 and config_sha256 and cli_sha256 != config_sha256:
        raise SystemExit("ERROR: CLI and config UWrMaxSAT SHA-256 pins disagree")
    expected_sha256 = cli_sha256 or config_sha256
    if expected_sha256 and expected_sha256 != binary_sha256:
        raise SystemExit(
            "ERROR: UWrMaxSAT SHA-256 mismatch: "
            f"expected {expected_sha256}, got {binary_sha256}"
        )

    environment_path = output_dir / "environment.json"
    if environment_path.is_file():
        with environment_path.open(encoding="utf-8") as stream:
            environment = json.load(stream)
        resume_mismatches = []
        if environment.get("plan_sha256") != plan["plan_sha256"]:
            resume_mismatches.append("plan SHA-256")
        if environment.get("git_commit") != commit:
            resume_mismatches.append("git commit")
        if environment.get("config_sha256") != file_sha256(config_path):
            resume_mismatches.append("config SHA-256")
        current_manifest_hashes = {
            _relative_to_project(_resolve_project_path(dataset["manifest"])):
            file_sha256(_resolve_project_path(dataset["manifest"]))
            for dataset in config["datasets"]
        }
        if environment.get("manifest_sha256") != current_manifest_hashes:
            resume_mismatches.append("dataset manifest SHA-256")
        if str(environment.get("uwrmaxsat_sha256", "")) != binary_sha256:
            resume_mismatches.append("UWrMaxSAT SHA-256")
        if resume_mismatches:
            raise SystemExit(
                "ERROR: refusing to mix environments while resuming: "
                + ", ".join(resume_mismatches)
            )
    else:
        environment = build_environment(
            config_path,
            config,
            plan,
            uwrmaxsat_binary=binary,
            command=[sys.executable, str(Path(__file__).resolve()), *(argv or sys.argv[1:])],
        )
        environment["git_commit"] = commit
        environment["git_dirty"] = dirty
        _write_json(environment_path, environment)

    raw_path = output_dir / "raw" / "results.jsonl"
    records = _read_jsonl(raw_path)
    latest = latest_attempts(records)
    completed = {
        run_key
        for run_key, record in latest.items()
        if record["row"].get("status") in TERMINAL_STATUSES
    }
    pending = []
    for job in plan["jobs"]:
        record = latest.get(job["run_key"])
        if job["run_key"] in completed:
            continue
        if record is not None and not args.retry_errors:
            continue
        pending.append(job)
    if args.max_runs is not None:
        pending = pending[: args.max_runs]
    print(
        f"resume_state completed={len(completed)} pending_selected={len(pending)} "
        f"raw_attempts={len(records)}"
    )

    timeout = float(plan["timeout_seconds"])
    controller_grace = float(plan["controller_grace_seconds"])
    interrupted_signal: int | None = None

    def _handle_signal(signum: int, _frame: Any) -> None:
        nonlocal interrupted_signal
        interrupted_signal = signum
        raise CampaignInterrupted(f"received signal {signum}")

    handled_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled_signals.append(signal.SIGHUP)
    previous_handlers = {
        signum: signal.signal(signum, _handle_signal)
        for signum in handled_signals
    }
    for position, job in enumerate(pending, start=1):
        previous = latest.get(job["run_key"])
        attempt = int(previous["attempt"]) + 1 if previous is not None else 1
        print(
            f"[{position}/{len(pending)}] order={job['run_order']} "
            f"block={job['experiment_block']} instance={job['instance']} "
            f"config={job['planned_configuration_id']} rep={job['repetition']}",
            flush=True,
        )
        try:
            record = run_job(
                job,
                output_dir=output_dir,
                timeout=timeout,
                controller_grace=controller_grace,
                uwrmaxsat_binary=binary,
                uwrmaxsat_sha256=binary_sha256,
                attempt=attempt,
                plan_sha256=plan["plan_sha256"],
                campaign_id=plan["campaign_id"],
                run_order_seed=int(plan["run_order_seed"]),
            )
        except (CampaignInterrupted, KeyboardInterrupt):
            rows = materialize_results(output_dir)
            _write_json(
                output_dir / "campaign_status.json",
                {
                    "updated_utc": utc_now(),
                    "plan_sha256": plan["plan_sha256"],
                    "planned_jobs": plan["job_count"],
                    "latest_rows": len(rows),
                    "terminal_rows": sum(
                        row.get("status") in TERMINAL_STATUSES for row in rows
                    ),
                    "error_rows": sum(
                        row.get("status") == "ERROR" for row in rows
                    ),
                    "interrupted": True,
                    "signal": interrupted_signal,
                },
            )
            print("campaign interrupted safely; rerun with --resume", flush=True)
            return 130
        _append_jsonl(raw_path, record)
        latest[job["run_key"]] = record
        status = record["row"].get("status")
        print(
            f"    status={status} vector={record['row'].get('objective_vector')} "
            f"runtime={record['row'].get('runtime_seconds')}",
            flush=True,
        )
    for signum, handler in previous_handlers.items():
        signal.signal(signum, handler)
    rows = materialize_results(output_dir)
    _write_json(
        output_dir / "campaign_status.json",
        {
            "updated_utc": utc_now(),
            "plan_sha256": plan["plan_sha256"],
            "planned_jobs": plan["job_count"],
            "latest_rows": len(rows),
            "terminal_rows": sum(row.get("status") in TERMINAL_STATUSES for row in rows),
            "error_rows": sum(row.get("status") == "ERROR" for row in rows),
        },
    )
    # A retry invocation must not report success merely because it selected no
    # new jobs while an earlier ERROR row is still the latest attempt.
    final_errors = sum(row.get("status") == "ERROR" for row in rows)
    return 2 if final_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
