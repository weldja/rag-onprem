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

**Response time depends on the GPU.** The answer model (Qwen3 4B) must fit entirely in GPU
memory to answer quickly. The installer sets the memory-related settings from the card's size
(only when it creates `.env`):

| GPU memory | Settings applied | Where the models run |
|---|---|---|
| 8 GB+ | defaults (`LLM_NUM_CTX=8192`, `LLM_MAX_TOKENS=1024`) | both on the GPU, with room to spare |
| 6–8 GB | `OLLAMA_KV_CACHE_TYPE=q8_0` | both on the GPU |
| under 6 GB (e.g. 4 GB) | `LLM_NUM_CTX=4096`, `LLM_MAX_TOKENS=768`, `EMBED_NUM_GPU=0`, `OLLAMA_KV_CACHE_TYPE=q8_0` | answer model on the GPU, search model on the CPU |
| no GPU | defaults | both on the CPU |

- **NVIDIA GPU with 8 GB+:** recommended. Answers start within 10 seconds.
- **4 GB GPU:** measured on a 4 GB NVIDIA T500 with real company documents: first words after
  about 10 seconds, complete answers about 14 seconds, document summaries about 30 seconds.
- **No GPU:** questions take 20–60 seconds or more, and summaries several minutes.

Check a new server with the checklist in **Checking a new GPU server** below.

**More precise answers (optional): the 7B model.** The bundle also contains
`qwen2.5:7b-instruct` (the `VERSION` file lists every answer model as `llm_models`). In our
evaluation it cited slightly more precisely and answered as accurately, but took about twice as
long. It needs an NVIDIA GPU with 8 GB+; on an 8 GB card also set `EMBED_NUM_GPU=0`, so the
search model doesn't compete with it for card memory (12 GB+ holds both). To switch, set
`LLM_MODEL=qwen2.5:7b-instruct` in `.env` and run `docker compose up -d`. Nothing is rebuilt or
downloaded, documents don't need re-indexing, and upgrades keep the choice. Switch back the same
way with `LLM_MODEL=qwen3:4b-instruct-2507-q4_K_M`.

Answers stream as they're written, so the first words appear before the answer is complete.
While a question is being answered, the page shows what's happening ("Searching your documents",
then "Reading 6 excerpts from 3 documents" with their names) and the seconds elapsed. A question
asked again that finds the same excerpts is answered at once from memory.

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
| `LLM_MAX_TOKENS`, `SUMMARY_CHUNKS` | longest answer in tokens (default 1024) and excerpts a document summary reads (default 6). Higher values give fuller answers and summaries but take longer. |
| `EMBED_NUM_GPU`, `LLM_NUM_CTX`, `OLLAMA_KV_CACHE_TYPE` | `0` keeps the search model on the CPU so it never displaces the answer model on a small GPU; context window of the answer model; `q8_0` halves the model's working memory so it fits a small card. Set from the GPU's memory by the installer (see **Requirements**). |
| `SHARE_1_PATH` ... `SHARE_3_PATH` and related | file shares indexed in place; see **File shares** below. |
| `ANSWER_CACHE_ENTRIES` | answers kept in memory for repeated questions (default 500; `0` turns it off). |
| `INDEX_PAUSE_MAX_S` | longest indexing waits (per excerpt) while answers are being written (default 120). |
| `EMBED_BATCH` | excerpts per embedding request while indexing (default 1, so questions never queue behind a batch). |
| `LLAMA_ARG_CACHE_RAM` | the model service's prompt cache in server memory, in MiB (default `0`, off: on small machines saving it slowed every question by up to 5 s). Try `8192` on a large server where many users ask about the same documents. The api loads both models when it starts, so the first question doesn't wait for them. |
| `PASSWORD_MIN_LENGTH` | minimum length of local passwords (default 12, lowest 1). Any characters are allowed, including spaces. Lowering it makes passwords easier to guess. |
| `LLM_MODEL` | the answer model: `qwen3:4b-instruct-2507-q4_K_M` (default) or another model listed in `LLM_EXTRA_MODELS` (see **Requirements**). |
| `LLM_EXTRA_MODELS`, `EMBED_MODEL`, `RAG_VERSION` | set by the bundle. Don't change them; the models are built into the images. |

### Checking a new GPU server

Do this once after installing on a server with an NVIDIA GPU of 8 GB or more, with a few documents
indexed.

1. **Both models on the GPU while answering.** Ask a question in the browser, then run
   `docker compose exec ollama ollama ps`. Both lines (`qwen3:4b-instruct-2507-q4_K_M` and
   `bge-m3`) should show `100% GPU` in the PROCESSOR column. On a card under 6 GB, `bge-m3`
   shows `100% CPU` by design.
2. **Both models stay on the GPU while indexing.** Upload a large document (or press **Scan now**
   on a share). While the Indexing page shows progress, ask a question and run `ollama ps` again.
   Both models must still be listed with `100% GPU`. If one is missing or shows a CPU/GPU split,
   the card can't hold both: set `EMBED_NUM_GPU=0` (or `OLLAMA_KV_CACHE_TYPE=q8_0`) in `.env` and
   run `docker compose up -d`.
3. **First words within 10 seconds.** Ask five questions about your documents, then run
   `docker compose logs api | grep "answer timing"`. `first_word` should be under `10000ms` on
   each line. A repeated question is answered from memory (`cached=True`), so it doesn't count.
4. **Note the results** (card model and memory, `ollama ps` output, `first_word` times) with your
   installation records. They help when comparing after an upgrade.

If step 1 fails on an 8 GB+ card, raise `LLAMA_ARG_FIT_TARGET` only if the ollama log shows
out-of-memory errors. A CPU/GPU split usually means another program is using the card:
`nvidia-smi` lists them.

