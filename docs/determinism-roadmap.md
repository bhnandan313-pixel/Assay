# Determinism Roadmap

## Request Replay vs. True Deterministic Execution

In **V1**, Assay performs **black-box HTTP request replay**: it reconstructs recorded HTTP requests against live baseline and candidate processes, accounts for expected non-determinism (timestamps, UUIDs, request IDs) via JSONPath `--ignore-path` filters, and attests to behavioral equivalence at the HTTP boundary.

Ordinary request replay does not control state *inside* the service under test—such as database mutations between calls, wall-clock `datetime.now()` reads, random number generation, or outbound third-party API calls.

## V2+ Roadmap

To move from black-box replay toward fully deterministic verification, future versions of Assay will introduce:

1. **Isolated Execution Sandboxes:** Running baseline and candidate services inside ephemeral container/VM snapshots so filesystem and process state start identically on every verification run.
2. **Virtual Time & Seeded Randomness:** Injecting deterministic clock shims (e.g. `libfaketime` / ASGI middleware) and fixed PRNG seeds so time-dependent business logic and generated identifiers become reproducible.
3. **Database State Virtualization:** Wrapping PostgreSQL and SQLite test databases in per-session snapshots or savepoint rollbacks so mutating `POST`/`PUT`/`DELETE` workloads can be replayed repeatedly against identical initial state.
4. **Outgoing Dependency Mocking:** Providing a local egress proxy that records and stubs outbound HTTP/gRPC calls (e.g., payment gateways, LLM APIs, email providers) so candidate services never trigger side effects or fail due to external network flakiness.
