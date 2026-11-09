# Backup before reusing the part 3 VM

VM: `instance-20261031-120104`, project
`project-4301720b-91e0-476d-bfe`, zone `us-central1-a`.

`b2b-runtime-and-results-20261109.tar.gz` preserves the complete frozen B2B
worktree and the result exports on this VM before cleanup. All 5,697 file hashes
were verified after download. Its archive SHA-256 and file counts are recorded
in `archive.receipt.json`; individual file hashes are in the archive's
`retirement_manifest.json`.

The archive contains:

- `runtime/`: source, configuration, benchmark files, raw JSONL, normalized CSV,
  logs, the original completed shard 1, completed partitions 1/4/7, and stopped
  partition 3 with 672 terminal cells.
- `repo-exports/`: the existing export archives and logs from the main checkout.
- `retirement_manifest.json`: original paths and SHA-256 hashes for each file.

The Git worktree pointer and Python caches are excluded. The worktree's base
commit is `6e5f7d7c5c9f1e3696c74a6d2c86126e4113ce42`. To recover an executable Git
worktree, create a worktree at that commit and copy the extracted `runtime/`
contents into it. The main repository, its Python environment, UWrMaxSAT and
CaDiCaL installations are retained on the VM.

These are backup copies of existing executions, not additional repetitions.
Part 3's final three cells continue on `instance-20261104-105839`, in project
`project-e19a1072-60cf-47ea-a1e`. Do not restart its old worker on this VM.

Cleanup and final VM state are recorded separately in
`cleanup-and-stop.receipt.json` after the archive has been pushed and its remote
source files have been rechecked.

Cleanup is complete. The temporary B2B worktree, its verified export copies,
and the generated solver inputs and helper files in `/tmp` were removed.
The disk had 6,254,747,648 bytes free (about 5.83 GiB) after cleanup. The primary
checkout, Python environment, UWrMaxSAT and CaDiCaL installations remain.
GCP confirmed `TERMINATED` on 9 November 2026; the persistent boot disk remains
attached. Parts 0 and 3 were confirmed running on their other VM after cleanup.
