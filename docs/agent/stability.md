# Agent: Stability & Limitations

## Stability guarantees

`aitaem[agent]`'s public surface carries the same semver promise as core `aitaem`, with
one deliberate exception:

| Surface | Stability |
|---|---|
| Convenience bot constructors (`QueryBot`, `DefinitionBot`) and primitives base classes (`Bot`, etc.) | Semver-stable |
| Default tool input/output schemas | Semver-stable |
| `RunTrace` and `BotResponse` field shapes | Semver-stable — the [eval substrate](evaluating-your-agent.md) depends on this contract holding |
| Default prompt **content** | Public, but **not** semver-stable — tuning is expected across patch and minor releases |

Don't pin application behavior to the exact wording of a default prompt — a patch
release may change it. If you need prompt stability, override it via the
[primitives layer](building-your-own-bot.md) instead of depending on default content.

!!! note "A prompt tune is also a cache-warmup reset"
    Changing the static instruction text (the parts every bot builds once and reuses
    across turns) invalidates the provider-side prompt cache — a prompt change isn't
    purely a behavior change, it's also a one-time cache cost the next time each routing
    lane runs.

## Not enabled

The following are intentionally unsupported, not just undocumented:

- Modifying the `ResultStore` entry schema
- Removing a default tool by name
- Hot-swapping the LLM runtime away from `pydantic-ai`
- Persistent state owned by the bot beyond what you explicitly serialize via
  `dump_history()`

## Limitations

See [Getting Started](getting-started.md#tenant_id) for the full `tenant_id` /
multi-tenancy note. In short:

- The agent module never stores credentials, persists history beyond what you
  explicitly serialize, manages multi-tenancy or RBAC, or holds connections other than
  the `ConnectionManager` you provide. Isolation between tenants is your application's
  responsibility, not something any constructor parameter grants.
- Concurrent `chat()`/`ask()` calls on the *same* bot instance are unsupported. One call
  at a time per instance; concurrency belongs at the caller level (one bot instance per
  active session).
