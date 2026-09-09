name: Bug report
description: Something answered wrong, crashed, or served stale/unauthorized data
labels: ["bug"]
body:
  - type: markdown
    attributes:
      value: |
        A correct answer matters more here than a fast one. If the bug is a
        **security issue**, do not open it publicly — use the
        [private advisory page](https://github.com/sandeepbist/Fenris/security/advisories).

  - type: textarea
    id: what
    attributes:
      label: What happened?
      description: The wrong behavior, in one paragraph. Include the exact question you asked if this is an answer-quality issue.
    validations:
      required: true

  - type: textarea
    id: expect
    attributes:
      label: What did you expect instead?
    validations:
      required: true

  - type: textarea
    id: repro
    attributes:
      label: Steps to reproduce
      description: Commands or UI actions, in order. Redact personal file names.
      placeholder: |
        fenris sync run
        fenris ask "..."
    validations:
      required: true

  - type: dropdown
    id: surface
    attributes:
      label: Where did it happen?
      options:
        - CLI (`fenris ...`)
        - Web UI (`make ui`)
        - Desktop app (Tauri)
        - `fenris doctor`
        - eval / test suite
    validations:
      required: true

  - type: input
    id: version
    attributes:
      label: Version
      description: Output of `fenris version`
      placeholder: "fenris 1.0.0"
    validations:
      required: true

  - type: textarea
    id: doctor
    attributes:
      label: `fenris doctor` output
      description: Paste the checklist (redact anything personal). This kills most back-and-forth.

  - type: textarea
    id: context
    attributes:
      label: Anything else?
      description: Parsing tier (slim/full), OS, Python version, vault count.
