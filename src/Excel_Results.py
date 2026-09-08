from __future__ import annotations

import math
import os
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape


RUNTIME_SCOPE = (
    "wall-clock from immediately before reading/parsing the .dzn instance "
    "through model construction, optimization, solution decoding, and "
    "independent validation; excludes worker-process startup and result-file export"
)
FORMULA_SCOPE = (
    "formalism-specific model size: SAT/MaxSAT reports base CNF and soft "
    "clauses, MIP reports variables/linear constraints/nonzeros, and CP reports "
    "integer variables plus linear/global constraints; SAT bound/totalizer "
    "overhead remains separate in optimizer_added_*"
)


@dataclass(frozen=True)
class ResultColumn:
    key: str
    number_style: str = "text"


RESULT_COLUMNS = (
    ResultColumn("campaign_id"),
    ResultColumn("experiment_block"),
    ResultColumn("planned_configuration_id"),
    ResultColumn("repetition", "integer"),
    ResultColumn("run_order", "integer"),
    ResultColumn("run_key"),
    ResultColumn("attempt", "integer"),
    ResultColumn("run_order_seed", "integer"),
    ResultColumn("campaign_plan_sha256"),
    ResultColumn("instance"),
    ResultColumn("instance_content_id"),
    ResultColumn("base_lineage_id"),
    ResultColumn("instance_family"),
    ResultColumn("configuration_label"),
    ResultColumn("factor_m"),
    ResultColumn("factor_f"),
    ResultColumn("factor_p"),
    ResultColumn("factor_g"),
    ResultColumn("factor_b"),
    ResultColumn("factor_o"),
    ResultColumn("factor_s"),
    ResultColumn("factor_i"),
    ResultColumn("status"),
    ResultColumn("sat_result"),
    ResultColumn("best_value", "integer"),
    ResultColumn("proven_optimum", "integer"),
    ResultColumn("objective_mode"),
    ResultColumn("objective_vector"),
    ResultColumn("proven_objective_vector"),
    ResultColumn("primary_objective_value", "integer"),
    ResultColumn("secondary_objective_value", "integer"),
    ResultColumn("tertiary_objective_value", "integer"),
    ResultColumn("lexicographic_scalar_cost", "integer"),
    ResultColumn("objective_tier_weights"),
    ResultColumn("runtime_seconds", "seconds"),
    ResultColumn("runtime_censored", "boolean"),
    ResultColumn("peak_memory_mb", "decimal"),
    ResultColumn("formalism"),
    ResultColumn("model_family"),
    ResultColumn("capacity_mode"),
    ResultColumn("capacity_cardinality"),
    ResultColumn("bg_counter_mode"),
    ResultColumn("idle_sla_threshold", "integer"),
    ResultColumn("hybrid_suffix_density", "decimal"),
    ResultColumn("precedence_dense_suffix_meetings", "integer"),
    ResultColumn("precedence_dense_suffix_clauses", "integer"),
    ResultColumn("formulation_name"),
    ResultColumn("model_family_display_name"),
    ResultColumn("model_configuration_display_name"),
    ResultColumn("implementation_provenance"),
    ResultColumn("optimization_procedure_display_name"),
    ResultColumn("n_vars", "integer"),
    ResultColumn("n_binary_variables", "integer"),
    ResultColumn("n_integer_variables", "integer"),
    ResultColumn("n_continuous_variables", "integer"),
    ResultColumn("n_linear_constraints", "integer"),
    ResultColumn("n_global_constraints", "integer"),
    ResultColumn("n_nonzeros", "integer"),
    ResultColumn("n_hard_clauses", "integer"),
    ResultColumn("n_soft_clauses", "integer"),
    ResultColumn("n_total_clauses", "integer"),
    ResultColumn("n_primary_variables", "integer"),
    ResultColumn("n_auxiliary_variables", "integer"),
    ResultColumn("n_total_literals", "integer"),
    ResultColumn("max_hard_clause_length", "integer"),
    ResultColumn("configuration_id"),
    ResultColumn("configuration_key"),
    ResultColumn("domain_mode"),
    ResultColumn("domain_filter_graph"),
    ResultColumn("precedence_encoding"),
    ResultColumn("precedence_graph"),
    ResultColumn("optimization_engine"),
    ResultColumn("solver_backend"),
    ResultColumn("solver_version"),
    ResultColumn("encoding_variant"),
    ResultColumn("idle_encoding"),
    ResultColumn("objective"),
    ResultColumn("objective_code"),
    ResultColumn("implied_constraints_code"),
    ResultColumn("n_hard_literals", "integer"),
    ResultColumn("n_soft_literals", "integer"),
    ResultColumn("max_soft_clause_length", "integer"),
    ResultColumn("n_unit_hard_clauses", "integer"),
    ResultColumn("n_binary_hard_clauses", "integer"),
    ResultColumn("n_ternary_hard_clauses", "integer"),
    ResultColumn("n_long_hard_clauses", "integer"),
    ResultColumn("soft_clause_weight", "integer"),
    ResultColumn("soft_weight_sum", "integer"),
    ResultColumn("n_objective_lits", "integer"),
    ResultColumn("n_optimizer_calls", "integer"),
    ResultColumn("best_bound", "decimal"),
    ResultColumn("optimality_gap", "decimal"),
    ResultColumn("branch_and_bound_nodes", "decimal"),
    ResultColumn("cp_branches", "integer"),
    ResultColumn("cp_fails", "integer"),
    ResultColumn("n_bound_encodings", "integer"),
    ResultColumn("optimizer_added_variables_peak", "integer"),
    ResultColumn("optimizer_added_clauses_peak", "integer"),
    ResultColumn("optimizer_added_literals_peak", "integer"),
    ResultColumn("optimizer_added_clauses_cumulative", "integer"),
    ResultColumn("objective_phase_seconds"),
    ResultColumn("objective_phase_calls"),
    ResultColumn("formula_scope"),
    ResultColumn("full_schedule_candidates", "integer"),
    ResultColumn("unary_eligible_schedule_candidates", "integer"),
    ResultColumn("reduced_schedule_candidates", "integer"),
    ResultColumn("active_schedule_candidates", "integer"),
    ResultColumn("precedence_direct_edges", "integer"),
    ResultColumn("precedence_closure_edges", "integer"),
    ResultColumn("precedence_relation_edges", "integer"),
    ResultColumn("precedence_max_distance", "integer"),
    ResultColumn("precedence_pairwise_clauses", "integer"),
    ResultColumn("precedence_sparse_link_clauses", "integer"),
    ResultColumn("precedence_unique_suffix_cuts", "integer"),
    ResultColumn("domain_filter_iterations", "integer"),
    ResultColumn("domain_filter_seconds", "seconds"),
    ResultColumn("input_parsing_seconds", "seconds"),
    ResultColumn("model_construction_seconds", "seconds"),
    ResultColumn("backend_model_construction_seconds", "seconds"),
    ResultColumn("model_build_seconds", "seconds"),
    ResultColumn("solve_and_validate_seconds", "seconds"),
    ResultColumn("runtime_scope"),
    ResultColumn("idle_range_pstar", "integer"),
    ResultColumn("total_internal_idle_slots", "integer"),
    ResultColumn("all_participant_idle_range", "integer"),
    ResultColumn("total_break_groups", "integer"),
    ResultColumn("break_group_range", "integer"),
    ResultColumn("participant_break_groups"),
    ResultColumn("memory_metric"),
    ResultColumn("instance_sha256"),
    ResultColumn("instance_variant"),
    ResultColumn("instance_path"),
    ResultColumn("source_alias_count", "integer"),
    ResultColumn("source_alias_paths"),
    ResultColumn("repository_alias_count", "integer"),
    ResultColumn("repository_alias_paths"),
    ResultColumn("dataset_source_page"),
    ResultColumn("dataset_archive_url"),
    ResultColumn("dataset_archive_sha256"),
    ResultColumn("run_started_utc"),
    ResultColumn("timeout_seconds", "seconds"),
    ResultColumn("memory_limit_mb", "decimal"),
    ResultColumn("git_commit"),
    ResultColumn("git_dirty", "boolean"),
    ResultColumn("python_version"),
    ResultColumn("pysat_version"),
    ResultColumn("hostname"),
    ResultColumn("platform"),
    ResultColumn("cpu_model"),
    ResultColumn("physical_cpu_cores", "integer"),
    ResultColumn("logical_cpu_cores", "integer"),
    ResultColumn("system_memory_mb", "decimal"),
    ResultColumn("threads", "integer"),
    ResultColumn("random_seed", "integer"),
    ResultColumn("runner_command"),
    ResultColumn("solver_binary"),
    ResultColumn("solver_binary_sha256"),
    ResultColumn("solver_command"),
    ResultColumn("validation_errors"),
    ResultColumn("solver_message"),
    ResultColumn("error_type"),
    ResultColumn("error_message"),
)


