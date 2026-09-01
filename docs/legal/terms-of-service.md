# Fenris — Terms of Service

**Version 1.0 — effective date: [DATE]**

These Terms of Service ("**Terms**") govern your use of the Fenris software, including its source code, command-line interface, local API, and web interface (collectively, the "**Software**"), together with any associated repositories, documentation, and websites operated by the Fenris project maintainers ("**Fenris**," "**we**," "**us**"). The Software is an open-source, locally run application. Fenris is not a hosted service: except where you explicitly obtain builds, packages, or documentation from us, the Software runs entirely on hardware you control.

**These are not boilerplate web terms.** The Software's position in your data flow is unusual and the Terms say exactly where responsibility sits: you run the Software, you connect it to your own Google Drive, and you decide what leaves your machine. Read Section 4 (What the Software does with your data) and Section 9 (No warranty) with particular care.

By installing, copying, or otherwise using the Software, you agree to these Terms on behalf of yourself or the entity you represent, and you confirm you have the authority to do so. If you do not agree, do not use the Software.

## 1. The license

1. **The code.** The Software is released under the MIT License. The MIT License governs your rights to use, copy, modify, and distribute the source code, and its terms control over these Terms for anything the MIT License covers. Where these Terms and the MIT License conflict, the MIT License wins.
2. **These Terms cover the rest.** Anything the MIT License does not address — acceptable use of the project's name, contribution terms, third-party service interactions, and the disclaimers below — is governed by these Terms.
3. **No license to your data.** Nothing in these Terms gives Fenris any right to your documents, your index, your questions, or your answers. Section 4 explains the actual data flows.

## 2. Who Fenris is, and what Fenris is not

1. **Not a service provider.** Fenris does not host, operate, or have access to your instance of the Software. We do not receive your documents, your search index, your questions, or your answers as part of the Software's normal operation.
2. **Not your Google Drive.** The Software reads from Google Drive under an OAuth scope you grant. The scope is `drive.readonly` — read-only. The Software never writes to, moves, renames, or deletes your Drive content. Google's own terms govern your Drive account; these Terms govern only the Software.
3. **Not a lawyer, doctor, accountant, or anything else.** Answers the Software produces are retrieval from your own documents. They can be wrong, stale, incomplete, or misread. See Section 9.
4. **You are the operator.** You chose to install the Software on your machine, connect it to your Drive, and configure it. Responsibilities that follow from that — legal compliance, Drive account standing, API key costs — are yours.

## 3. Accounts and authentication

1. **Google OAuth.** The Software authenticates to Google Drive using OAuth 2.0 with PKCE. When you approve the connection, Google issues a refresh token. The Software stores that token in your operating system's credential store (macOS Keychain, Windows Credential Manager, or Linux's Secret Service) — not in a plaintext file, not with us, because we have no servers.
2. **Read-only, and you can end it.** The scope is read-only Drive access. Running `fenris drive disconnect` calls Google's revocation endpoint and deletes the stored token. After revocation, the Software cannot access your Drive.
3. **What Google gets.** When you authenticate, Google processes that interaction under Google's own privacy policy. The Software's privacy document explains what we — the project — receive in that flow, which is nothing except what you publish to the repository or send us as a contribution.
4. **Your API keys.** You supply a Gemini API key. That key belongs to your Google Cloud project, is stored in your environment, and its costs and usage limits are between you and Google.

## 4. What the Software does with your data

This is the section that matters. The Software is designed so that your content stays under your control, and these Terms describe it precisely.

1. **Your documents.** The Software reads your Drive files (content and metadata) over Google's API when you run a sync. Files are parsed and reduced to chunks, embeddings, and a lexical index, all stored in a SQLite database **on your machine**. The database is a rebuildable projection: deleting it loses no Drive content, only the index.
2. **Your questions.** To answer a question, the Software sends the question text — and, in current configurations, the retrieved chunks needed to answer it — to the Gemini API using your key. Google processes that content under Google's terms and its data-retention policies for the Gemini API. If this is not acceptable to you, do not ask questions you consider confidential, or configure the Software to run without generation.
3. **What never leaves.** The index, the database, your Drive files, OAuth tokens, and your API key never leave your machine as part of the Software's operation. The Software contains no telemetry, no analytics, no crash reporting, and no phone-home of any kind.
4. **Retention.** Everything local persists until you delete it. Deleting the data directory (`~/.local/share/fenris` by default, overridable) removes the index and every copy of your content the Software holds. Revoking the Drive token removes the credential.
5. **The local web UI.** The Software's web interface binds to `127.0.0.1` only. It is reachable from your machine, not the internet. If you deliberately expose it, you assume the resulting risk: the interface has no authentication by design, because it was never meant to face anything but localhost.

## 5. Acceptable use

You will not use the Software to:

