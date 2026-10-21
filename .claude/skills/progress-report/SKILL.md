---

name: progress-report
description: Maintain JARVIS implementation progress and project hygiene. Use when starting work, completing a meaningful task, updating progress, preparing a checkpoint, or resuming an interrupted implementation.
---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

# JARVIS Progress and Project Hygiene

## Purpose

Maintain `PROGRESS.md` as a small, accurate checkpoint for the JARVIS project.

Do not duplicate the contents of `SPEC.md` or `IMPLEMENTATION.md`.

The purpose of `PROGRESS.md` is to answer:

> Where are we, what was completed, what is being worked on, and what should happen next?

## Source of Truth

Use:

* `SPEC.md` for requirements and locked behavior.
* `IMPLEMENTATION.md` for implementation order, tests, and Definition of Done.
* The actual repository for what has really been implemented.
* `PROGRESS.md` for the current checkpoint.

If `PROGRESS.md` conflicts with the code, inspect the code and correct `PROGRESS.md`.

If the code conflicts with `SPEC.md`, stop and ask the user.

## Starting or Resuming Work

1. Read `PROGRESS.md`.
2. Identify the current phase and module.
3. Read the corresponding section of `IMPLEMENTATION.md`.
4. Read only the relevant sections of `SPEC.md`.
5. Inspect only the files relevant to that module.
6. Continue from the Resume Point.

Do not scan the entire repository unless necessary.

## After Meaningful Implementation

After completing a meaningful piece of work:

1. Run the tests required by the current module.
2. Verify the implementation against the relevant specification.
3. Update `PROGRESS.md`.
4. Update affected project/base files.
5. Check whether documentation is now stale.
6. Check whether dependency declarations are accurate.
7. Check whether `.gitignore` needs changes.
8. Check whether `README.md` needs changes.
9. Check whether `.env.example` needs new variables.
10. Check whether Docker/Compose configuration needs updating.
11. Check whether CI or Makefile configuration needs updating.

Only modify files when the implementation actually requires the change.

## Dependency Files

`pyproject.toml` is the authoritative Python dependency/configuration file when using the implementation guide's `uv` setup.

If `requirements.txt` exists or is required by the project:

* Keep it synchronized with the actual runtime dependencies.
* Do not create conflicting dependency versions.
* Do not add unused dependencies.
* Do not remove dependencies that are still required.

If the project does not use `requirements.txt`, do not create one merely for the progress report.

## README

Keep `README.md` useful for someone cloning the repository.

Update it when appropriate with:

* Current project status
* Development setup
* Required tools
* Local startup commands
* Testing commands
* Architecture information that has actually been implemented
* Configuration instructions
* Current limitations

Do not document features that do not exist yet as completed functionality.

## .gitignore

Keep `.gitignore` aligned with the actual development environment.

At minimum, ensure generated files, local environments, secrets, caches, test artifacts, IDE files, and other non-source artifacts are ignored when applicable.

Never add a blanket rule that hides important source files.

## Other Base Files

When implementation introduces or changes a project convention, update the relevant base file.

Examples:

* New environment variable → `.env.example`
* New dependency → `pyproject.toml` and `requirements.txt` if maintained
* New development command → `Makefile` and `README.md`
* New Docker service → Compose files and `README.md`
* New CI requirement → `.github/workflows/`
* New project requirement → requirements/traceability documentation when required by `IMPLEMENTATION.md`
* New architecture decision → ADR documentation when required

## PROGRESS.md Format

Keep `PROGRESS.md` short.

Maintain only:

### Status

Overall implementation state.

### Current Phase

The phase from `IMPLEMENTATION.md`.

### Current Module

The active module ID and name.

### Completed

Only completed work.

Include commit SHA when meaningful.

### In Progress

What is currently being implemented.

### Next

The next concrete implementation steps.

### Blockers / Questions

Only unresolved issues requiring attention.

### Resume Point

A precise instruction describing where implementation should continue.

### Last Updated

Date of the latest meaningful update.

Do not turn `PROGRESS.md` into an architecture document, specification, changelog, or diary.

## At Phase Checkpoints

Follow the checkpoint protocol in `IMPLEMENTATION.md`.

Before declaring a phase complete:

* Verify every module Definition of Done.
* Run the required test suites.
* Run required benchmarks.
* Update traceability.
* Create the checkpoint report.
* Update `PROGRESS.md`.
* Stop and wait for user approval.

Never start the next phase automatically after a checkpoint.

## Accuracy Rules

Never:

* Claim a feature is implemented without evidence.
* Claim tests passed without running them.
* Invent benchmark numbers.
* Invent commit SHAs.
* Mark a module complete when its Definition of Done is incomplete.
* Mark planned functionality as implemented.
* Rewrite the specification to match an implementation shortcut.

Keep the progress report factual and compact.
