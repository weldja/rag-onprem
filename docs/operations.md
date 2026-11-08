# rag-onprem: installation and operations

rag-onprem answers staff questions from your company's documents, with references to the source
pages. Everything runs on this server. It makes no internet connections, needs no cloud services
and sends no telemetry. The model, search index and documents never leave the machine.

Run all commands from the bundle folder, as a user allowed to run Docker.

## 1. Requirements

| | Minimum (works, slowly) | Required for answers within 10 seconds |
|---|---|---|
| OS | 64-bit Linux with Docker Engine 24+ and the Compose v2 plugin | Ubuntu 22.04/24.04 LTS, RHEL 9 |
| CPU | 4 cores with AVX2 | 8+ cores |
| RAM | 12 GB | 16 GB+ |
| Disk | 25 GB free for Docker | 40 GB+, plus room for documents and backups |
| GPU | none | **NVIDIA with 8 GB+ VRAM** and the NVIDIA Container Toolkit |
| Network | ports 80 and 443 reachable from staff PCs | a DNS name and a certificate for it |

**Response time depends on the GPU.** The model must fit entirely in GPU memory to answer quickly:

- **NVIDIA GPU with 8 GB+:** the whole model runs on the GPU. This is the configuration for
  answers within 10 seconds.
- **Smaller GPU (e.g. 4 GB):** part of the model runs on the CPU. Questions take around
  30 seconds and document summaries a few minutes.
- **No GPU:** questions take 20–60 seconds or more, and summaries several minutes.

Answers stream as they're written, so the first words appear before the answer is complete.

`bash preflight.sh` checks all of this and changes nothing.

## 2. Install

1. Copy the bundle folder (or the `.tar`) to the server. Extract the `.tar` if that's what you
   copied.
2. Optional but recommended: put your TLS certificate in `certs/` (see `certs/README.md`).
   Without one, the installer creates a self-signed certificate and browsers will warn.
3. Run `bash install.sh`. It:
   - checks the server
   - verifies the bundle checksums
   - loads the images
   - creates `.env` with a random database password
   - turns on the GPU if one is usable
   - starts everything

   The first start loads the model into memory, which can take a few minutes.
4. Create the first administrator: `bash create-admin.sh alice`. There's no default account or
   password.
5. Open `https://<server>/`, sign in, and add users and groups under **Admin**.

Configuration lives in `.env`, which is readable only by its owner. After changing it, run
`docker compose up -d`.

| Setting | Meaning |
|---|---|
| `HTTP_BIND`, `HTTP_PORT`, `HTTPS_PORT` | where the web interface listens |
| `MIN_SIMILARITY`, `TOP_K`, `CHUNK_SIZE`, `CHUNK_OVERLAP` | retrieval tuning (defaults were chosen by evaluation) |
| `PASSWORD_MIN_LENGTH` | minimum length of local passwords (default 12, lowest 1). Any characters are allowed, including spaces. Lowering it makes passwords easier to guess. |
| `LLM_MODEL`, `EMBED_MODEL`, `RAG_VERSION` | set by the bundle. Don't change them; the models are built into the images. |

## 3. Daily use

- **Documents:** upload PDF, Word, PowerPoint, Excel, CSV, HTML, text or Markdown files, and
  scanned images (PNG, JPEG, TIFF).
  - Each document is visible only to its uploader, administrators, and the groups it's shared with.
  - Indexing runs in the background, and the status column shows progress.
  - Scanned PDF pages and images are read with OCR on this server. Scanned documents take longer to
    index, roughly a few seconds per page. Clean scans at 300 dpi read best; photos of paper and
    handwriting may read poorly.
  - OCR reads English by default. For other languages, set `OCR_LANGUAGES` in `.env`, for example
    `eng+deu`. The bundle must include those languages; check `ocr_languages` in `VERSION` and ask
    for a bundle built with them if they're missing.
- **Users and groups:** managed under **Admin**.
  - New users join **All staff** by default.
  - Removing someone from a group takes effect immediately.
  - Disabling an account signs it out everywhere.
  - Accounts lock for 5 minutes after 5 wrong passwords.
- **Lost admin password:** `docker compose exec api python -m app.admin reset-password alice --password-stdin`
  (type the new password, then press Enter).

### Directory sign-in (Active Directory / LDAP)

Staff can sign in with their company account instead of a separate password. Set the `LDAP_*`
settings in `.env`, then run `docker compose up -d`. If the configuration is incomplete, the API
refuses to start and says what's missing (`docker compose logs api`).

- **What you need from the directory team:**
  - the server address (`ldaps://dc1.example.local`)
  - the CA certificate that signed the directory's certificate
  - a read-only service account to look users up
  - the OU where staff accounts and groups live
- **CA certificate:** save it as `certs/ldap-ca.pem` and set `LDAP_CA_FILE=/etc/rag/certs/ldap-ca.pem`.
- **Encryption:** the connection is always encrypted and the certificate always checked.
  - Use `ldaps://`, or `ldap://` with `LDAP_START_TLS=true`.
  - Plain unencrypted LDAP is refused.
- **Who can sign in:**
  - On first sign-in an account is created automatically and shown as "directory" under Admin.
  - Its password stays in the directory: change it there, not here.
  - `LDAP_REQUIRED_GROUP` limits sign-in to members of one directory group.
