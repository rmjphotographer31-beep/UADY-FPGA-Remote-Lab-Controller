# Publish This Repository to GitHub

The prepared repository is named **UADY FPGA Remote Lab Controller**. A suggested GitHub repository slug is:

```text
uady-fpga-remote-lab-controller
```

## Before publishing

Run:

```bash
python scripts/validate_repository.py
```

Then review `docs/SECURITY.md` and choose a software license. The supplied project did not include an open-source license, so this package does not invent one.

## Create the Git repository locally

```bash
git init -b main
git add .
git commit -m "Release UADY FPGA Controller v5.4 final manual-grounded build"
```

## Push to an empty GitHub repository

After creating an empty repository in your GitHub account/organization:

```bash
git remote add origin git@github.com:<owner>/uady-fpga-remote-lab-controller.git
git push -u origin main
```

HTTPS remote form is also fine if that is how your Git credentials are configured.

## Suggested GitHub description

> Remote FPGA board-farm controller for DE1-SoC and DE10-Agilex using a Windows GUI, Raspberry Pi orchestration, local Qwen/Ollama classification, FIFO scheduling, and Quartus/JTAG programming.

## Suggested topics

```text
fpga
verilog
quartus
jtag
raspberry-pi
ollama
qwen
remote-lab
de1-soc
de10-agilex
```

## First release tag

After a live-hardware acceptance pass, tag the exact deployed commit:

```bash
git tag -a v5.4.0 -m "UADY FPGA Controller v5.4 final manual-grounded release"
git push origin v5.4.0
```

Do not tag a hardware-validated release until the Pi/Ollama/Quartus/JTAG acceptance checklist has actually passed on the target lab.
