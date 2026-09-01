# Fenris — Security & Data Handling Disclosure

**Version 1.0 — effective date: [DATE]**

This document states where your data physically sits, what defends it, and what Fenris deliberately does not defend. It exists because security claims should be checkable, and the source is open — every statement below maps to code you can read.

## 1. The data inventory

**On your machine:**

| Artifact | Location | Contains |
|---|---|---|
| The index (`fenris.db`) | `~/.local/share/fenris/` (XDG; overridable) | Chunk text, embedding vectors, lexical index, table records, sync cursor, eval results — i.e., **the readable text of everything you indexed** |
| Docling parse artifacts | Same directory, capped ~2 GB, LRU-evicted | Raw parsed document structure |
| OAuth refresh token | OS credential store (Keychain / Credential Manager / Secret Service) | A Google refresh token, read-only scope |
| Trace/eval logs | Local database | Your questions, retrieved spans, answer states |
| Gemini API key | Your environment / keyring | The key itself |

**Off your machine:** your Drive content (Google's servers, under your account), your questions plus retrieved chunks (Google's Gemini API, under your key). Nothing else. No telemetry, no analytics, no crash reporting, no update checks — the code contains no other network paths.

## 2. The threat model, plainly

**Defended:**

1. **Token theft from the filesystem.** The refresh token never touches disk in plaintext — it lives in the OS credential store, which prompts per access on macOS and requires an unlocked keyring on Linux.
2. **Drive writes.** The OAuth scope is `drive.readonly`. The software cannot modify your Drive even if fully compromised at the application layer.
3. **Lingering access after disconnect.** `fenris drive disconnect` calls Google's revocation endpoint and deletes the local credential. A token that is revoked cannot be replayed.
4. **Prompt injection through your documents.** Retrieved text is fenced as data with explicit instructions to the model; evidence is rendered through a deterministic citation resolver that cannot be talked into inventing sources; tools available to the answer loop are read-only local queries — no URL fetching, no shell, no Drive writes. A scanner flags known injection patterns at ingest time. This is a layered mitigation, and we say so: adversarial documents are a moving target, and no claim of complete immunity is made.
5. **Stale or revoked content serving.** The sync loop retires deleted/unshared files' index rows in the same transaction as its cursor, and retrieval filters on active rows during the search itself — a trashed file's chunks do not surface in BM25 or vector results after the sync that learned of it.
6. **Silent index corruption.** The database runs in WAL mode with full synchronous commits; the change cursor only advances alongside committed index writes, so crashes replay rather than skip.

**Not defended, by design — and why:**

1. **Local database encryption.** A single-user desktop database guarded by the OS user is not meaningfully protected by an application-layer passphrase — the same user who can read the DB can read the key. We rely on OS-level protections (FileVault, BitLocker, LUKS, and the OS login itself) and say this plainly instead of shipping a checkbox. If a future version ships encryption, it will be for a real threat (multi-user hosts, shared machines) and will say what it does and does not protect.
2. **A compromised operating system.** If your machine is compromised, the index — which contains your documents' text — is readable. No local application can defend against its own host.
3. **The network interface beyond localhost.** The web UI binds `127.0.0.1` only and has **no authentication** — it was never meant to face anything but your own browser. If you port-forward or reverse-proxy it, you are exposing your document index and question history without a password. Don't.
4. **What Google does with what it receives.** Drive storage and Gemini API processing occur under Google's policies for those products. We neither see nor control that processing; we chose the read-only scope and question-only transmission to minimize what reaches them, and the rest is between you and Google.

## 3. Supply chain

The Software depends on open-source packages installed from public registries (PyPI via pip, npm via bun). The lockfiles are committed; review them. We do not vendor binaries. `make setup` deliberately uninstalls a ~4.5 GB GPU dependency stack the default install pulls in — a size decision that is also an attack-surface decision, documented in `pyproject.toml`.

## 4. Vulnerability reporting

Report suspected vulnerabilities to **[SECURITY CONTACT EMAIL]**. The project is maintained by volunteers; treat disclosure timelines as best-effort. Do not open public issues for exploitable findings.

## 5. What we would tell you to check

1. Run `fenris drive status` after disconnecting elsewhere — confirm revocation took.
2. Review Google's connected-apps page periodically: the Fenris entry should exist only for accounts you connected yourself.
3. If your machine is shared, do not index confidential documents on it — see section 2, "Not defended."
4. The Sources page shows exactly what is indexed. Audit it the way you'd audit a filing cabinet.

---

*This disclosure is engineering documentation, not legal advice. The Terms of Service and Privacy Policy in this directory carry the legal framing; this file carries the facts.*
