# PointNXT MCP Production Session-Database Migration Runbook

This runbook describes a controlled migration of the existing SQLite session
database for the `pnxt-mcp` Docker Compose deployment. It is documentation
only. No migration, backup, volume creation, or production restart has been
performed.

## Known deployment facts

- Container: `pnxt-mcp`
- Compose file: `/srv/mcp/pnxt-mcp/docker-compose.yml`
- Current database: `/app/pointnxt-sessions.sqlite3`
- Current container mounts: none
- SQLite sidecars observed: `pointnxt-sessions.sqlite3-wal` and
  `pointnxt-sessions.sqlite3-shm`
- Proposed volume: `pnxt-mcp-data`
- Proposed database path: `/var/lib/pnxt-mcp/pointnxt-sessions.sqlite3`
- Production session encryption is required.

The operator must confirm the actual deployment, secret-management, backup,
and change-control procedures before proceeding. Do not infer them from this
repository.

## Stop/go criteria

Proceed only when all of the following are true:

- A maintenance window and owner are approved.
- The current container and Compose project have been identified from the
  active deployment, not from local assumptions.
- The current encryption key has been located in the approved secret manager,
  without displaying or copying its value into chat, logs, or shell history.
- A consistent, restorable backup of the SQLite database has been created and
  verified.
- The original database, original container state, and any existing storage
  remain preserved.
- The new volume and migration destination are approved.
- A rollback owner and tested rollback procedure are available.
- A staging or maintenance validation plan exists for authentication and OAuth.

Stop immediately if the key source, backup destination, database ownership,
Compose project, or rollback path is unknown.

## Phase 1: Read-only preflight

The following commands are inspection-only. Run them on the authorized
production host and do not print environment variables or database contents.

```bash
cd /srv/mcp/pnxt-mcp
docker ps --filter name=pnxt-mcp --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
docker inspect pnxt-mcp --format '{{json .Mounts}}'
docker inspect pnxt-mcp --format '{{.Config.WorkingDir}}'
docker volume ls
# Optional candidate lookup only; this is not proof of the Compose volume identity.
docker volume inspect pnxt-mcp-data 2>/dev/null || true
```

Do not inspect or print the complete container environment. Verify required
variable names and set/unset status through the approved deployment or secret
manager interface. If the container environment must be checked, use an
operator-approved name-only method that emits entries such as
`MCP_PUBLIC_BASE_URL=set` or `MCP_OAUTH_REDIRECT_URI=unset`, never the value.
The exact method is **NOT VERIFIED** and must be supplied by the operator.

Confirm the current database path without opening it:

```bash
docker exec pnxt-mcp sh -c 'test -e /app/pointnxt-sessions.sqlite3 && echo database-present || echo database-missing'
docker exec pnxt-mcp sh -c 'for f in /app/pointnxt-sessions.sqlite3-wal /app/pointnxt-sessions.sqlite3-shm; do test -e "$f" && echo "$(basename "$f"):present" || echo "$(basename "$f"):absent"; done'
```

Do not proceed if the expected container, database, or sidecars differ from
the known facts without an operator-approved revised plan.

## Phase 2: Consistent SQLite backup

SQLite uses WAL mode in this application. Copying only the main database file
while the service is writing can omit committed WAL frames. Use one approved
method and record its result.

The preferred method is an SQLite-aware backup while the application is still
running, if the production image includes the SQLite CLI and the operator has
supplied and approved a real host backup destination or an explicitly mounted
backup location. The destination is **NOT VERIFIED** and must not be replaced
with a guessed path.

Before running any backup command, the operator must verify that the approved
destination is:

- Mounted or otherwise reachable from the maintenance context.
- Durable outside the container writable layer.
- Writable by the approved backup procedure.
- Accessible outside the container for independent verification.
- Covered by the approved backup and retention policy.

The exact backup command and destination are intentionally omitted until the
operator supplies and approves them. Do not run a command that writes to an
unmounted placeholder path.

If the SQLite CLI is unavailable, use the approved maintenance procedure to
stop application writes first, then copy the database together with its WAL
and SHM state or checkpoint it according to the storage team’s procedure. Do
not invent a backup destination or delete the sidecars before consistency is
verified.

After backup, perform read-only checks on the backup using an approved host
or staging copy. Do not print rows or payloads:

```bash
# READ-ONLY against the backup copy.
sqlite3 <OPERATOR_APPROVED_BACKUP_FILE> 'PRAGMA integrity_check;'
sqlite3 <OPERATOR_APPROVED_BACKUP_FILE> 'PRAGMA journal_mode;'
```

