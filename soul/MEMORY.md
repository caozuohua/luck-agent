markdown
# MEMORY.md

## Operational Pattern: Shell Tool Execution

**Trigger Format:** `tool execution completed: shell`
**Outcome Structure:** JSON with `output` (stdout string) and `returncode` (integer)

### Observed Success Pattern
- **Return code:** 0 indicates success
- **Output encoding:** Chinese locale dates (e.g., `2026-07-19 星期日`)
- **Line endings:** CRLF (`\r\n`)

### Lessons Learned
1. Shell commands return stdout as raw string; parse carefully for locale-specific formats
2. Always check `returncode` before trusting `output` content
3. Date outputs may include day-of-week in system locale language

### Status
Single success sample logged. Pattern stable. Continue monitoring for failures or edge cases.