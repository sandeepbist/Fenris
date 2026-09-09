name: Feature request
description: A missing capability you actually need
labels: ["enhancement"]
body:
  - type: markdown
    attributes:
      value: |
        Fenris is deliberately narrow: citation-enforced QA over your own
        Drive, local-first, honest about what it can't do. Check the
        [roadmap](../docs/roadmap.md) and the "Deliberately not on the
        roadmap" section before proposing — some absences are decisions.

  - type: textarea
    id: problem
    attributes:
      label: What can't you do today?
      description: The job to be done, not the implementation. "I want to ask X and get Y."
    validations:
      required: true

  - type: textarea
    id: proposal
    attributes:
      label: How would it work?
      description: Sketch welcome. Note whether it touches the six correctness invariants in CONTRIBUTING.md — anything that trades a citation for a nicer answer will be declined.

  - type: checkboxes
    id: checks
    attributes:
      label: Fit check
      options:
        - label: It does not require uploading documents to any third party
          required: true
        - label: It does not weaken the answer-state ladder (INSUFFICIENT/STALE/CONFLICTING stay honest)
          required: true
        - label: It is not already listed under "Deliberately not on the roadmap" in docs/roadmap.md
          required: true
