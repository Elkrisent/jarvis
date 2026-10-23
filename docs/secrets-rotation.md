# Secrets and master key rotation

JARVIS stores upstream API credentials in the `secrets` table, encrypted. This page explains how,
and how to replace the master key. Requirement: SPEC §11.2.

## What is encrypted and how

* Algorithm: AES-256-GCM. Each record has its own random 96-bit nonce, so encrypting the same value
  twice gives different ciphertext. Tampering with the ciphertext or nonce fails authentication.
* The master key is 32 bytes, supplied as standard base64 in `JARVIS_MASTER_KEY`. Surrounding
  whitespace is tolerated.
* Each record is also bound to the id of the row that holds it (as AES-GCM associated data), so a
  ciphertext copied into another row fails authentication instead of decrypting there.
* Key ID: `k_` followed by the first 16 hex characters of the SHA-256 of the key (for example
  `k_1a2b3c4d5e6f7a8b`). It is stored on every row in `secrets.key_id`, so JARVIS can tell which key sealed a
  record. A key ID identifies a key; it does not reveal it.
* Plaintext secrets and keys are never written to logs, `repr`/`str` output, errors or command output.

The api uses the master key to seal provider secrets when an API is saved. The gateway uses it to
open them: the config it loads carries each provider secret as ciphertext (also in its local
snapshot file), and it decrypts them into memory whenever it installs a config. Plaintext is never
written to disk or logs.

## Generate a key

```bash
openssl rand -base64 32
```

Store it in your secret manager. Do not paste it into tickets, chat or shell history you share.

## Rotation runbook

1. Back up the database and keep the old key. Rotation is all-or-nothing, but a backup is your
   recovery path if something else goes wrong.
2. Stop the api, or accept that it may see errors while rotation runs. Rotation takes
   `LOCK TABLE secrets IN SHARE ROW EXCLUSIVE MODE` inside one transaction: concurrent writers to
   `secrets` wait until it finishes; readers are not blocked.
3. Generate the new key (above) and run the command with both keys in the environment. Keys are
   read only from environment variables, never from arguments:

   ```bash
   export JARVIS_MASTER_KEY='<old key>'
   export JARVIS_NEW_MASTER_KEY='<new key>'
   export DATABASE_URL='postgresql://...'
   jarvis rotate-master-key
   ```

   Success prints one line on stdout and exits 0:

   ```text
   re-encrypted <N> secret(s) from <old key id> to <new key id>
   ```

4. Replace `JARVIS_MASTER_KEY` with the new value wherever it is configured, and remove
   `JARVIS_NEW_MASTER_KEY`.
5. Restart the api and gateway. A running instance still holds the old key; anything it writes
   after rotation is sealed under the old key ID and would then be refused as an unknown key. A
   gateway that is not restarted can no longer open the re-encrypted secrets the next time it
   reloads config (at the latest on its next resync, within 60 s by default): requests to APIs with
   a stored credential then answer `JV_UPSTREAM_UNREACHABLE` ("the upstream credential is not
   available") and the gateway logs one warning per API naming the API id and key id, never a
   secret. Restarting it with the new key fixes this.
6. Verify: the services start and can read their secrets, and `jarvis rotate-master-key` is not
   needed again. Re-running a finished rotation (old and new keys unchanged) rewrites nothing and
   exits 0 with `re-encrypted 0 secret(s) ...`.
7. Destroy the old key only after you have verified the above and no backup you may restore from
   still needs it.

## Failure behavior

Rotation is one transaction. If anything fails, nothing is changed and the old key stays valid.
Failures print one line on stderr. Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Success (including a rotation that had nothing left to rewrite). |
| 1 | Rotation refused (`rotation refused: ...`): a record is sealed under an unknown key, or a record fails authentication under the old key. Or a database failure (could not connect, or rotation could not complete). |
| 2 | Usage or configuration problem: a variable is not set, a key is not 32 bytes of base64, `DATABASE_URL is invalid`, arguments were passed, or the old and new keys are identical. |

Exit 1 is safe to retry once the cause is fixed. A message beginning `database error` (rather than
`could not connect`) means the database answered but refused: usually the schema has not been
migrated (start the api once) or the database user may not lock the `secrets` table.

`DATABASE_URL` accepts `sslmode` and `connect_timeout`. Other libpq-only options
(`application_name`, `sslrootcert`, ...) are rejected with exit 2 and the option's name, because the
async driver cannot take them and ignoring one could weaken TLS verification.

## Unknown key IDs

`rotation refused: N secret(s) are sealed under unknown key(s) k_... ; expected <old> or <new>`
means some rows were sealed with a key that is neither the one in `JARVIS_MASTER_KEY` nor the one
in `JARVIS_NEW_MASTER_KEY`. Typical causes: a previous rotation was run with different keys, or an
instance still on an old key wrote records after a rotation. Nothing was changed. Find the key whose
ID matches (the ID is in the message), rotate from that key first, then rotate again. Do not delete
rows to get past it.

## Keys never appear in output

The command prints key IDs and counts only. Errors name the variable or the exception class, not
its text, because driver messages can contain a database URL or password.