## 3. Daily use

- **Documents:** upload PDF, Word, PowerPoint, Excel, CSV, HTML, text or Markdown files, and
  scanned images (PNG, JPEG, TIFF).
  - Each document is visible only to its uploader, administrators, and the groups it's shared with.
  - Indexing runs in the background, and the status column shows progress.
  - Password-protected PDFs can't be read; PDFs that only restrict editing or printing can.
  - **Summaries:** click **Summary** next to a file, or limit the question to one file (up to
    five) in the sidebar and ask "summarise this".
  - **Questions about the collection itself**, such as "how many documents are indexed?" or "which
    documents do you have?", are answered from the database: only the documents the user can see,
    within the sidebar's current scope. The model itself only ever reads a few excerpts and can't
    count documents.
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

### File shares

Instead of (or as well as) uploading, documents can be indexed straight from up to three folders,
such as a company file share. The left side of the **Ask** page lists each share with the path
it's indexed from, how many documents are ready or still indexing, when it was last checked, and
its folders. Click a folder, a share or a document to ask about just that (the question box
shows "Answering from ..."); click the ✕ to ask about everything again.

1. Mount the share on the server, read-only. For a Windows share, for example in `/etc/fstab`:
   `//fileserver/policies /mnt/policies cifs ro,credentials=/root/.smb-rag,iocharset=utf8 0 0`
   (the account only needs read access).
2. In `.env`:
   ```
   SHARE_1_PATH=/mnt/policies
   SHARE_1_NAME='Policies'
   SHARE_1_DISPLAY_PATH='\\fileserver\policies'
   SHARE_1_GROUPS='All staff'
   ```
   Keep the single quotes around values with spaces or backslashes.
3. `docker compose up -d`. The share is scanned within a few seconds, then every 5 minutes
   (`SHARE_SCAN_INTERVAL_S`). Administrators can click **Scan now** next to a share.

How it behaves:
- New and changed files are indexed and deleted files drop out of the index. A file is only read
  again when its size or date changes. Identical copies in several folders are indexed once.
- Files are never modified, copied or moved; the share is mounted read-only.
- Office lock files (`~$...`), temporary and hidden files, and recycle bins are ignored, as are
  unsupported types and files over `MAX_UPLOAD_MB`. The sidebar shows how many were skipped.
- **Who sees what:** every document on a share is visible to the groups in `SHARE_1_GROUPS` (and
  administrators). The share's own Windows permissions are **not** read. Keep folders that need
  different permissions out with `SHARE_1_EXCLUDE='HR/*,Board/*'`, and mount them as another share
  with its own groups.
- If the share is unmounted or unreachable it looks empty; nothing is removed from the index then,
  and the sidebar shows a warning.
- Share documents can't be deleted or re-shared in the app; change the share instead. Removing a
  share from `.env` keeps its documents until an administrator clicks **Remove** in the sidebar.
- Backups contain the index, not the share's files; back the share up as you do today.
- **The first scan of a large share can take hours** on a server without a big GPU (the
  embedding model runs on the CPU, a few seconds per excerpt; a 1,000-page manual is about 1,000
  excerpts). Questions can be asked meanwhile: indexing pauses while an answer is being written
  (for at most `INDEX_PAUSE_MAX_S`, default 120 s, per excerpt), so it doesn't slow answers down.

### Indexing page (administrators)

The **Indexing** tab lists every file the assistant knows about: uploads, files on the shares, and
share files it doesn't index (with the reason: file type not supported, empty, over the size
limit, or excluded by `SHARE_n_EXCLUDE`).

- **Filter** by status (Indexing, Queued, Failed, Indexed, Excluded, Not indexed), by source, and
  by name or folder. Files being indexed show progress in excerpts; queued files their place in
  the queue.
- **Re-index** an indexed or failed file.
- **Exclude** removes a file from the index and keeps it out: it isn't searched, and the share
  scan doesn't queue it again, even when the file changes. Excluding a queued file takes it off
  the queue ("Remove from queue"); excluding the file being indexed stops the worker at the
  next excerpt ("Stop & exclude").
- **Include** puts an excluded file back in the queue.
- Files are grouped by status (untick **Group by status** for one list). Each group's header
  has buttons that act on the whole group as filtered, for example **Remove all from queue**
  or **Re-index all**, and a box to select the group. Tick individual files, or the header box
  for everything listed, to act on a selection.

Excluding a file here is per file and reversible. To keep a whole folder out, use
`SHARE_n_EXCLUDE` instead (see *File shares*).

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

**Upgrading from 1.0.0-rc6 or earlier:** re-index once after the upgrade. On the **Indexing**
page, select the **Indexed** group and choose **Re-index all**. Questions keep working
meanwhile. Until a document is re-indexed it misses three improvements: each excerpt is now
indexed with its document's name (a question naming a form or manual finds excerpts deep
inside it), merged table cells in Word documents are read once rather than repeated, and
PDFs are read with a different reader that keeps the spaces between words (some PDFs used to
come out as "Mounttheserversinracks", which search couldn't match).

## 7. Troubleshooting

| Symptom | Check |
|---|---|
| Install says preflight failed | Read the FAIL lines. `FORCE=1 bash install.sh` continues anyway. |
| Not healthy after 15 minutes | `docker compose logs ollama api`. Most often there isn't enough RAM for the model. |
| Browser certificate warning | You're using the self-signed certificate. Install a real one in `certs/` and run `docker compose restart proxy`. |
| Document stuck in "pending" | `docker compose logs worker` |
| A share shows a warning or no documents | `docker compose exec worker ls /shares/1` should list the files. If it's empty, the share isn't mounted on the host, or `SHARE_1_PATH` is wrong. `docker compose logs worker \| grep Share` shows each scan. |
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