- **Groups:**
  - `LDAP_GROUP_MAP` links directory groups to app groups, for example
    `'{"CN=Finance,OU=Groups,DC=example,DC=local": "Finance"}'`.
  - Mapped app groups are created if needed and kept in step with the directory at every sign-in.
  - Unmapped app groups are left for administrators to manage by hand.
  - For nested AD groups, use the `LDAP_GROUP_FILTER` shown in `.env.example`.
- **Administrators:** if `LDAP_ADMIN_GROUP` is set, admin rights follow that group at every
  sign-in, so manual changes made under Admin are overwritten. If it isn't set, admin rights are
  managed in the app.
- **Keep a local administrator** (`bash create-admin.sh`) with a name that doesn't exist in the
  directory. Local accounts always sign in with their own password, even when the directory is
  unreachable.
- **Quoting:** single-quote values in `.env` that contain `( ) & { }` or spaces, as in the
  examples.

If the directory can't be reached, directory users see "The company directory can't be reached
right now"; local accounts are unaffected. Disabling an account under Admin blocks it here, whatever
the directory says.

## 4. Backups

```bash
bash backup.sh                 # writes backups/rag-onprem-<UTC time>/
bash backup.sh /mnt/nas/rag    # or to another location
```

A backup contains:
- the database: users, groups, documents and the search index
- the original uploaded files
- the configuration (`config/.env` and `config/certs/`)
- a manifest and checksums

**It holds the database password and the TLS private key.** Store it as securely as the server.

Schedule it nightly, for example with cron (`crontab -e`):

```
30 2 * * *  cd /opt/rag-onprem-1.0.0 && bash backup.sh /mnt/nas/rag >>backups.log 2>&1
```

Copy backups off the server, and delete old ones yourself.

## 5. Restore

```bash
bash restore.sh backups/rag-onprem-20261107-023000
```

This **replaces all current data** with the backup, and asks you to type `restore` to confirm.

The backup must come from the same rag-onprem version as the install: restore first, then
upgrade. `restore.sh` doesn't change `.env` or `certs/`.

To move to a new server:
1. Install the same bundle version there.
2. Copy `config/.env` and `config/certs/` from the backup into the bundle folder.
3. Run `docker compose up -d`.
4. Run `bash restore.sh`.

## 6. Upgrade

From the current installation folder:

```bash
bash upgrade.sh /path/to/rag-onprem-1.1.0
```

It:
1. verifies the new bundle
2. takes a backup
3. loads the new images
4. replaces the scripts and compose files (keeping `.env` and `certs/`)
5. adds new settings with their defaults
6. starts the new version, which applies database migrations automatically

**Rollback:** the old images stay on the server.
1. Set `RAG_VERSION` back to the old version in `.env`.
2. Run `docker compose up -d`.
3. Run `bash restore.sh <backup made by upgrade.sh>`.

Don't skip this step: the new version may already have migrated the database.

If a release changes the embedding model, documents need re-indexing. They're shown as
"needs re-index" in **Documents**, and the release notes will say so.

## 7. Troubleshooting

| Symptom | Check |
|---|---|
| Install says preflight failed | Read the FAIL lines. `FORCE=1 bash install.sh` continues anyway. |
| Not healthy after 15 minutes | `docker compose logs ollama api`. Most often there isn't enough RAM for the model. |
| Browser certificate warning | You're using the self-signed certificate. Install a real one in `certs/` and run `docker compose restart proxy`. |
| Document stuck in "pending" | `docker compose logs worker` |
| A document shows "failed" | The error is shown next to it. Often the PDF is encrypted or damaged, or the scan is too poor to read. |
| Answers are slow | `docker compose logs api \| grep "answer timing"` shows each answer's search time, time to first word and total time (no question or document text is logged). Then `docker compose exec ollama ollama ps`: the PROCESSOR column should say "100% GPU". A CPU/GPU split means the model doesn't fit the card: try `OLLAMA_KV_CACHE_TYPE=q8_0` in `.env`, then `docker compose up -d`. Without a GPU, slow answers are normal; check that `docker compose ps` shows no restarts and the server isn't swapping. |
| The model service crashes after enabling the GPU | Raise `LLAMA_ARG_FIT_TARGET` in `.env` to 768 or 1024, then `docker compose up -d`. |
| "The company directory can't be reached" | `docker compose logs api` shows the LDAP error. Check `LDAP_URL`, the firewall, the service account password, and that `certs/ldap-ca.pem` signed the directory's certificate (expired certificates are refused). |
| "The local model is unavailable" | `docker compose restart ollama`, then wait for it to become healthy. |

Status: `docker compose ps`. Logs: `docker compose logs -f api worker`. Logs stay on this server
and don't include document text.

## 8. Security notes

- Only nginx publishes ports. The database, model runtime, API and worker sit on an internal Docker
  network with no route to the internet. No component phones home.
- Sessions use HttpOnly, Secure, SameSite=Strict cookies and are stored server-side. Passwords are
  hashed with Argon2id.
- After installing a real certificate, consider enabling HSTS: uncomment
  `Strict-Transport-Security` in `nginx/templates/default.conf.template`.
- Backups contain secrets. Protect them accordingly.

## Appendix: updating pinned images (developers)

Every base image (Python, uv, Node, nginx, Ollama, pgvector, Alpine) is pinned by digest in its
Dockerfile, `deploy/docker-compose.yml` and `scripts/make-dev-cert.sh`. To update one:
1. Resolve the new digest with `docker buildx imagetools inspect <image>:<tag>`.
2. Update the pin.
3. Run the full test suite and the eval (`docs/eval-results.md`).
4. Then build a release: `bash scripts/build-release.sh X.Y.Z`.
