# Database Operations Guide

This guide documents the guarded database commands used by the repository. Keep these procedures separate from the root README so operational details do not overwhelm the project entry documentation.

## Purpose

The database administrator commands are used to validate connectivity, create the schema, seed canonical fixture data, verify expected state, and reset generated demo artifacts.

## Read-Only Checks

Test connectivity and inspect the configured database without changing data:

```powershell
.\.venv\Scripts\python.exe -m db.admin doctor --database final
```

Verify runtime state after a live run:

```powershell
.\.venv\Scripts\python.exe -m db.admin verify --database final --phase runtime
```

## Create And Seed

For a new guarded demo database:

```powershell
.\.venv\Scripts\python.exe -m db.admin create --database final --confirm final
.\.venv\Scripts\python.exe -m db.admin seed --database final --confirm final
.\.venv\Scripts\python.exe -m db.admin verify --database final --phase baseline
```

`create` fails if the target database already exists. `seed` requires the application tables to be empty before inserting the canonical roots.

## Reset Generated Artifacts

To keep the canonical roots while removing generated workflow outputs:

```powershell
.\.venv\Scripts\python.exe -m db.admin reset --database final --confirm final
```

This command is guarded. It refuses to run when the expected demo roots are not present or when non-demo traces are detected.

## Verification Phases

- `baseline`: validates the clean seeded state before live execution.
- `runtime`: validates the database after workflow outputs may have been written.

## Safety Rules

- Mutating commands require both `--database final` and `--confirm final`.
- Do not rerun `create` or `seed` against a populated database.
- Use reset only for the guarded demo environment, not for arbitrary production-like data.

## Related Guides

- Demo execution and acceptance: `demo/DEMO_WORKFLOW_GUIDE.md`
- Dashboard behavior over persisted data: `dashboard_app/README.md`
- Refund API behavior against the guarded database: `refund_app/README.md`