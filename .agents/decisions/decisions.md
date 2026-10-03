# Recorded decisions

## Safety-first refresh

The source file is replaced only after source validation, temporary-copy
validation, SHA-256 comparison, and atomic replacement succeed. The program
must prefer failure over guessing about ambiguous source state.

## Metadata-only inventory

Starting a run records metadata rather than hashing the entire HDD. The source
hash is computed immediately before processing each individual file.

## SQLite as state authority

SQLite is the authoritative record for runs, directory jobs, files, failures,
and checkpoints. Store it on a local/internal drive rather than the refreshed
HDD.

## Independent runs and one worker

Every `start` creates a new inventory independent of prior runs. `resume`
continues only the selected run. Processing remains single-worker; `--jobs`
limits sequential directory jobs and does not request parallel work.