1. Access a Google Drive you do not own or lack rights to read.
2. Circumvent Google's API quotas, rate limits, or the Drive terms of service, including by pooling accounts or sharding storage.
3. Build a product whose primary value is re-serving others' Drive content without their knowledge;
4. Or violate any law applicable to you.

You accept that the Software is single-user by design and that you are responsible for what you do with it.

## 6. Third-party services and components

1. **Google Drive API and Gemini API.** The Software calls Google's public APIs. Your use of those APIs is governed by your agreements with Google. Google's quotas and pricing apply. Fenris does not interpose itself in that relationship.
2. **Open-source components.** The Software depends on third-party open-source packages (each carrying its own license, preserved in the distribution) and, for the web UI, on packages installed from public registries. We do not warrant those components.
3. **Model outputs.** Answers are produced by a third-party model (Google's Gemini) reading your content. Google's policies for that service apply to what the model does with it.

## 7. Contributions

1. **Your submissions.** If you contribute code, documentation, or issues to the project's repositories, you grant the project maintainers a perpetual, worldwide, non-exclusive, royalty-free license to use, modify, and distribute your contribution as part of the Software under its MIT License. You confirm your contribution is yours to give and does not include others' confidential content — **including anything from your own Drive corpus**. Do not paste documents you indexed with the Software into issues or code.
2. **No obligation.** We are not obligated to accept, review, or maintain any contribution.

## 8. Changes and termination

1. **To the Software.** The Software changes as the project develops. Sync behavior, parsing, models, and interfaces may change between versions. You control when you update.
2. **To these Terms.** We may update these Terms when the Software's behavior materially changes. The effective date above will change with it. Continued use after an update means acceptance.
3. **You can stop any time.** Delete the Software and the data directory, run `fenris drive disconnect`, and revoke the API key in your Google Cloud console. There is nothing to cancel and no account to close.

## 9. No warranty, and this matters more than usual

**THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.**

Beyond that general disclaimer, you specifically acknowledge:

1. **Answers can be confidently wrong.** Retrieval-augmented generation reduces but does not eliminate hallucination. The Software's answer states, citation design, and abstention behavior are mitigations, not guarantees. Verify anything that matters — legal figures, medical facts, financial numbers — against the cited source, which the Software shows you precisely so you can.
2. **The index can be stale.** Answers reflect the last sync. A file edited after your last sync may be quoted in its old form; the Software marks staleness where it detects it but cannot detect what it has not seen.
3. **Retrieved text is untrusted.** A document in your Drive may contain adversarial instructions. The Software sandboxes retrieved text and never follows instructions found in documents, but no defense is perfect for all content.
4. **Your machine is the security boundary.** The index contains the text of your documents. If your machine is compromised, the index is readable. Fenris does not encrypt the local database; we rely on your operating system's protections, and say so rather than pretend a toggle makes it safe.

## 10. Limitation of liability

**TO THE MAXIMUM EXTENT PERMITTED BY LAW, THE FENRIS PROJECT AND ITS CONTRIBUTORS WILL NOT BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES, INCLUDING LOST PROFITS, LOST DATA, OR BUSINESS INTERRUPTION, ARISING FROM OR RELATED TO THE SOFTWARE. BECAUSE THE SOFTWARE IS FREE OF CHARGE AND RUNS AGAINST YOUR OWN INFRASTRUCTURE AND DATA, OUR TOTAL LIABILITY TO YOU FOR ANY CLAIM IS LIMITED TO THE AMOUNT YOU PAID US FOR THE SOFTWARE, WHICH IS ZERO. THE PARTIES AGREE THIS ALLOCATION OF RISK IS REFLECTED IN THE PRICE.**

Some jurisdictions do not allow certain limitations or disclaimers. In those jurisdictions, the limitations apply to the maximum extent permitted, and nothing here removes rights that cannot be waived.

## 11. General

1. **Governing law.** These Terms are governed by the laws of **[JURISDICTION]**, without regard to conflict-of-law rules.
2. **Disputes.** The parties will first attempt to resolve any dispute informally by contacting the project maintainers at **[CONTACT EMAIL]**. If unresolved after 60 days, disputes will be brought only in the courts of **[JURISDICTION]**, except that either party may seek injunctive relief for intellectual-property violations in any competent court.
3. **Severability.** If a provision is unenforceable, it is modified to the minimum extent necessary; the rest remains in force.
4. **No waiver.** Not enforcing a provision is not waiving it.
5. **Entire agreement.** These Terms, the MIT License, and the documents in `docs/legal/` are the entire agreement between you and the Fenris project regarding the Software.
6. **Contact.** **[CONTACT EMAIL / ADDRESS]**

---

*These Terms were adapted for Fenris from attorney-drafted templates published by [General Legal](https://github.com/General-Legal/legal-templates) under CC0 1.0. They are not legal advice, and running them past an attorney before publishing is the responsible move — the bracketed fields require your input, and jurisdictions vary.*