README_ROWS = (
    ("Workbook grain", "One Results row per instance x configuration run."),
    ("runtime_seconds", RUNTIME_SCOPE),
    (
        "Timeout runtime",
        "For TIMEOUT, runtime_seconds is a right-censored controller cutoff and "
        "runtime_censored is TRUE; no completed result exists at that time.",
    ),
    (
        "model_build_seconds",
        "input_parsing_seconds + model_construction_seconds.",
    ),
    (
        "input_parsing_seconds",
        "Reading and parsing the .dzn input inside the timed worker.",
    ),
    (
        "model_construction_seconds",
        "Domain/graph preprocessing and construction of the shared base "
        "formula or solver-neutral exact-model IR.",
    ),
    (
        "domain_filter_iterations",
        "Complete propagation passes, including the final pass that confirms "
        "the exact preprocessing fixpoint.",
    ),
    (
        "domain_filter_seconds",
        "Wall-clock time spent only in exact domain reduction; graph "
        "construction and CNF construction are excluded.",
    ),
    (
        "backend_model_construction_seconds",
        "For MIP/CP only: translation of the shared IR/specification into the "
        "commercial solver API; included in runtime_seconds and "
        "solve_and_validate_seconds.",
    ),
    (
        "solve_and_validate_seconds",
        "Optimization followed by solution decoding and independent validation.",
    ),
    ("Formula counts", FORMULA_SCOPE),
    (
        "MaxSAT clauses",
        "n_hard_clauses and n_soft_clauses are reported separately; "
        "n_total_clauses is their sum.",
    ),
    (
        "SAT clauses",
        "n_soft_clauses is zero. Base counts are comparable across engines; "
        "optimizer_added_* records bound/totalizer overhead separately.",
    ),
    (
        "MIP size",
        "n_binary_variables, n_integer_variables, n_linear_constraints, and "
        "n_nonzeros describe the shared MIP-SpanRange model.",
    ),
    (
        "CP size",
        "n_integer_variables counts meeting-time variables; "
        "n_global_constraints counts all_diff and count constraints.",
    ),
    (
        "Configuration key",
        "configuration_id/configuration_key contains the M/F/P/G/B/O/S/I "
        "factors and the exact backend; legacy Filter-E* identifiers remain "
        "stable, while new Filter-E identifiers include f-direct explicitly. "
        "configuration_label is a compact display alias.",
    ),
    (
        "Dataset identity",
        "instance_content_id and instance_sha256 identify canonical content; "
        "source_alias_paths preserves all official UdG archive names.",
    ),
    (
        "best_value",
        "Best IdleRange(P*) value returned by the selected engine; blank when no "
        "incumbent was returned.",
    ),
    (
        "proven_optimum",
        "Populated only when optimality was proved; blank for TIMEOUT or ERROR.",
    ),
)