`<OPERATOR_APPROVED_BACKUP_FILE>` is a documentation placeholder, not a
runnable path. The expected integrity result is exactly `ok`. Record the
approved backup path, timestamp,
operator, file size, checksum, and restore-test result in the change record.
Do not proceed unless the SQLite-aware backup completed successfully and this
integrity check passed.

## Phase 3: Preserve encryption configuration

The current `MCP_SESSION_ENCRYPTION_KEY` must remain unchanged. Retrieve it
only through the existing approved secret-management process. Do not print it,
place it in a command line, commit it, or paste it into a Compose file.

Before migration, confirm through secret-manager metadata or a controlled
configuration check that:

- The secret exists.
- The same secret will be supplied to the replacement container.
- Production encryption enforcement remains enabled.
- All replicas, if any, use the same key and storage.

If the deployment uses an external secret reference, preserve that reference.
If the source is unknown, stop and obtain an operator decision.

## Phase 4: Prepare the destination

The following are mutating commands and must not be run until the backup,
secret, and rollback gates are approved.

The operator may create the named volume using the approved Compose workflow:

```bash
# MUTATING: example only; do not run until the resolved Compose volume identity
# has been determined and approved.
cd /srv/mcp/pnxt-mcp
docker volume create <OPERATOR_APPROVED_RESOLVED_VOLUME_NAME>
```

The Compose declaration `pnxt-mcp-data` is a logical key, not a guaranteed
Docker volume name. Before creating or using storage, the operator must:

1. Determine the active Compose project name from the authorized deployment
   metadata or Compose invocation.
2. Resolve the declared volume to the actual Docker volume name that Compose
   will mount. The result is **NOT VERIFIED** in this repository.
3. Inspect that resolved volume and confirm its driver, mountpoint, ownership,
   and whether it already contains approved data.
4. Confirm the migration copy procedure and the application startup procedure
   reference the same resolved volume.
5. Record the resolved name in the change record.

Do not assume the resolved name is `pnxt-mcp-data`, and do not create a second
volume with a guessed name.

The optional Phase 1 lookup of `pnxt-mcp-data` is non-authoritative. It may
return no volume or may inspect a different volume from the one Compose will
mount. Only the volume resolved from the active Compose project and service
configuration may be used for migration or startup.

The destination path must be mounted at `/var/lib/pnxt-mcp`, with the database
configured as:

```text
POINTNXT_SESSION_STORE_PATH=/var/lib/pnxt-mcp/pointnxt-sessions.sqlite3
```

Do not start the replacement application against an empty destination. The
database must be copied into the mounted volume before the application is
allowed to serve traffic.

## Phase 5: Migrate the database

The migration is blocked until the operator supplies and approves the real
backup destination, resolved Docker volume name, and SQLite-aware copy method.
These values are **NOT VERIFIED** here. Use an approved maintenance container
or host-side procedure that can access both the verified backup and that exact
volume.

The migration must:

1. Preserve the original container and its original database.
2. Copy the verified SQLite-aware backup into the mounted destination; do not
   copy the live database file while WAL is active.
3. Preserve the database filename expected by `POINTNXT_SESSION_STORE_PATH`.
4. Apply ownership and permissions appropriate for the current root-running
   process. Since the current process runs as root, do not introduce a
   different container user during this migration without a separate review.
5. Avoid copying secrets, unrelated runtime files, or an unverified database.
6. Preserve the original database and its `-wal` and `-shm` sidecars until the
   migration is verified and rollback is no longer required.

Do not delete the original database, sidecars, backup, or any volume as part
of this migration.

After copying, run read-only checks against the destination database before
starting the application:

```bash
# TEMPORARY RESOURCE OPERATION: creates a short-lived maintenance container;
# it is read-only against the mounted volume and removes only that temporary
# container after completion. Run only with explicit operator approval.
# Placeholders are not runnable until the operator has resolved the exact
# volume name and approved an image with SQLite tooling.
docker run --rm -v <OPERATOR_APPROVED_RESOLVED_VOLUME_NAME>:/var/lib/pnxt-mcp:ro <approved-sqlite-image> \
  sqlite3 /var/lib/pnxt-mcp/pointnxt-sessions.sqlite3 'PRAGMA integrity_check;'
```

