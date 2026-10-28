# Database Decision: SQLite in WAL Mode (Single-Writer)

## Decision: SQLite (WAL Mode)
For this project, I have chosen **SQLite in Write-Ahead Logging (WAL) mode** as the primary store for the Event Store and Memory.

### Justification:
1. **Single-Writer Architecture**: The Money-Zone is designed as a deterministic, single-threaded execution core. All broker orders and risk checks happen sequentially. SQLite's single-writer model perfectly matches this "Money-Zone" bottleneck, eliminating the need for complex distributed locking.
2. **Immutability**: Since the Event Store is append-only, we avoid the primary weakness of SQLite (concurrent writes). We can enforce immutability at the application level and via DB triggers.
3. **Deployment Simplicity**: For a $\sim 200$ CHF account, the overhead of managing a Postgres container (backups, users, network) outweighs the benefits. A single file is easier to back up and replay.
4. **Performance**: In WAL mode, readers (the Dashboard API and Agents) do not block the writer (the Money-Zone).

### Constraint Enforcement:
- **Writing**: Only the `firm-core` process has write access to the DB.
- **Reading**: The `firm-api` and `Agent-Runtime` containers use a read-only mount of the DB file or a restricted read-only user.

---

## Isolation Blueprint
- **Container A (Firm-Core)**: Full access to DB, `limits.toml`, and Broker Secrets.
- **Container B (Agent-Runtime)**: Read-only access to DB, NO access to Broker Secrets, NO write access to `limits.toml`.
- **Container C (Firm-API)**: Read-only access to DB.