def _column_name(index: int) -> str:
    """Return a one-based Excel column name."""

    if index < 1:
        raise ValueError("Excel column indexes are one-based")
    chars: list[str] = []
    while index:
        index, remainder = divmod(index - 1, 26)
        chars.append(chr(ord("A") + remainder))
    return "".join(reversed(chars))


def _style_id(number_style: str, *, header: bool = False) -> int:
    if header:
        return 1
    return {
        "text": 0,
        "integer": 2,
        "decimal": 3,
        "seconds": 4,
        "boolean": 0,
    }[number_style]


def _display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def _cell_xml(reference: str, value: Any, style_id: int) -> str:
    style = f' s="{style_id}"' if style_id else ""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return f'<c r="{reference}"{style}/>'
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"{style}><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{reference}"{style}><v>{value}</v></c>'

    text = str(value)
    if len(text) > 32767:
        text = text[:32764] + "..."
    preserve = ' xml:space="preserve"' if text != text.strip() else ""
    return (
        f'<c r="{reference}" t="inlineStr"{style}><is><t{preserve}>'
        f"{escape(text)}</t></is></c>"
    )


def _sheet_xml(
    rows: list[list[Any]],
    styles: list[list[int]],
    widths: list[float],
    *,
    freeze_header: bool,
    add_filter: bool,
) -> str:
    if not rows or not rows[0]:
        raise ValueError("A worksheet must contain at least one populated cell")
    row_count = len(rows)
    column_count = len(rows[0])
    last_cell = f"{_column_name(column_count)}{row_count}"

    cols = "".join(
        f'<col min="{index}" max="{index}" width="{width:.1f}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )
    xml_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells = "".join(
            _cell_xml(
                f"{_column_name(column_index)}{row_index}",
                value,
                styles[row_index - 1][column_index - 1],
            )
            for column_index, value in enumerate(row, start=1)
        )
        height = ' ht="24" customHeight="1"' if row_index == 1 else ""
        xml_rows.append(f'<row r="{row_index}"{height}>{cells}</row>')

    if freeze_header:
        sheet_view = (
            '<sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
            "</sheetView></sheetViews>"
        )
    else:
        sheet_view = "<sheetViews><sheetView workbookViewId=\"0\"/></sheetViews>"

    auto_filter = f'<autoFilter ref="A1:{last_cell}"/>' if add_filter else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_cell}"/>{sheet_view}'
        '<sheetFormatPr defaultRowHeight="16"/>'
        f"<cols>{cols}</cols><sheetData>{''.join(xml_rows)}</sheetData>"
        f"{auto_filter}</worksheet>"
    )