The expected result is exactly `ok`. The image name is intentionally an
operator decision. Do not pull an image without following the organization’s
image-approval process. If temporary Docker containers are not permitted,
use an approved host-side or already-running maintenance method instead. Abort
if the destination file is absent, the volume identity is uncertain, the
destination is empty, or the check does not return exactly `ok`.

## Phase 6: Start and validate

Start the replacement only after the destination database and encryption
configuration have passed the gates:

### Mandatory pre-start gate

Immediately before any application-start or container-replacement command,
the operator must confirm all of the following:

- The resolved volume name is the one the Compose service will mount.
- `/var/lib/pnxt-mcp/pointnxt-sessions.sqlite3` exists in that destination.
- An approved maintenance method ran `PRAGMA integrity_check` against that
  destination database.
- The result was exactly `ok`.
- The destination is not empty and is not an uninitialized volume.

Abort if any check fails or if the volume identity is uncertain. Never use
`docker compose up -d pnxt-mcp` as the first test of whether the destination
is populated.

```bash
# MUTATING: starts or recreates the local Compose service.
cd /srv/mcp/pnxt-mcp
docker compose up -d pnxt-mcp
```

Immediately inspect status without printing environment values:

```bash
docker ps --filter name=pnxt-mcp --format 'table {{.Names}}\t{{.Status}}'
docker inspect pnxt-mcp --format '{{json .Mounts}}'
docker logs --since 5m pnxt-mcp
```

Log inspection is restricted to authorized operators. Prefer targeted health
checks and status/mount inspection when they are sufficient. Review logs
locally for startup, encryption, database, or migration failures, and redact
identifiers, tokens, customer data, and secret values before sharing any log
excerpt.

Application validation should use a controlled reviewer account and approved
test tenant:

- Confirm the MCP server starts with encryption enforcement enabled.
- Confirm the configured session path is the mounted path.
- Confirm an existing approved test session can be read.
- Start an OAuth authorization request and verify explicit consent appears.
- Approve consent and exchange the authorization code with the correct PKCE
  verifier.
- Verify MCP access and refresh tokens are issued without backend token values.
- Verify refresh rotation and replay rejection.
- Verify token-specific revocation.
- Verify logout revokes the associated MCP grants.
- Verify an unrelated controlled tenant remains isolated.

Do not use real customer accounts or production business data for these tests.

## Rollback

Rollback is permitted only if the original database and backup remain intact.

1. Stop accepting traffic using the approved traffic-control procedure.
2. Preserve the new named volume; do not remove it.
3. Preserve the migration logs and validation results.
4. Restore the previous Compose configuration and original container/storage
   arrangement from the approved change record.
5. Start the original deployment with the unchanged encryption key.
6. Verify the original database and authentication behavior using the approved
   test account.
7. Leave `pnxt-mcp-data` untouched for investigation and later retry.

Do not run `docker compose down -v`, `docker volume rm`, `docker system prune`,
or any command that deletes the original database, backup, or new volume.

## Failure handling

If any integrity, encryption, mount, authentication, consent, token, logout,
or tenant-isolation check fails:

- Stop the migration and do not broaden permissions or disable encryption.
- Do not retry against the original database without recording the failure.
- Preserve both storage locations and all evidence.
- Invoke the approved rollback procedure if the replacement is serving traffic.
- Escalate to the deployment owner and security owner.
- Do not rotate or replace the encryption key as an ad hoc fix.

## Post-migration persistence test

Run this only against the approved deployment after functional validation:

1. Establish a controlled test authentication session and record only a
   non-sensitive test-session identifier.
2. Confirm the session is usable.
3. Replace the container using the normal Compose replacement procedure while
   keeping `pnxt-mcp-data` attached.
4. Confirm the replacement container has the same mount destination.
5. Confirm the test session remains readable and encrypted session access still
   works.
6. Confirm OAuth consent, refresh, revocation, and logout behavior remain
   correct after replacement.

The test proves persistence only if the container is replaced while the named
volume remains intact. It must not use `down -v`, volume removal, pruning, or
an unapproved production database copy.

## Operator decisions still required

- Approved backup destination and retention policy.
- SQLite-aware backup method available on the host/image.
- Secret-manager source and rotation policy for `MCP_SESSION_ENCRYPTION_KEY`.
- Exact production Compose project and change-control procedure.
- Volume ownership and filesystem policy.
- Whether the service will remain single-replica.
- Approved staging/test tenant and reviewer account.
- Traffic cutover and rollback mechanism.
- Approved diagnostic image or host tooling for SQLite integrity checks.

