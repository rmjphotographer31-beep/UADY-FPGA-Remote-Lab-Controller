# Raspberry Pi Installation

## Base packages

The controller requires Python 3.9+ per the Pi project metadata. You also need a C compiler for the signal extractor and network/SSH access to the Quartus server.

Example Debian/Raspberry Pi OS packages:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip build-essential openssh-client curl
```

Install NetBird separately if your lab uses it. NetBird is a transport choice, not embedded in this repository.

## Copy the controller

Copy `raspberry_pi_ai_hat/` to the Pi. The location is not required to be `/home/<pi-user>/...`; use your own account, for example:

```bash
cd ~
# copy/extract the raspberry_pi_ai_hat directory here
cd raspberry_pi_ai_hat
```

## Python environment and C extractor

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements_pi.txt
chmod +x BUILD_SIGNAL_EXTRACTOR.sh RUN_PI_CONTROLLER.sh UADY_PI.sh
./BUILD_SIGNAL_EXTRACTOR.sh
```

Expected extractor output is `fpga_signal_extractor`. It is a build artifact and should not be committed.

## Configure the Quartus server

Interactive setup:

```bash
python3 UADY_PI.py --setup
```

You will be asked for:

- Quartus server host/IP;
- Quartus server SSH username;
- Standard `quartus_pgm` path;
- Pro/Agilex `quartus_pgm` path;
- Standard and Pro project directories;
- Standard and Pro log files;
- SSH private-key path as it exists on the Pi;
- job-history base directory;
- timeout settings.

These deployment values are stored outside the source tree in protected Pi storage.

Check the result:

```bash
python3 UADY_PI.py --check
python3 UADY_PI.py --test-jtag
```

## Install and verify Ollama/Qwen

The default config expects local Ollama and model `qwen2.5-coder:1.5b`:

```bash
./INSTALL_QWEN_1_5B.sh
curl http://127.0.0.1:11434/api/tags
```

The controller uses stateless `/api/generate` calls, temperature `0`, seed `42`, context size `4096`, and a structured decision schema.

## Validate release files

```bash
python3 VALIDATE_V5.py
python3 TEST_FPGA_CLASSIFIER_POLICY.py
```

## Print pairing keys

```bash
python3 UADY_PI.py --keys
```

The API and terminal keys are generated in protected Pi storage and are not returned by the public status endpoints.

## Start controller

Recommended interactive start:

```bash
python3 UADY_PI.py --start
```

This path:

1. creates/updates the Pi virtual environment if needed;
2. installs requirements when the requirements hash changes;
3. performs JTAG prewarming;
4. launches the Flask controller;
5. restarts the controller if it exits unexpectedly;
6. writes controller logs to private Pi application storage.

For service installation, adapt `deploy/uady-fpga-pi.service.example`.
