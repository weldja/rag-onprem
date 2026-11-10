# rag-onprem

Ask questions of your organisation's documents and get answers that cite their sources. Everything
runs on your own server, with no internet connection: the language model, the search model, the
database and the web interface all ship as Docker images.

- **Documents:** upload PDF, Word, PowerPoint, Excel, CSV, HTML, text and scanned images (OCR), or
  index up to three read-only file shares in place.
- **Answers:** hybrid search (meaning + keywords), answers cite document and page, and say so when
  the documents don't cover the question. Document summaries and follow-up questions are supported.
- **Access:** local accounts or Active Directory/LDAP; documents are visible only to the groups
  they're shared with, enforced in the database query.

## Documentation

| For | Read |
|---|---|
| Installing and running a release bundle | [docs/operations.md](docs/operations.md) |
| Developing: stack, constraints, commands | [CLAUDE.md](CLAUDE.md) |
| Answer quality and speed measurements | [docs/eval-results.md](docs/eval-results.md) |
| Milestone plan | [docs/build-plan.md](docs/build-plan.md) |

## Quick start (development, Windows + Git Bash or Linux)

```bash
bash scripts/dev-up.sh            # build and start the stack (creates .env on first run)
bash scripts/create-admin.sh you  # first administrator
# open https://localhost
bash scripts/test.sh              # lint, types and unit tests, network disabled
```
