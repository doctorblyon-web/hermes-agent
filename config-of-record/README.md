# config-of-record — read-only mirrors

These files are **reference mirrors**, not runtime configuration. The live,
authoritative copies run on Williams-Mini under Christine's home:

| Mirror here | Runtime source (authoritative) |
|-------------|--------------------------------|
| `SOUL.md`   | `~/.hermes/SOUL.md` |
| `config.yaml` | `~/.hermes/config.yaml` |

**Editing anything in this directory changes nothing at runtime.** To change
Christine's behaviour, edit the files under `~/.hermes/` and restart the
`ai.hermes.gateway` service. Update these mirrors afterwards to keep the record
current.

## Why they exist

`~/.hermes/` is intentionally git-ignored (see repo `.gitignore`), so the runtime
persona and gateway config are not otherwise tracked. These mirrors capture the
state that the committed code assumes — most recently the **pa-goals slice**
(model-callable `pa_object` tool, clarify-then-write conduct, and the SIGNAL
day-routing / no-swallow rules that live in `SOUL.md`).

## Redactions

`config.yaml` here has live secrets removed and replaced with
`<REDACTED — see runtime ~/.hermes/config.yaml>`:

- `gateway`/dashboard `password_hash`
- dashboard `secret`
- `signal_gate.ssh_identity` (a filesystem path)

The runtime file holds the real values. Never commit the unredacted runtime
`config.yaml` — this fork is public. `SOUL.md` contains no secrets and is mirrored
verbatim.
