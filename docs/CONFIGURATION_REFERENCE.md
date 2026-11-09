# Apex Trader configuration reference

## Operator controls

The process reads `.env` at startup through `main.py`. Copy `.env.example` to
`.env`, set deployment-specific values, and restart the process to apply
startup configuration. The application must not rewrite `.env`.

### Instrument universe

| Variable | Accepted values | Effect |
|---|---|---|
| `TRADING_MODE` | `gold`, `full`, `selected`, `single` | Selects the enabled instrument universe |
| `TRADING_SYMBOLS` | Comma-separated broker-neutral symbols | Required for `selected` and `single` |
| `TRADING_ENABLED_CATEGORIES` | Comma-separated category names | Restricts categories in `full` mode |

`gold` resolves to exactly `XAUUSD`. `full` resolves the broker-supported
universe within the selected categories. `selected` uses every symbol in
`TRADING_SYMBOLS`; `single` requires exactly one symbol.

### New exposure and position management

| Variable | Accepted values | Effect |
|---|---|---|
| `TRADING_ENABLED` | boolean | Master switch for new Brain-originated exposure |
| `COGNITION_ENABLED` | boolean | Enables/disables the cognition subsystem |
| `COGNITION_GATE_MODE` | `authoritative` (production) | Keeps Brain as the decision authority |
| `COGNITION_ORIGINATION_MODE` | `off`, `shadow`, `live` | Controls new campaign origination |
| `COGNITION_MANAGEMENT_MODE` | `off`, `shadow`, `live` | Controls Brain position management |

`TRADING_ENABLED=false` blocks new origination. It deliberately does not
disable reconciliation, existing-position management, or risk-reducing closes.
`COGNITION_ORIGINATION_MODE=live` is necessary but not sufficient for an order:
the Brain's evidence, opportunity, expected-value, owner-limit, portfolio,
broker-feasibility, and execution gates remain mandatory.

## Configuration ownership

- `.env.example` is the documented deployment/environment interface.
- `config.py` defines typed subsystem defaults and validates configuration.
- `api/config.py` defines control-plane/API settings.
- `api/instance_config.py` applies validated per-user overrides after process
  defaults for multi-tenant instances.
- Broker-native constraints (volume step, tick size/value, stop/freeze levels,
  margin and symbol availability) are read from the broker and are not operator
  substitutes.
- Canonical identity, Brain-only decision authority, evidence integrity,
  completed-bar semantics, fail-closed unknown states, and execution safety
  invariants are code-enforced. They are not operator-disableable switches.

## Coverage rule

Every literal runtime environment read in Python production code must have a
matching declaration in `.env.example`. This is enforced by
`tests/test_env_example_coverage.py`.

The typed configuration dataclasses also contain implementation defaults and
analytical parameters. Those are not automatically deployment settings: they
must be classified as operator controls, data-derived methodology, internal
implementation detail, or non-overridable safety invariant before exposing
them. Do not expose a threshold merely to make it editable; that would turn
internal methodology into an ungoverned operator override.

## Frontend build-time configuration

The root `.env.example` is for the Python runtime/control plane. Vite build-time
variables belong in the frontend-specific templates:

- `frontend/.env.example` configures the multi-tenant React client.
- `dashboard/frontend/.env.example` configures the operator dashboard.

Do not copy broker credentials, JWT secrets, API master keys, or other private
secrets into a `VITE_*` variable. Vite embeds those values in browser assets.
The dashboard's `VITE_DASHBOARD_API_KEY` is only a convenience fallback for
single-user/local builds and is visible to every browser user; production
operators should enter the key at runtime in the dashboard Controls page.
The server-side secret remains `DD_DASHBOARD_API_KEY` in the root `.env`.

Vite environment coverage is enforced by
`tests/test_frontend_env_example_coverage.py`.


## Full typed-field inventory

See [CONFIGURATION_FIELD_INVENTORY.md](CONFIGURATION_FIELD_INVENTORY.md) for the
source-backed inventory of all 716 typed fields across 43 configuration
dataclasses. The inventory is not a blanket permission to expose every field.
Each field must be classified before it becomes an environment override.
