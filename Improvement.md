# Improvement: move the DLQ from SQLite to Aurora PostgreSQL with IAM authentication

## Why

The DLQ is currently a SQLite file inside the service container. That is fine for Round 1, but it has limits in a real deployment:

| Problem with SQLite | What Aurora PostgreSQL fixes |
|---|---|
| The file lives on one container's disk. If that container is replaced, the dead letters go with it. | Durable, replicated storage that outlives any container. |
| Every service replica has its own file, so there is no single place to inspect or replay failures. | One shared DLQ table that all replicas write to and replay from. |
| Two replicas cannot safely replay the same queue. | Row locking (`FOR UPDATE SKIP LOCKED`) lets replicas replay without overlap. |
| No protection against the same `event_id` being stored or replayed twice. | A `UNIQUE (event_id)` constraint gives idempotency. |
| Inspecting failures means shelling into a container. | Plain SQL from anywhere with IAM access. |

Credentials follow the same rule as the rest of the project: **no passwords anywhere**. The service logs in to Postgres with a short-lived IAM token, so there is no database secret to store, sync, rotate, or leak.

> This replaces the SQS plan mentioned earlier. SQS gives redrive and buffering, while Postgres gives queryable, replayable history with idempotency. Pick one as the system of record.

## Target flow

```
service (IAM role)
   │ 1. role credentials from the AWS credential chain (no keys in code)
   ▼
rds:GenerateDBAuthToken  →  token valid for 15 minutes
   │ 2. token used as the Postgres password, over TLS
   ▼
Aurora checks IAM: may this role connect as DB user "dlq_app"?
   │ 3. yes → connection opened
   ▼
INSERT into dlq.dead_letters  (on failed calls)
SELECT/UPDATE ... SKIP LOCKED (on replay)
```

Three things must line up for a login to work:
1. **Permission:** the service's IAM role has `rds-db:connect` for exactly one cluster and one DB user.
2. **Database:** that Postgres user exists and is a member of `rds_iam`.
3. **Token:** the app generates a fresh token for every new connection and connects over TLS.

If login fails, one of these is wrong.

## What changes in the code

| File | Change |
|---|---|
| `service/app/dlq.py` | Keep the SQLite class. Add `PostgresDeadLetterQueue` with the **same methods** (`add`, `list`, `count`, `mark_replayed`, `record_failed_replay`) and a `build_dlq(settings)` factory. |
| `service/app/config.py` | Add `dlq_backend`, `db_host`, `db_port`, `db_name`, `db_user`, `aws_region`. No password setting exists. |
| `service/app/main.py` | Replace `DeadLetterQueue(settings.dlq_path)` with `build_dlq(settings)`. Show `dlq_backend` in `/health`. |
| `service/app/service.py` | Handle a failing DLQ write (see "When the DLQ itself is down"). Replay claims rows with `claim_pending`. |
| `service/requirements.txt` | Add `boto3`, `SQLAlchemy`, `psycopg2-binary`. |
| `terraform/` (new) | IAM policy for `rds-db:connect`. |

`resilience.py` and the `pipeline/` folder are untouched.

SQLite stays the default (`DLQ_BACKEND=sqlite`), so Round 1 still runs locally with no AWS account and no extra steps.

## Step 1: confirm IAM authentication is on

```bash
aws rds describe-db-clusters --db-cluster-identifier <cluster> \
  --query 'DBClusters[0].IAMDatabaseAuthenticationEnabled'
```

It must print `true`. If you manage the cluster with Terraform, set `iam_database_authentication_enabled = true` on `aws_rds_cluster`.

## Step 2: get the cluster resource ID

The IAM policy needs the cluster **resource ID** (`cluster-XXXX`), not the cluster name. Using the name is the most common mistake: the policy applies cleanly and login still fails.

```bash
aws rds describe-db-clusters --db-cluster-identifier <cluster> \
  --query 'DBClusters[0].DbClusterResourceId' --output text
```

The ARN format is:

```
arn:aws:rds-db:<region>:<account-id>:dbuser:<cluster-resource-id>/<db-user>
```

## Step 3: Terraform, the IAM permission

This attaches a least-privilege policy to the IAM role your service already runs with, and looks up the resource ID for you.

