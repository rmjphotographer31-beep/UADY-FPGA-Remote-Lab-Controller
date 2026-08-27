# UADY FPGA Controller v5.2 — Ephemeral Job Pipeline

## One-use data policy

The following data exists only while its job is active:

- uploaded `.v` / `.sv`
- uploaded `.qsf`
- uploaded `.sof`
- temporary files passed to the C extractor
- C extractor JSON/evidence
- JSON/prompt sent to Qwen
- Qwen raw response
- `core_evidence`

The C extractor communicates through stdout. Its temporary `.v` and `.qsf`
inputs are deleted in a `finally` block. Qwen receives the extracted JSON in
memory and returns only the board decision. No evidence objects are required
from Qwen.

When a job becomes `completed`, `failed`, or `cancelled`, its temporary upload
spool, queued staging cache, and registered ephemeral paths are deleted.
Startup cleanup removes abandoned staging data after the configured TTL.

Permanent history contains metadata only: job ID, status, user, timestamps,
selected board, JTAG cable, filenames, sizes, and the final short message.
Source contents, evidence contents, prompt JSON, and raw AI output are excluded.

## Expected banner

`UADY Pi AI/HAT Dynamic JTAG Controller v5.2 Ephemeral Job Pipeline`
