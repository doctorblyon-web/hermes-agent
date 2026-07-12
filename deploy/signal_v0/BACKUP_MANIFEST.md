# Durable SIGNAL V0 pre-deployment backup manifest

Recorded 2026-07-12 before any live deployment change.

## Williams existing files

| File | SHA-256 | Owner/group | Mode | mtime epoch |
|---|---|---|---|---|
| `~/.hermes/hermes-agent/gateway/platforms/base.py` | `f7de166de1ffbd328a90844e91468c4195f8de2653e90e41a37c1f4083b5751c` | `willlyon:staff` | `0600` | `1783830878` |
| `~/.hermes/hermes-agent/plugins/platforms/telegram/adapter.py` | `ab0de14569cfc95005014d5657e0274b8571200cb1cc0f20ffd8a5c6e1a4dc27` | `willlyon:staff` | `0600` | `1783830878` |
| `~/.hermes/SOUL.md` | `1b7fc019ef342a943cf909d828cb0bcc2f125b611721f4273f322ab2675f00dc` | `willlyon:wheel` | `0644` | `1783825781` |
| `~/.hermes/config.yaml` | `2c79e0fac31f7579169273645eed1dc8bf61437bac0172ab75aa0ee29bc6798a` | `willlyon:staff` | `0600` | `1783822560` |
| `~/.hermes/hermes-agent/package-lock.json` (preserve; never change) | `54bb7972e916df083d39f68fe44bfef96807b8d117edd3c6f9178afe2c43bad0` | `willlyon:staff` | `0600` | `1783830344` |

`~/.ssh/billos_signal_v0` and `~/.hermes/signal_gate.db` were absent.

## M3 existing files

| File | SHA-256 | Owner/group | Mode | mtime epoch |
|---|---|---|---|---|
| `/Users/billlyon/.ssh/authorized_keys` | `421b261177370d71a3cba67da43abab11f7ade746002e4217d3c8f4602baa98c` | `billlyon:staff` | `0600` | `1783822436` |
| `/Users/billlyon/VaultHub/BillVault/Agent-Shared/BillOS/bin/process-event` (verify only; never change) | `8a03e90dd43cd041ce1d7be0393c5e5fc3d6c31ef7de028f47a398dde22e3a00` | `billlyon:staff` | `0755` | `1783565243` |

`/Users/billlyon/bin/signal-set-endpoint` and
`/Users/billlyon/.local/state/billos-signal-endpoint/requests.sqlite` were absent.

Immediately before deployment, recheck every hash and then make timestamped,
same-filesystem backups of every existing file that will change. Hash each backup
before installing any candidate file.
