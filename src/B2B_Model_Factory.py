"""Single construction point for Boolean B2B model families.

The SAT optimizers deliberately depend on this module rather than on a
specific encoder.  Keeping model selection here prevents the optimization
algorithms from drifting when a new compact encoding is introduced.
"""

from __future__ import annotations

from typing import Literal

from B2B_Instance import B2BInstance, B2BSATModel, VALID_OBJECTIVE_MODES
from B2B_Instance_CompactV2 import (
    B2BSATModelV2,
    VALID_BG_COUNTER_MODES,
    VALID_CAPACITY_CARDINALITY,
    VALID_CAPACITY_MODES,
    VALID_V2_OBJECTIVE_MODES,
    VALID_V2_PRECEDENCE_ENCODINGS,
)


ModelFamily = Literal["compact", "compact_v2"]
VALID_MODEL_FAMILIES = {"compact", "compact_v2"}


def validate_boolean_configuration(
    *,
    model_family: str = "compact",
    objective_mode: str = "ir",
    precedence_encoding: str | None = None,
    capacity_mode: str | None = None,
    capacity_cardinality: str = "seqcounter",
    bg_counter_mode: str = "shared_dp",
    idle_sla_threshold: int = 2,
    hybrid_suffix_density: float = 0.70,
) -> None:
    """Validate family-specific Boolean options before any solver is built."""

    if model_family not in VALID_MODEL_FAMILIES:
        raise ValueError(
            f"Unknown model_family={model_family!r}; expected one of "
            f"{sorted(VALID_MODEL_FAMILIES)}"
        )
    objectives = (
        VALID_OBJECTIVE_MODES
        if model_family == "compact"
        else VALID_V2_OBJECTIVE_MODES
    )
    if objective_mode not in objectives:
        raise ValueError(
            f"objective_mode={objective_mode!r} is not supported by "
            f"model_family={model_family!r}; expected one of {sorted(objectives)}"
        )
    if model_family == "compact":
        if precedence_encoding == "hybrid_suffix":
            raise ValueError(
                "precedence_encoding='hybrid_suffix' requires "
                "model_family='compact_v2'"
            )
        if capacity_mode is not None:
            raise ValueError("capacity_mode requires model_family='compact_v2'")
        return

    if (
        precedence_encoding is not None
        and precedence_encoding not in VALID_V2_PRECEDENCE_ENCODINGS
    ):
        raise ValueError(f"Unknown precedence_encoding={precedence_encoding!r}")
    if capacity_mode is not None and capacity_mode not in VALID_CAPACITY_MODES:
        raise ValueError(f"Unknown capacity_mode={capacity_mode!r}")
    if capacity_cardinality not in VALID_CAPACITY_CARDINALITY:
        raise ValueError(
            f"Unknown capacity_cardinality={capacity_cardinality!r}"
        )
    if bg_counter_mode not in VALID_BG_COUNTER_MODES:
        raise ValueError(f"Unknown bg_counter_mode={bg_counter_mode!r}")
    if idle_sla_threshold < 0:
        raise ValueError("idle_sla_threshold must be non-negative")
    if not 0.0 <= hybrid_suffix_density <= 1.0:
        raise ValueError("hybrid_suffix_density must be in [0,1]")


def create_boolean_model(
    inst: B2BInstance,
    precedence_mode: str | None = None,
    encoding_variant: str = "imp12+",
    domain_mode: str = "reduced",
    *,
    model_family: str = "compact",
    precedence_encoding: str | None = None,
    precedence_graph: str | None = None,
    domain_filter_graph: str = "distance_closure",
    objective_mode: str = "ir",
    capacity_mode: str | None = None,
    capacity_cardinality: str = "seqcounter",
    bg_counter_mode: str = "shared_dp",
    idle_sla_threshold: int = 2,
    hybrid_suffix_density: float = 0.70,
) -> B2BSATModel:
    """Build Compact or Compact-v2 with one backward-compatible interface."""

    validate_boolean_configuration(
        model_family=model_family,
        objective_mode=objective_mode,
        precedence_encoding=precedence_encoding,
        capacity_mode=capacity_mode,
        capacity_cardinality=capacity_cardinality,
        bg_counter_mode=bg_counter_mode,
        idle_sla_threshold=idle_sla_threshold,
        hybrid_suffix_density=hybrid_suffix_density,
    )
    shared = dict(
        inst=inst,
        precedence_mode=precedence_mode,
        precedence_encoding=precedence_encoding,
        precedence_graph=precedence_graph,
        encoding_variant=encoding_variant,
        domain_mode=domain_mode,
        domain_filter_graph=domain_filter_graph,
        objective_mode=objective_mode,
    )
    if model_family == "compact":
        return B2BSATModel(**shared)
    return B2BSATModelV2(
        **shared,
        capacity_mode=capacity_mode,
        capacity_cardinality=capacity_cardinality,
        bg_counter_mode=bg_counter_mode,
        idle_sla_threshold=idle_sla_threshold,
        hybrid_suffix_density=hybrid_suffix_density,
    )


def boolean_model_metadata(model: B2BSATModel) -> dict[str, object]:
    """Expose configuration metadata without duplicating V2 internals."""

    metadata: dict[str, object] = {
        "model_family": "compact_v2" if isinstance(model, B2BSATModelV2) else "compact"
    }
    if isinstance(model, B2BSATModelV2):
        metadata.update(model.compact_v2_metadata())
    return metadata