def _column_widths(rows: Iterable[list[Any]], *, maximum: float = 42.0) -> list[float]:
    materialized = list(rows)
    if not materialized:
        return []
    return [
        min(
            maximum,
            max(9.0, max(len(_display_value(row[column])) for row in materialized) + 2),
        )
        for column in range(len(materialized[0]))
    ]


def _result_column_widths(rows: list[list[Any]]) -> list[float]:
    widths = _column_widths(rows)
    wide_text = {
        "configuration_id",
        "configuration_key",
        "instance_path",
        "source_alias_paths",
        "repository_alias_paths",
        "runner_command",
        "solver_command",
        "solver_message",
        "validation_errors",
        "error_message",
    }
    exact_hashes = {
        "instance_sha256",
        "dataset_archive_sha256",
        "solver_binary_sha256",
        "git_commit",
    }
    for index, column in enumerate(RESULT_COLUMNS):
        if column.key in wide_text:
            widths[index] = 80.0
        elif column.key in exact_hashes:
            widths[index] = 68.0
        elif column.key == "configuration_label":
            widths[index] = 36.0
    return widths


def _styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="2">
    <numFmt numFmtId="164" formatCode="0.000"/>
    <numFmt numFmtId="165" formatCode="0.000000"/>
  </numFmts>
  <fonts count="2">
    <font><sz val="11"/><name val="Aptos"/><family val="2"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Aptos Display"/><family val="2"/></font>
  </fonts>
  <fills count="3">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border><left/><right/><top/><bottom style="thin"><color rgb="FFD9E2F3"/></bottom><diagonal/></border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="5">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top"/></xf>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>
    <xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _write_zip_entry(archive: zipfile.ZipFile, name: str, content: str) -> None:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, content.encode("utf-8"))


