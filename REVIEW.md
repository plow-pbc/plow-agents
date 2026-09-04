# Review instructions — plow-agents

Repo-specific reviewer policy. The universal voice posture (Broken-Glass,
pro-simplification, and the don't-propose list) is supplied by the reviewers
themselves and is deliberately not restated here.

## What this repo is

**The way a developer runs one of the Plow agent images on a machine of their
own.** One stdlib-only Python CLI over the credential lifecycle — `login`,
`lines`, `mint`, `revoke` — plus a minimal `compose.example.yml` to
copy into a new agent repository. It builds and mints; it does not own the
image, the API, or the container.

**Stage:** pre-PMF, a single operator, a handful of agents. Iteration speed
beats hardening for scale: prefer loud failures to fallbacks, and don't guard
edge cases this size cannot reach.

**Neighbours:** `plow-pbc/plow` owns the API and `cli/plow`;
`plow-pbc/plow-hermes-agent` owns the base image and its boot contract;
`plow-pbc/agent-mgr` still owns container lifecycle until `agent-mgr#130`
moves it here. `README.md` § Where changes go states the boundary; this file
does not restate it.

## Review priority

Subtractive remedies outrank additive ones. The falsifiable gate is whether a
change grows a surface a sibling repo already owns.

**Repo-specific contrast pairs:**

| Runner DON'T (suppress / flag-as-shape) | Runner DO (real finding) |
|---|---|
| Ask for a container abstraction, a lifecycle backend, or a fallback when Docker or the API is absent. Refusing loudly is the design. | Flag a change that a **sibling repo owns** per `plow-hermes-agent` README § The repos: a second copy of a plow CLI command (`plow`, `cli/plow`), a container lifecycle `agent-mgr` still owns, an agent's persona or skills (the variant, or the base for the default). The test is who else would have to change if the fact changed. |
| Treat doc-only edits to `README.md` as low-value churn. | Flag **prose↔code drift**, and any fact restated from a sibling rather than linked — a pin, a cap, a boot path. |

**Update cadence:** edit when the stage changes — a second operator, or
`agent-mgr#130` handing container lifecycle to this repo.
