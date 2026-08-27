# Configuration Reference

## Public/default configuration

`raspberry_pi_ai_hat/config_pi_hat.json` contains portable behavior defaults. It deliberately does not contain deployment-specific SSH credentials, Pi API keys, terminal keys, or Quartus server credentials.

Important sections:

| Section | Purpose |
|---|---|
| `ai` | Qwen/Ollama model, confidence threshold, prompt/grounding behavior |
| `board_catalog` | Enabled board families and GPIO/JTAG defaults |
| `testing` | Default/max board test lease |
| `server_history` | One-record-per-job behavior |
| `queue_estimates` | Learned programming-time estimates |
| `realtime_stream` | SSE queue update timing |
| `strict_resource_engine` | Watchdogs, requeue and slot-safety behavior |
| `traffic_control` / `dynamic_scaling` | Adaptive request concurrency |
| `queue_staging` | Temporary file staging/compression/cleanup |
| `quartus_programming_reliability` | Per-job JTAG retry/warmup behavior |
| `jtag_prewarm_daemon` | Idle/startup JTAG readiness |
| `fair_share` | Per-student active-job limit |
| `hardware_profiles` | External `board_profiles.json` integration |
| `ephemeral_job_storage` | Student source/evidence retention policy |

## Protected Pi configuration

Deployment settings are written through `uady_secure_store.py` to Pi private storage. `UADY_PI.py --setup` manages the `quartus_server` and `server_history` sections and the Pi-side SSH-key path.

Storage preference is:

1. `UADY_PI_SECRET_DIR` environment variable, if set;
2. `/var/lib/uady_fpga_lab` if writable;
3. per-user application data fallback.

## Protected GUI configuration

The GUI stores connection/profile data under OS per-user application data. `UADY_CLASSIC_CONFIG` can point the legacy Classic Mode loader to an explicitly provided private INI file.

## Environment overrides

Quartus discovery supports:

```bash
UADY_QUARTUS_STANDARD_PGM
UADY_QUARTUS_PRO_PGM
UADY_QUARTUS_SEARCH_ROOTS
```

The Pi secret root supports:

```bash
UADY_PI_SECRET_DIR
```

The GUI API key can also be supplied through:

```text
UADY_PI_API_KEY
```

when supported by the GUI code path.

## Board profiles

`board_profiles.json` is the single external source for physical board profile matching and manual-grounded identity evidence. New board families should be added carefully and validated end-to-end; the current AI policy explicitly allows DE1-SoC, DE10-Agilex, or ambiguity.
