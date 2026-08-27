# Security and Private Data

## Secrets that must never be committed

- Pi API key;
- GUI terminal access key;
- queue creator/cancel tokens;
- SSH private keys;
- deployment-specific Quartus server credentials/addresses if the lab considers them private;
- private GUI configuration files.

The repository `.gitignore` excludes common secret/config filenames and private-key patterns. Always run a secret scan before publishing.

## Pi API authentication

The Pi generates the API key on first use. It is stored outside `config_pi_hat.json`. All normal HTTP routes are protected by the global request hook when the key exists.

The terminal key is separate from the API key. `/security/terminal_key_status` never returns its value; administrators retrieve it locally with `UADY_PI.py --keys`.

## SSH architecture

Students should not need the Quartus server SSH private key. The key stays on the Raspberry Pi and the Pi performs server-side copy/programming operations.

Prefer a dedicated server account and an SSH key restricted to the required server. Protect the key with filesystem permissions and avoid copying it into the GUI folder.

## Source/evidence retention

The final config requests:

- job source files only in temporary spool storage;
- C extractor result in memory/stdout;
- AI prompt in memory;
- raw AI response in memory;
- compact permanent history metadata only;
- terminal cleanup and startup orphan cleanup.

This is application-level behavior, not a guarantee against OS swap, process dumps, administrator logging, filesystem snapshots, or backups.

## Network transport

The Flask API is HTTP by default. NetBird can provide an encrypted overlay transport between GUI and Pi, but the application itself does not terminate HTTPS. If you expose the API outside a trusted overlay/LAN, place it behind an authenticated TLS reverse proxy and firewall it appropriately.

## Public GitHub checklist

Before publishing:

```bash
python scripts/validate_repository.py

git grep -n -E 'BEGIN (OPENSSH|RSA|EC|DSA) PRIVATE KEY|setup-key|api[_-]?key[[:space:]]*=[[:space:]]*[^<]'
```

Also inspect commit history; deleting a secret from the latest tree does not remove it from earlier Git commits.
