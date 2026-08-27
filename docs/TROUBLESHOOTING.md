# Troubleshooting

## GUI cannot connect to Pi

Symptoms: `/status` fails, GUI shows Pi offline, or requests return 401.

Check:

```bash
# On Pi
python3 UADY_PI.py --keys
python3 UADY_PI.py --start
```

Then verify the GUI has the correct Pi host and API key. A normal browser request to the Pi API can return 401 because it does not send `X-API-Key`; that is expected.

## `UADY_SETUP.py` import failure

The original final archive omitted `SETUP_CLASSIC_AND_PI.py`. The GitHub-ready package restores that helper. If this error appears, verify the repository is complete and that `SETUP_CLASSIC_AND_PI.py` is beside `UADY_SETUP.py`.

## Job created but does not enter queue

Use `/queue`, `/queue/<job_id>`, and GUI terminal output to identify whether the job is still `receiving`, failed upload verification, was rejected by fair-share/duplicate admission, failed AI safety checks, or has no compatible live slot.

The GUI should not call `/queue/<job_id>/upload_files` unless `/queue/prequeue_upload` returned a real Job ID.

## Cannot cancel a job

Cancellation requires creator authority/token for normal users. Queue creator tokens are stored in the GUI user's private application-data directory, not in the extracted project folder. Make sure the job was created by the current GUI identity and that the private token store was not deleted.

## Same job appears twice in server history

The final config requires one record per job. Confirm the Pi private `server_history` settings were not overwritten and that `one_record_per_job` and `record_on_queue_accept` remain enabled. Also ensure the server history directory is writable by the configured Quartus SSH account.

## First JTAG programming attempt fails but retry works

This was a major development issue. The final build adds startup and idle JTAG prewarming. Check:

```bash
python3 UADY_PI.py --test-jtag
python3 pi_ai_hat_controller.py --jtag-prewarm-startup
```

Also verify the configured Standard/Pro `quartus_pgm` path and actual cable/device chain.

## Waiting FIFO job does not start after board becomes free

Inspect:

- `/boards?force=1` for live slot state;
- `/queue` for the job target/status;
- whether the physical slot is disabled/quarantined;
- whether a stale running/testing owner still holds the slot;
- controller watchdog/audit logs.

The final dispatcher is event-driven and contains orphan/stale-runner repair logic; a persistent stall indicates a real resource/worker condition that should be visible in job messages or diagnostics.

## AI selects the wrong board

First inspect the QSF device. If it is one of the four recognized IDs in `board_profiles.json`, the final guarded result must agree with that identity. Run:

```bash
python3 TEST_FPGA_CLASSIFIER_POLICY.py
python3 VALIDATE_V5.py
```

If the QSF is missing/unknown, classification may rely more heavily on current Verilog evidence and can correctly return ambiguity rather than forcing a board.

## Agilex takes much longer than DE1-SoC

The production config intentionally gives Pro/Agilex programming larger watchdog/timeout windows than Standard. Do not reduce them to match DE1-SoC unless you have measured the complete Quartus/JTAG path on your specific lab.
