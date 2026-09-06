markdown
# MEMORY.md

## Tool Error Patterns (Root Causes)

- **shell exit 2**: Missing required command argument. Always provide non-empty command string; shell rejects empty input.
- **web_search failure**: `SERPER_API_KEY` environment variable not configured. Web search is currently non-functional until key is set.

## Reliable Tool Behaviors

- **date command**: Returns `2026-07-19 星期日` consistently. System clock indicates Sunday, July 19, 2026.
- **ls -la**: Lists project root with consistent file set: `.gitignore.un~`, `.codegraph/`, `.coveragerc`, `.dockerignore`, `.editorconfig`. Ownership: `Cao Zuohua (Be) 197609`.

## Operational Lessons

1. **Shell commands**: Avoid empty/truncated input—agent sometimes emits partial command strings.
2. **Web search**: Pre-flight check for `SERPER_API_KEY` or expect failure.
3. **Timezone/Date**: System is running in Chinese locale (`星期日`). Date appears fixed at 2026-07-19 across multiple runs—likely container/image timestamp.

## Environment Context

- Project appears to be a Python/Django-style repo (`.coveragerc`, `.editorconfig`, `.codegraph/` present).
- Files dated June–July 2026. User "Cao Zuohua (Be)" with uid 197609.
- Duplicate outcomes seen: identical `date` and `ls` outputs in multiple runs—indicates idempotent read-only queries or repeated tool calls.

## Deduplication Notes

- Date output repeated 4 times → summarized as single reliable value.
- Directory listings repeated 3 times with minor size variations (1045, 1445, 1189) → structural pattern is stable, size fluctuations are transient.
```

*(Character count: ~1,100 / 3,000 limit)*