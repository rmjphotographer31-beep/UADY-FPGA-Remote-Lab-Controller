# Project Evolution and Design Rationale

This document captures the main engineering changes reflected in the final code and the internship development history.

## From direct GPIO/SPI ideas to vendor JTAG programming

Early work explored Raspberry Pi GPIO/SPI-style interaction with FPGA pins. The final system moved programming responsibility to **Quartus JTAG**. This gave the project a vendor-supported programming path, allowed physical cable/device discovery, supported both Cyclone V SoC and Agilex toolchains, and made it possible for one Pi controller to orchestrate multiple board families through a central Quartus server.

The final shipped configuration therefore has `use_gpio: false`. Board GPIO mappings remain available for optional relays/reset/status hardware but are not the primary programming transport.

## Remote lab architecture

The project evolved into a board-farm model:

- student laptop runs the GUI;
- Raspberry Pi is the controller/gateway;
- local Qwen/Ollama selects the target board from submitted Verilog/QSF evidence;
- Quartus server owns Standard/Pro programming tools and JTAG access;
- the Pi keeps server credentials away from students;
- queue state is shared back to GUIs through the HTTP API/SSE stream.

## Queue correctness work

Development repeatedly focused on keeping GUI, Pi, server, and physical hardware synchronized. Major behaviors added across the iterations include:

- FIFO progression when a board becomes free;
- prevention of stale/inflated timers from blocking the queue;
- event-driven queue wakeups instead of relying on long countdown polling;
- one active request per student by default;
- immediate eligibility restoration after cancellation or test completion;
- duplicate active submission detection;
- stable Job IDs created before file upload;
- creator-aware cancellation;
- stale runner/orphan upload repair;
- retained terminal jobs so failure/completion does not disappear from the GUI.

## JTAG first-attempt reliability

A recurring failure pattern was that the first programming attempt could fail to connect to JTAG, while a later retry succeeded. The final design prewarms the JTAG/toolchain during startup and while idle, caches discovery state, and uses bounded retry/recovery logic so a student's first job is not the mechanism that initializes the chain.

## History deduplication

Earlier testing exposed multiple history records for the same job. The final configuration uses one stable Job ID record, created on queue acceptance and updated asynchronously across later events.

## AI classifier evolution

The classifier began as token/signal heuristics for distinguishing DE1-SoC and DE10-Agilex. It evolved toward local Qwen classification, then toward a more rigorous split:

- C code extracts current-job evidence but does not decide the board;
- Qwen produces the board decision;
- deterministic policy verifies/grounds that decision;
- recognized exact QSF device identity is authoritative;
- complete board-template QSF assignments are not mistaken for active Verilog features;
- model context is stateless between jobs;
- benchmark output records raw vs guarded accuracy.

This preserves AI reasoning while preventing a small model's stale or hallucinated evidence from programming the wrong physical FPGA.

## Latency goal

The project pursued minimal GUI/Pi/server delay. The final queue stream is event-driven with a 25 ms configured cache/interval and immediate wakeups on state change. This reduces avoidable software delay, but the complete system cannot provide nanosecond-level synchronization: network scheduling, Python threads, SSH, Ollama inference, Quartus, USB/JTAG, and FPGA programming are all much slower and variable. The correct engineering approach is to measure p50/p95 latency for each stage and optimize the dominant stages.

## Early proposal tracks that were not the final production focus

The original board-farm plan also investigated MSEL/configuration-mode control, JAM Player concepts, I2C device-tree/voltage-monitor access, and agent-based HDL complexity/platform selection. Development also explored OpenFPGALoader and automatic `.rbf`/`.svf` generation while moving away from GPIO programming ideas.

Those investigations are useful project history, but the **final v5.4 production code in this repository is centered on Verilog/QSF/SOF submission, DE1-SoC vs DE10-Agilex selection, FIFO/fair-share scheduling, Raspberry Pi orchestration, and Quartus `quartus_pgm` JTAG programming**. Do not claim that the final repository implements remote MSEL switching, JAM Player programming, or I2C regulator monitoring unless those features are separately added and tested.

## Networking evolution

NetBird was adopted as the remote overlay path so the GUI could reach the Raspberry Pi without exposing the lab controller directly to the public Internet. The final code treats the Pi address as user/deployment configuration and never requires a specific historical NetBird IP or setup key. Setup keys are enrollment credentials and must not be committed to a public repository.
