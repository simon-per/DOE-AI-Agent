# Review Execution Script Directive

Use this directive after creating or modifying scripts in `execution/`.

## Review Scope

Check:
- correctness
- edge cases
- platform compliance
- error handling
- idempotency
- configuration via `.env`
- no secrets in code
- no destructive writes without confirmation
- no banned automation behavior
- clear logs and outputs
- testability

## Priority Levels

CRITICAL:
- could leak secrets
- could send applications without approval
- could violate anti-ban rules
- could corrupt tracker data

HIGH:
- likely incorrect scoring or filtering
- poor error handling for common cases
- duplicate detection failures
- brittle parsing that breaks normal alerts

MEDIUM:
- maintainability, naming, clarity, missing tests

LOW:
- style or small ergonomics

## Completion Rule

Implement CRITICAL and HIGH fixes before considering the script ready.

