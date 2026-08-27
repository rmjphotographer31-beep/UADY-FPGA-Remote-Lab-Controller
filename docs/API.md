# Raspberry Pi HTTP API

Default base URL:

```text
http://<pi-host>:5050
```

## Authentication

The Pi auto-generates an API key in protected Pi storage. When a key exists, the Flask `before_request` hook requires it for every non-OPTIONS route. The GUI sends it as:

```http
X-API-Key: <pi-api-key>
```

`X-PI-KEY` and an `api_key` query parameter are also recognized by the server, but headers are preferred. Do not put keys in shared URLs or logs.

All JSON responses include server timing/sync headers when possible:

- `X-UADY-Server-Time-Ns`
- `X-UADY-Sync-Mode: event-driven-low-latency`

## Main endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/` | Controller identity and endpoint index |
| GET | `/healthz` | Liveness |
| GET | `/sync/ping` | Lightweight Pi timestamp/sync probe |
| GET | `/status` | Controller/runtime/load status |
| GET | `/jtag` | JTAG discovery/cache; `?force=1` forces scan |
| POST | `/jtag/prewarm_now` | Administrative manual prewarm |
| GET | `/jtag/prewarm_status` | Prewarm daemon state |
| GET | `/boards` | Board/JTAG slot status; `?force=1` forces refresh |
| POST | `/ai/classify_board` | Classify Verilog/QSF evidence |
| POST | `/ai/select_board` | Classification + board-selection context |
| GET | `/ai/ollama_status` | Local Ollama/model reachability |
| POST | `/jtag/instance/action` | Enable/disable/control a physical JTAG instance |
| GET | `/server/projects` | List remote server projects by family |
| GET | `/diagnostics/load` | Controller load diagnostics |
| GET | `/diagnostics/dynamic_config` | Derived runtime limits |
| POST | `/diagnostics/audit_now` | Trigger resource/state audit |
| GET | `/diagnostics/classroom_load` | Classroom/load summary |
| GET | `/queue` | Queue snapshot |
| GET | `/stream/queue` | Server-Sent Events queue stream |
| POST | `/queue/prequeue_upload` | Admit/create Job ID before large upload |
| POST | `/queue/<job_id>/upload_files` | Upload job `.v/.sv`, `.qsf`, `.sof` |
| POST | `/queue/<job_id>/upload_failed` | Mark a failed upload |
| POST | `/queue/upload_failed_batch` | Batch upload-failure reporting |
| GET | `/queue/<job_id>/verify_sof` | Verify staged SOF integrity |
| GET | `/queue/<job_id>` | Read one job |
| POST | `/queue/<job_id>/cancel` | Cancel one authorized job |
| POST | `/queue/cancel_batch` | Cancel multiple authorized jobs |
| POST | `/queue/deploy` | Deploy/enqueue alternate/legacy job payload |
| POST | `/queue/<job_id>/archive_retry_now` | Retry history archive for a job |
| POST | `/queue/stage_cleanup_now` | Stage cleanup action |
| POST | `/queue/kick_now` | Wake/run queue dispatch cycle |
| POST | `/diagnostics/cleanup_temp` | Temporary-file cleanup |
| GET | `/security/terminal_key_status` | Key-management status only; never returns key |
| POST | `/security/verify_terminal_key` | Verify GUI terminal access key |

## Recommended submission sequence

```text
POST /queue/prequeue_upload
        ↓ real job_id
POST /queue/<job_id>/upload_files
        ↓
GET /stream/queue   (persistent UI updates)
        ↓
POST /queue/<job_id>/cancel   (if requested)
```

The GUI already implements this sequence; direct API clients should copy its semantics rather than bypassing prequeue admission.

## Concurrency control

Requests are classified as read, write, upload, AI, or stream. Adaptive limits are derived from runtime resources/slots, with explicit minimums and safe throttling responses. AI concurrency remains intentionally low because local model inference is resource-heavy on the Pi.

## Submission payload example

The GUI sends metadata first, before the multipart file body. A representative prequeue request is:

```json
{
  "requested_board": null,
  "client_hostname": "student-workstation",
  "student_ip": "<client-ip>",
  "priority": 1,
  "priority_label": "Student",
  "priority_role": "Student",
  "student": "student-workstation",
  "major": "<program-or-major>",
  "source_mode": "<gui-source-mode>",
  "submit_mode": "<gui-source-mode>",
  "test_minutes": 5,
  "client_token": "<per-GUI-random-token>",
  "kind": "upload",
  "filename": "design.v",
  "verilog_filename": "design.v",
  "qsf_filename": "design.qsf",
  "sof_filename": "design.sof",
  "submission_signature": "<GUI-generated-fingerprint>"
}
```

The successful response contains a real `job_id` plus a creator `cancel_token`. The GUI stores the cancel token privately and then uploads multipart fields named `verilog_file`, `qsf_file` (when local), and `sof_file`. Server-path variants can instead use fields such as `verilog_path`, `qsf_path`, or `sof_path` where the controller explicitly permits that path.

## Cancellation payload example

Batch cancellation is the GUI's normal path:

```json
{
  "explicit_cancel": true,
  "student": "student-workstation",
  "client_hostname": "student-workstation",
  "student_ip": "<client-ip>",
  "client_token": "<per-GUI-random-token>",
  "jobs": [
    {
      "job_id": "<job-id>",
      "cancel_token": "<creator-cancel-token>",
      "client_token": "<per-GUI-random-token>"
    }
  ]
}
```

Do not log or publish real cancel tokens.
