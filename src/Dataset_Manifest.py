from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from B2B_Instance import read_instance


DATASET_SOURCE_PAGE = "https://imae.udg.edu/Recerca/LAI/"
DATASET_ARCHIVE_URL = "https://ima.udg.edu/Recerca/lai/b2b/b2b.zip"
REPOSITORY_DATA_DIRECTORIES = (
    "data_table03_origin",
    "data_table06_forb",
    "data_table07_fixed",
    "data_table08_prec",
)
MANIFEST_FIELDS = (
    "content_id",
    "base_lineage_id",
    "sha256",
    "canonical_instance",
    "canonical_run_path",
    "family",
    "variant",
    "source_alias_count",
    "source_alias_paths",
    "repository_alias_count",
    "repository_alias_paths",
    "n_business",
    "n_meetings",
    "n_tables",
    "n_total_slots",
    "n_morning_slots",
    "n_forbidden_assignments",
    "n_fixed_meetings",
    "n_direct_precedence_edges",
    "dataset_source_page",
    "dataset_archive_url",
    "dataset_archive_sha256",
)


def base_lineage_id(instance_name: str) -> str:
    """Return the source-instance lineage shared by all constraint variants."""

    name = Path(instance_name).name
    if name.lower().endswith(".dzn"):
        name = name[:-4]
    base = re.sub(
        r"\.(?:original|forb\d+|fixed\d+-forb|prec\d+)$",
        "",
        name,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    if not normalized:
        raise ValueError(f"Cannot derive base lineage from {instance_name!r}")
    return f"b2b-lineage-{normalized}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_instance_name(name: str) -> tuple[str, str]:
    lower = name.lower()
    if lower.endswith(".original.dzn"):
        return "original", "original"
    if ".forb0003.dzn" in lower:
        return "forbidden", "forb0003"
    if ".forb0007.dzn" in lower:
        return "forbidden", "forb0007"
    if ".fixed020-forb.dzn" in lower:
        return "fixed", "fixed020"
    if ".fixed040-forb.dzn" in lower:
        return "fixed", "fixed040"
    precedence_match = re.search(r"\.prec(\d+)\.dzn$", lower)
    if precedence_match:
        return "precedence", f"prec{precedence_match.group(1)}"
    return "unknown", "unknown"


def _relative(path: Path, base: Path) -> str:
    return Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()


def _repository_aliases(repository_root: Path) -> dict[str, list[Path]]:
    aliases: dict[str, list[Path]] = defaultdict(list)
    for directory_name in REPOSITORY_DATA_DIRECTORIES:
        directory = repository_root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.dzn")):
            aliases[file_sha256(path)].append(path)
    return aliases


def _preferred_run_path(
    source_path: Path,
    family: str,
    repository_root: Path,
    repository_aliases: list[Path],
) -> Path:
    preferred_directory = {
        "original": "data_table03_origin",
        "forbidden": "data_table06_forb",
        "fixed": "data_table07_fixed",
        "precedence": "data_table08_prec",
    }.get(family)
    if preferred_directory:
        preferred = repository_root / preferred_directory / source_path.name
        if preferred.is_file() and preferred.read_bytes() == source_path.read_bytes():
            return preferred
    if repository_aliases:
        return sorted(repository_aliases)[0]
    return source_path


def build_manifest_rows(
    source_directory: str | Path,
    repository_root: str | Path,
) -> list[dict[str, Any]]:
    source_root = Path(source_directory).resolve()
    repo_root = Path(repository_root).resolve()
    source_paths = sorted(source_root.glob("*.dzn"))
    if not source_paths:
        raise FileNotFoundError(f"No official .dzn files found in {source_root}")

    source_by_hash: dict[str, list[Path]] = defaultdict(list)
    for path in source_paths:
        source_by_hash[file_sha256(path)].append(path)

    repository_by_hash = _repository_aliases(repo_root)
    archive = repo_root / "b2b.zip"
    archive_sha256 = file_sha256(archive) if archive.is_file() else ""

    rows: list[dict[str, Any]] = []
    for digest, aliases in sorted(source_by_hash.items()):
        canonical_source = sorted(aliases, key=lambda path: path.name)[0]
        families = sorted({classify_instance_name(path.name)[0] for path in aliases})
        variants = sorted({classify_instance_name(path.name)[1] for path in aliases})
        family = "|".join(families)
        variant = "|".join(variants)
        parsed = read_instance(canonical_source)
        repository_aliases = repository_by_hash.get(digest, [])
        run_path = _preferred_run_path(
            canonical_source,
            families[0],
            repo_root,
            repository_aliases,
        )
        rows.append(
            {
                "content_id": f"b2b-{digest[:16]}",
                "base_lineage_id": base_lineage_id(canonical_source.name),
                "sha256": digest,
                "canonical_instance": canonical_source.stem,
                "canonical_run_path": _relative(run_path, repo_root),
                "family": family,
                "variant": variant,
                "source_alias_count": len(aliases),
                "source_alias_paths": " | ".join(
                    _relative(path, repo_root) for path in sorted(aliases)
                ),
                "repository_alias_count": len(repository_aliases),
                "repository_alias_paths": " | ".join(
                    _relative(path, repo_root)
                    for path in sorted(repository_aliases)
                ),
                "n_business": parsed.n_business,
                "n_meetings": parsed.n_meetings,
                "n_tables": parsed.n_tables,
                "n_total_slots": parsed.n_total_slots,
                "n_morning_slots": parsed.n_morning_slots,
                "n_forbidden_assignments": sum(
                    len(slots) for slots in parsed.forbidden
                ),
                "n_fixed_meetings": sum(
                    slot is not None for slot in parsed.fixed
                ),
                "n_direct_precedence_edges": sum(
                    len(predecessors) for predecessors in parsed.precedences
                ),
                "dataset_source_page": DATASET_SOURCE_PAGE,
                "dataset_archive_url": DATASET_ARCHIVE_URL,
                "dataset_archive_sha256": archive_sha256,
            }
        )
    return rows


def write_manifest(path: str | Path, rows: Iterable[dict[str, Any]]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=MANIFEST_FIELDS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    return destination


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=(
            "Build the canonical SHA-256 manifest for the official UdG B2B "
            "benchmark archive."
        )
    )
    parser.add_argument(
        "--source-dir",
        default=str(project_root.parent / "noves"),
        help="directory extracted from the official b2b.zip archive",
    )
    parser.add_argument(
        "--repository-root",
        default=str(project_root),
    )
    parser.add_argument(
        "--output",
        default=str(project_root / "instances_manifest.csv"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows = build_manifest_rows(args.source_dir, args.repository_root)
    output = write_manifest(args.output, rows)
    source_aliases = sum(int(row["source_alias_count"]) for row in rows)
    repository_aliases = sum(int(row["repository_alias_count"]) for row in rows)
    print(
        f"Wrote {output}: {len(rows)} distinct contents, "
        f"{source_aliases} official aliases, {repository_aliases} repository paths"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
