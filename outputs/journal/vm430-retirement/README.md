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
contents into it. The initial cleanup retained the main repository and its
Python environment. Both were subsequently deleted at the author's request,
as recorded in `repository-removal.receipt.json`. Standalone UWrMaxSAT and
CaDiCaL directories remain outside the removed checkout.

These are backup copies of existing executions, not additional repetitions.
Part 3's final three cells continue on `instance-20261104-105839`, in project
`project-e19a1072-60cf-47ea-a1e`. Do not restart its old worker on this VM.

Cleanup and final VM state are recorded separately in
`cleanup-and-stop.receipt.json` after the archive has been pushed and its remote
source files have been rechecked.

The first cleanup removed the temporary B2B worktree, its verified export
copies, and generated solver inputs and helper files in `/tmp`. The disk then
had 6,254,747,648 bytes free (about 5.83 GiB), as recorded in
`cleanup-and-stop.receipt.json`.

At the author's subsequent request, the VM was temporarily started and
`/home/longkevin05062006/Business-Meeting-Scheduling-Problem` was removed,
including its `.venv-ubuntu` environment and machine-specific experiment
configuration. There were no modified tracked files or dependent worktrees.
This freed another 418,287,616 bytes, leaving 6,674,677,760 bytes free (about
6.22 GiB). The persistent boot disk remains attached. The VM was stopped again;
the verified final state is in `repository-removal.receipt.json`.

Parts 0 and 3 run on `instance-20261104-105839` in the other project. Deletion
on the retired VM does not change their runtime or output directories.
