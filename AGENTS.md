# AGENTS.md

## Token efficiency
- Be extremely concise.
- Minimize token usage.
- Do not explain obvious code.
- Do not repeat the task or requirements.
- Do not output full files unless explicitly requested.
- Keep final responses very short.

## Scope
- Make only the minimum changes necessary.
- Do not refactor unrelated code.
- Do not modify unrelated files.
- Do not add unrequested features.
- Do not add dependencies unless strictly necessary.
- Reuse existing code and patterns.

## Repository exploration
- Inspect only files directly relevant to the task.
- Never scan the entire repository unless absolutely necessary.
- Prefer targeted searches.
- If file paths are provided, inspect those first.
- Do not reread unchanged files unnecessarily.

## Implementation
- Prefer the simplest working solution.
- Preserve existing architecture and coding style.
- Avoid unnecessary abstractions.
- Avoid unnecessary comments or documentation.
- Do not create extra files unless needed.

## Testing
- Do not create tests.
- Do not search for tests.
- Do not run tests unless explicitly requested.

## Communication
- Do not provide progress commentary.
- Do not describe commands executed.
- Ask questions only when essential.
- At completion, state only:
  - What changed.
  - Files modified.