```hcl
variable "region"                { default = "ap-south-1" }
variable "db_cluster_identifier" { type = string }
variable "db_iam_user"           { default = "dlq_app" }   # the Postgres user the service logs in as
variable "service_role_name"     { type = string }         # existing IAM role the service runs with

data "aws_caller_identity" "current" {}

data "aws_rds_cluster" "this" {
  cluster_identifier = var.db_cluster_identifier
}

data "aws_iam_role" "service" {
  name = var.service_role_name
}

locals {
  # arn:aws:rds-db:<region>:<account>:dbuser:<cluster-resource-id>/<db-user>
  db_connect_arn = "arn:aws:rds-db:${var.region}:${data.aws_caller_identity.current.account_id}:dbuser:${data.aws_rds_cluster.this.cluster_resource_id}/${var.db_iam_user}"
}

# WHAT the role can do: log in as one DB user, nothing else
data "aws_iam_policy_document" "db_connect" {
  statement {
    actions   = ["rds-db:connect"]
    resources = [local.db_connect_arn]
  }
}

resource "aws_iam_role_policy" "db_connect" {
  name   = "dlq-aurora-rds-db-connect"
  role   = data.aws_iam_role.service.id
  policy = data.aws_iam_policy_document.db_connect.json
}

output "db_connect_arn" {
  value = local.db_connect_arn
}
```

After `terraform apply`, check that `terraform output db_connect_arn` shows your real resource ID. This replaces the SQS statement in the Appendix F policy sketch. The policy still contains no wildcard actions.

## Step 4: create the DB user and table (one time, as the admin user)

IAM only decides who may log in. What the user can do inside Postgres is controlled by normal SQL grants. Run this once as the master user (keep that password in Secrets Manager, e.g. `manage_master_user_password = true`, and use it only for admin tasks like this).

```sql
-- 1. the login: no password, authenticates with IAM tokens only
CREATE USER dlq_app WITH LOGIN;
GRANT rds_iam TO dlq_app;
GRANT CONNECT ON DATABASE appdb TO dlq_app;

-- 2. a dedicated schema and the table
CREATE SCHEMA dlq;

CREATE TABLE dlq.dead_letters (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id     UUID        NOT NULL UNIQUE,            -- idempotency
    payload      JSONB       NOT NULL,                   -- original call event
    error        TEXT        NOT NULL,                   -- e.g. http_503 or circuit_open (last: ...)
    attempts     INT         NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'replaying', 'replayed')),
    replay_count INT         NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX dead_letters_status_id_idx ON dlq.dead_letters (status, id);

-- 3. least privilege: the app can read, insert and update. No DELETE, DROP or TRUNCATE.
GRANT USAGE ON SCHEMA dlq TO dlq_app;
GRANT SELECT, INSERT, UPDATE ON dlq.dead_letters TO dlq_app;
```

What the important lines do:

| Statement | Effect |
|---|---|
| `CREATE USER dlq_app WITH LOGIN` | Creates a user with no password, on purpose. |
| `GRANT rds_iam TO dlq_app` | Makes the user authenticate with IAM tokens. After this a password login is refused. This line links Postgres to IAM. |
| `UNIQUE (event_id)` | The same call can only occupy one DLQ row, so retries and replays cannot create duplicates. |
| `GRANT SELECT, INSERT, UPDATE` | The service can record, inspect, and mark rows replayed, but cannot delete history. |

Quick check:

```sql
SELECT has_database_privilege('dlq_app', 'appdb', 'CONNECT');
\du dlq_app
```

## Step 5: configuration

No password variable exists. The AWS SDK finds the role credentials on its own.

| Variable | Example | Purpose |
|---|---|---|
| `DLQ_BACKEND` | `postgres` | `sqlite` (default) or `postgres` |
| `AWS_REGION` | `ap-south-1` | region used to sign the token |
| `DB_HOST` | `<cluster-writer-endpoint>` | the token is bound to this exact hostname |
| `DB_PORT` | `5432` | |
| `DB_NAME` | `appdb` | |
| `DB_USER` | `dlq_app` | must match the user in the IAM policy |

Add the matching fields to `Settings` and `load_settings()` in `config.py`:

```python
dlq_backend=e("DLQ_BACKEND", "sqlite"),
aws_region=e("AWS_REGION", "ap-south-1"),
db_host=e("DB_HOST", ""),
db_port=int(e("DB_PORT", "5432")),
db_name=e("DB_NAME", ""),
db_user=e("DB_USER", ""),
```

`service/requirements.txt` additions:

```
boto3>=1.35
SQLAlchemy>=2.0
psycopg2-binary>=2.9
```

## Step 6: the Postgres DLQ

