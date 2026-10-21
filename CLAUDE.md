# JARVIS

## Source of Truth

* `SPEC.md` defines what JARVIS must do.
* `IMPLEMENTATION.md` defines how and in what order JARVIS must be built.
* `PROGRESS.md` records what has actually been implemented.
* `SKILL.md` defines the progress-maintenance workflow.

If implementation and specification conflict, stop and ask the user. Do not silently change locked decisions from `SPEC.md`.

## Before Starting Work

1. Read `PROGRESS.md`.
2. Read the relevant section of `IMPLEMENTATION.md`.
3. Read the relevant sections of `SPEC.md`.
4. Inspect only the files needed for the current module.
5. Do not scan the entire repository unless the progress report is missing, stale, contradictory, or the task genuinely requires it.

## Implementation Rules

* Follow the module order in `IMPLEMENTATION.md`.
* Build one module at a time.
* Do not begin the next module until the current module's Definition of Done is satisfied.
* Write tests required by the implementation guide.
* Never delete, weaken, skip, or loosen tests simply to make CI pass.
* Bug fixes require regression tests.
* Keep dependencies pinned.
* Ask before introducing major infrastructure, frameworks, databases, or services not specified by the project.
* Never commit secrets or `.env` files.
* Follow the Git and branch conventions defined in `IMPLEMENTATION.md`.
* Do not change locked architectural decisions without asking the user.

## Progress

After meaningful implementation work:

1. Run the relevant tests.
2. Update `PROGRESS.md`.
3. Update project hygiene/base files when the implementation state requires it.
4. Record the next concrete task.
5. Record blockers or open questions.

Use `SKILL.md` for the progress-update procedure.

## Project Hygiene

When implementation changes require it, keep these synchronized with the actual project:

* `README.md`
* `.gitignore`
* `.env.example`
* `pyproject.toml`
* `requirements.txt`, if present
* `Makefile`
* Docker/Compose files
* CI configuration
* documentation
* package configuration

Do not modify these files merely to create noise. Update them when the implementation makes them inaccurate or incomplete.

## Checkpoints

At the end of a phase, follow the checkpoint protocol in `IMPLEMENTATION.md`.

Do not automatically begin the next phase after a checkpoint.

Stop after the checkpoint and wait for user approval.

## Current Project State

Always use `PROGRESS.md` to determine the current module and resume point.

## Commands

### Resume
When the user says "Resume from PROGRESS.md":
1. Read PROGRESS.md.
2. Identify the current phase, module, and resume point.
3. Read the relevant section of IMPLEMENTATION.md.
4. Read only the relevant sections of SPEC.md.
5. Continue from the recorded resume point.

### Update Checkpoint
When the user says "Update checkpoint":
1. Read SKILL.md.
2. Follow its checkpoint/progress-update procedure.
3. Run the required tests before recording completion.
4. Update PROGRESS.md.
5. Update affected project/base files when necessary.
6. Do not start a new module unless instructed.

### Phase Checkpoint
When the user says "Run the checkpoint protocol":
1. Read the checkpoint protocol in IMPLEMENTATION.md.
2. Execute it exactly.
3. Update PROGRESS.md.
4. Stop and wait for user approval before starting the next phase.