def write_instance_workbook(
    path: str | Path,
    instance_name: str,
    results: list[dict[str, Any]],
) -> Path:
    """Atomically write one dependency-free XLSX workbook for an instance."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    headers = [column.key for column in RESULT_COLUMNS]
    result_rows = [
        headers,
        *[
            [result.get(column.key) for column in RESULT_COLUMNS]
            for result in results
        ],
    ]
    result_styles = [
        [_style_id("text", header=True) for _ in RESULT_COLUMNS],
        *[
            [_style_id(column.number_style) for column in RESULT_COLUMNS]
            for _ in results
        ],
    ]
    result_widths = _result_column_widths(result_rows)

    readme_rows: list[list[Any]] = [
        ["field", "definition"],
        ["instance", instance_name],
        *[list(row) for row in README_ROWS],
    ]
    readme_styles = [
        [_style_id("text", header=True), _style_id("text", header=True)],
        *[[0, 0] for _ in readme_rows[1:]],
    ]
    readme_widths = [26.0, 100.0]

    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""
    root_relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <bookViews><workbookView activeTab="0"/></bookViews>
  <sheets>
    <sheet name="Results" sheetId="1" r:id="rId1"/>
    <sheet name="README" sheetId="2" r:id="rId2"/>
  </sheets>
</workbook>"""
    workbook_relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
    core_properties = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{escape(instance_name)} benchmark results</dc:title>
  <dc:creator>B2B benchmark runner</dc:creator>
  <cp:lastModifiedBy>B2B benchmark runner</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>
</cp:coreProperties>"""
    app_properties = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>B2B benchmark runner</Application>
  <AppVersion>1.0</AppVersion>
</Properties>"""

    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.stem}.",
            suffix=".xlsx",
            delete=False,
        ) as temp_stream:
            temp_name = temp_stream.name
        with zipfile.ZipFile(temp_name, "w") as archive:
            _write_zip_entry(archive, "[Content_Types].xml", content_types)
            _write_zip_entry(archive, "_rels/.rels", root_relationships)
            _write_zip_entry(archive, "docProps/core.xml", core_properties)
            _write_zip_entry(archive, "docProps/app.xml", app_properties)
            _write_zip_entry(archive, "xl/workbook.xml", workbook)
            _write_zip_entry(
                archive,
                "xl/_rels/workbook.xml.rels",
                workbook_relationships,
            )
            _write_zip_entry(archive, "xl/styles.xml", _styles_xml())
            _write_zip_entry(
                archive,
                "xl/worksheets/sheet1.xml",
                _sheet_xml(
                    result_rows,
                    result_styles,
                    result_widths,
                    freeze_header=True,
                    add_filter=True,
                ),
            )
            _write_zip_entry(
                archive,
                "xl/worksheets/sheet2.xml",
                _sheet_xml(
                    readme_rows,
                    readme_styles,
                    readme_widths,
                    freeze_header=True,
                    add_filter=False,
                ),
            )
        os.replace(temp_name, destination)
        temp_name = None
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)

    return destination


def safe_workbook_name(instance_name: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid else char for char in instance_name)
    cleaned = cleaned.strip(" .") or "instance"
    return f"{cleaned[:180]}.xlsx"