Add this to `service/app/dlq.py` (keep the existing SQLite class as is).

```python
import json

import boto3
import psycopg2
from sqlalchemy import create_engine, text


class PostgresDeadLetterQueue:
    """Aurora PostgreSQL DLQ authenticated with short-lived IAM tokens (no password)."""

    def __init__(self, host: str, port: int, dbname: str, user: str, region: str):
        self._host, self._port, self._db, self._user, self._region = host, port, dbname, user, region
        self._rds = boto3.client("rds", region_name=region)
        self._engine = create_engine(
            "postgresql+psycopg2://",
            creator=self._connect,      # called for every NEW connection
            pool_size=5,
            max_overflow=5,
            pool_recycle=600,           # recycle well inside the token lifetime
            pool_pre_ping=True,
        )

    def _connect(self):
        token = self._rds.generate_db_auth_token(
            DBHostname=self._host, Port=self._port, DBUsername=self._user, Region=self._region
        )
        return psycopg2.connect(
            host=self._host, port=self._port, dbname=self._db, user=self._user,
            password=token, sslmode="require", connect_timeout=5,
        )

    def add(self, event_id: str, payload: dict, error: str, attempts: int) -> int:
        with self._engine.begin() as c:
            return c.execute(
                text("""
                    INSERT INTO dlq.dead_letters (event_id, payload, error, attempts)
                    VALUES (:event_id, CAST(:payload AS jsonb), :error, :attempts)
                    ON CONFLICT (event_id) DO UPDATE
                       SET error = EXCLUDED.error, attempts = EXCLUDED.attempts,
                           status = 'pending', updated_at = now()
                    RETURNING id
                """),
                {"event_id": event_id, "payload": json.dumps(payload),
                 "error": error, "attempts": attempts},
            ).scalar_one()

    def list(self, status: str = "pending", limit: int = 100) -> list[dict]:
        with self._engine.connect() as c:
            rows = c.execute(
                text("SELECT id, event_id::text AS event_id, payload, error, attempts, status, "
                     "replay_count, created_at, updated_at "
                     "FROM dlq.dead_letters WHERE status = :s ORDER BY id LIMIT :n"),
                {"s": status, "n": limit},
            ).mappings().all()
        return [dict(r) for r in rows]

    def count(self, status: str = "pending") -> int:
        with self._engine.connect() as c:
            return c.execute(
                text("SELECT count(*) FROM dlq.dead_letters WHERE status = :s"), {"s": status}
            ).scalar_one()

    def claim_pending(self, limit: int = 100) -> list[dict]:
        """Atomically take rows for replay so two replicas never replay the same row."""
        with self._engine.begin() as c:
            rows = c.execute(
                text("""
                    UPDATE dlq.dead_letters SET status = 'replaying', updated_at = now()
                    WHERE id IN (
                        SELECT id FROM dlq.dead_letters
                        WHERE status = 'pending' ORDER BY id LIMIT :n
                        FOR UPDATE SKIP LOCKED)
                    RETURNING id, event_id::text AS event_id, payload
                """),
                {"n": limit},
            ).mappings().all()
        return [dict(r) for r in rows]

    def mark_replayed(self, dlq_id: int) -> None:
        with self._engine.begin() as c:
            c.execute(
                text("UPDATE dlq.dead_letters SET status = 'replayed', "
                     "replay_count = replay_count + 1, updated_at = now() WHERE id = :id"),
                {"id": dlq_id},
            )

    def record_failed_replay(self, dlq_id: int, error: str) -> None:
        with self._engine.begin() as c:
            c.execute(
                text("UPDATE dlq.dead_letters SET status = 'pending', error = :e, "
                     "replay_count = replay_count + 1, updated_at = now() WHERE id = :id"),
                {"id": dlq_id, "e": error},
            )


def build_dlq(s):
    if s.dlq_backend == "postgres":
        return PostgresDeadLetterQueue(s.db_host, s.db_port, s.db_name, s.db_user, s.aws_region)
    if s.dlq_backend == "sqlite":
        return DeadLetterQueue(s.dlq_path)
    raise ValueError(f"Unknown DLQ_BACKEND: {s.dlq_backend!r} (use 'sqlite' or 'postgres')")
```

Then in `main.py`, replace `dlq = DeadLetterQueue(settings.dlq_path)` with `dlq = build_dlq(settings)`.

