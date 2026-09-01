# Fenris — Privacy Policy

**Version 1.0 — effective date: [DATE]**

This policy explains what data the Fenris software handles, where each piece goes, and what stays on your machine. It is written for the software's actual behavior, not for a generic web service — Fenris is a locally run, open-source application, and most of what a privacy policy normally covers does not apply, because the operator who would collect your data is *you*.

**Who is the data controller here?** You are. You run the Software on your machine, you point it at your own Drive, and you supply the API key. The Fenris project ("**we**") has no servers receiving your data, no telemetry, and no accounts. Where the Software's operation causes a third party (Google) to process content, this policy says exactly which content, and Google's own policies govern that processing.

If a statement here ever disagrees with the Software's actual behavior, that is a bug, and the code is public — report it.

## 1. The data map, up front

| Data | Where it lives | Who can see it |
|---|---|---|
| Your Drive documents' content | Google Drive (yours), and a local SQLite index on your machine | You; Google for the Drive copy; nobody else |
| Your questions and the chunks used to answer them | Sent to the Gemini API under your key | You; Google as the model provider |
| Model-generated answers | Your terminal and local UI only | You |
| The OAuth refresh token | Your OS credential store (Keychain / Credential Manager / Secret Service) | You; nobody else — not in files, not with us |
| Your Gemini API key | Your environment / keyring on your machine | You and Google |
| Sync cursors, index metadata, eval results | The local database | You |
| Anything you contribute to the public repository | GitHub | The world, per GitHub's policies |

There is no telemetry, analytics, crash reporting, advertising, or tracking of any kind in the Software. It contains no code that transmits anything except: (1) Drive API calls reading your own files, and (2) Gemini API calls carrying your questions and the retrieved context needed to answer them. Both use credentials you supply.

## 2. What the Software reads from your Drive

When you run a sync, the Software calls the Google Drive API under the read-only scope to:

1. **List file metadata** — names, types, sizes, checksums, modification times, parent folders. This builds the sync ledger.
2. **Download content** for files it ingests — parsed locally into chunks, embeddings, and a searchable index in the local database.
3. **Read the change feed** — the API's record of what changed since the last sync, so edits and deletions are picked up without full re-reads.

The scope is `drive.readonly`. The Software cannot write, modify, move, trash, or share anything in your Drive, and it never attempts to. Google logs these API calls against your account in your Google Cloud console, where you can audit them.

## 3. What the Software sends to the Gemini API

To answer a question, the Software sends, using your API key:

1. **The question text** you typed.
2. **The retrieved chunks** — the passages from your documents the retrieval stage judged relevant, including their titles and the contextual headers built from file paths and section names.
3. **A fixed system prompt** instructing the model to treat retrieved text as data, cite evidence IDs, and abstain rather than guess.

That request is processed by Google under the Gemini API's terms and its data-handling practices for that product. **The full content of your documents never goes** — only the chunks selected as relevant to the specific question. Embedding at ingest time likewise sends chunk text (title, path context, and the chunk's body) to be vectorized. If you use Google's batch embedding mode, the same content is transmitted with different pricing and scheduling, not different privacy scope.

If any of that is unacceptable for certain documents, the honest options are: don't index them (the Sources page shows what's indexed), or run the Software without generation configured.

## 4. What never leaves your machine

1. **The index and database.** All chunks, vectors, lexical indexes, tables, and sync state live in one SQLite file under your data directory. Deleting the directory deletes every copy of your content the Software holds.
2. **The web UI.** Binds to `127.0.0.1` only. It is not reachable from other machines, and it sends no analytics.
3. **OAuth credentials.** The refresh token lives in the OS credential store. `fenris drive disconnect` revokes it at Google and deletes the local entry.
4. **API keys.** Read from your environment; never written into the database or logs.

## 5. Data retention and deletion

1. **Local data persists until you delete it.** `fenris reindex` rebuilds the index from Drive without the old database; deleting the data directory removes everything, including eval results and traces.
2. **Drive-side deletion propagates.** When a file is deleted or unshared from your Drive, the next sync retires its index entries, and they stop appearing in answers. The retired rows remain in the database (marked inactive) until you purge or rebuild.
3. **Traces and logs.** The Software records per-query traces (route taken, chunks considered, citations resolved) locally for debugging and its evaluation suite. These contain your questions and quoted spans of your documents. They are local, and the retention window defaults to 30 days, configurable.
4. **Google's retention.** What Google retains from Drive storage and Gemini API calls is governed by Google's policies for those products, not by us — we never see it in the first place.

## 6. Children

The Software is not directed at children and is not intended for users under 18. It is a tool for adults managing their own documents.

## 7. Your rights, concretely

Because the project holds no personal data, most data-subject requests route to the party that actually holds the data:

1. **Access, correction, deletion of your Drive content:** Google's Drive tools, or just your Drive.
2. **Access, deletion of your local index and traces:** the local database — delete it, or run `fenris reindex`.
3. **Withdraw Drive access:** `fenris drive disconnect`, or Google's security page listing connected apps.
4. **Stop model processing of your content:** delete the API key and stop asking questions; indexing alone embeds chunk text, so if that is unwanted, stop indexing too.
5. **What we hold:** nothing beyond public repository contributions, handled under GitHub's terms.

GDPR and state-privacy-law language: to the extent the Software processes personal data, the processing runs on infrastructure you control, as controller, and the project is not a processor for it. The third-party processors your configuration engages are Google (Drive storage and APIs) — whose policies describe lawful bases, transfers, and safeguards — and any model provider whose key you configure.

## 8. Security

1. **Local boundary.** The index contains your documents' text. The machine's OS protections are the boundary; the database is not encrypted (stated plainly in our security disclosure rather than hidden behind a checkbox).
2. **Token storage.** OS credential store, not files; revocation on disconnect; least-privilege read-only scope.
3. **Untrusted content.** Documents are treated as untrusted input. Retrieved text is fenced as data in prompts, the model cannot extend its permissions through a document, and a scanner flags known injection patterns. These are real mitigations, honestly not guarantees.
4. **Disclosure.** No security telemetry exists. If you find a vulnerability, the security notes in the repository describe responsible disclosure to the maintainers.

## 9. Changes to this policy

The policy changes if the Software's data flows change — a new model provider, a new local feature. The effective date above moves with it. Because we have no mailing list for your data-protection choices, the changelog of the repository is the notice mechanism.

## 10. Contact

**[CONTACT EMAIL]** — for questions about this policy or the Software's data handling.

---

*Adapted for Fenris from attorney-drafted templates by [General Legal](https://github.com/General-Legal/legal-templates) (CC0 1.0). This document describes the Software's behavior accurately to the best of the project's knowledge, but it is not legal advice; have an attorney review it for your jurisdiction before relying on it.*