Notes on the design:
- **A fresh token per new connection.** The token is only checked when a connection is opened, so open pooled connections keep working after 15 minutes.
- **`ON CONFLICT`** makes `add` idempotent: a repeated failure for the same event updates the row instead of creating a duplicate.
- **`claim_pending`** moves rows to `replaying` inside one statement. A replay that fails puts the row back to `pending`. If a replica dies mid-replay, rows stay in `replaying`, so add a periodic sweep that resets `replaying` rows whose `updated_at` is older than a few minutes.
- **TLS is required.** For stricter verification use `sslmode=verify-full` with the RDS CA bundle (`sslrootcert=...`).

## When the DLQ itself is down

The DLQ write sits on the failure path, so if the database is unreachable, a failed call could vanish, which is exactly what this design exists to prevent. Handle it in `service.py`:

1. Wrap `dlq.add(...)` in `try/except`.
2. On failure, log the **full event payload** as one structured ERROR line (`dlq_write_failed event_id=... payload=...`) so it can be recovered from logs.
3. Return **503** instead of 202, so the caller knows the event was not captured and can resend it.

This keeps the rule that every call ends as completed, in the DLQ, or explicitly rejected.

## Step 7: verify

**1. Check IAM login from a machine that has the same role permissions:**

```bash
export PGPASSWORD=$(aws rds generate-db-auth-token \
  --hostname <cluster-writer-endpoint> --port 5432 \
  --username dlq_app --region ap-south-1)

psql "host=<cluster-writer-endpoint> port=5432 dbname=appdb user=dlq_app sslmode=require" \
  -c "select current_user"
```

**2. Run the service with `DLQ_BACKEND=postgres`:**

```bash
curl <service>/health        # dlq_backend: postgres, dlq_pending: 0
python script/traffic.py --duration 120 --speed 0.5
```

**3. Inspect the DLQ with SQL:**

```sql
SELECT status, error, count(*) FROM dlq.dead_letters GROUP BY 1, 2 ORDER BY 3 DESC;
SELECT id, event_id, attempts, created_at FROM dlq.dead_letters WHERE status = 'pending' ORDER BY id LIMIT 10;
```

**4. Replay and confirm:**

```bash
curl -X POST "<service>/dlq/replay?limit=500"
```

Then re-run the first query. Rows should move from `pending` to `replayed`.

## Troubleshooting

| Error | Likely cause |
|---|---|
| `PAM authentication failed` | The user is missing `rds_iam`, the token was generated for a different hostname than `DB_HOST`, or the policy ARN uses the cluster name instead of the resource ID. |
| `AccessDenied` / `not authorized to perform rds-db:connect` | The policy is not attached to the role the service actually runs with, or `DB_USER` differs from the user in the ARN. |
| `no pg_hba.conf entry ... no encryption` | IAM auth requires TLS. Use `sslmode=require`. |
| Works for 15 minutes, then fails | A token is cached somewhere. Generate a new one per connection. |
| Connection timeout | The security group does not allow port 5432 from the service's security group. |
| `permission denied for table dead_letters` | The `GRANT SELECT, INSERT, UPDATE` or `GRANT USAGE ON SCHEMA dlq` was not applied. |
| Breaks after recreating the cluster | The resource ID changed, so re-apply Terraform. |

## Security notes

- Scope the policy to one DB user. Never use `.../*`.
- Use one IAM role and one DB user per service, so CloudTrail shows which service logged in.
- The app user cannot `DELETE`, `DROP`, or `TRUNCATE`, so failure history cannot be erased through the service. The payload contains borrower data, so keep the cluster in `ap-south-1` private subnets with encryption at rest, in line with the data-residency note in the challenge.
- IAM authentication has a limit of roughly 200 new connections per second. DLQ writes are low volume and the pool reuses connections, so this is comfortable. If many replicas are added, use RDS Proxy.

## Trade-offs and follow-ups

- **Cost:** Aurora adds a line to `COST.md` (instance or Serverless v2 capacity, storage, I/O). Re-estimate with current ap-south-1 prices before finalising, because the DLQ is low volume and a small configuration may be enough.
- **Local development:** SQLite remains the default, since IAM database authentication cannot run on a laptop. The `DLQ_BACKEND` switch keeps both paths consistent, with the same interface and the same replay logic.
- **Migration:** before switching, drain the SQLite DLQ with `POST /dlq/replay` so no pending rows are left behind.
- **Retention:** add a scheduled job (run as the admin role) to archive `replayed` rows after a retention period, since the service itself cannot delete.