# Task
Fix failing test

# Workspace
cwd: /repo/project
# Delegation
This task was delegated by codex via AgentDispatch (hop 0 of 2).
You may split this task into at most 2 independent subtasks and delegate each with the AgentDispatch `dispatch` tool; the router picks the best agent for each. Do not delegate the whole task as-is. Subtasks share this working tree: give each a disjoint `files` list.
# Result format
End your final message with a fenced block tagged `agent-dispatch-result` containing a JSON object with fields: status (completed|partial|failed|needs_context|needs_escalation), summary, changed_files, tests {command, result}, confidence (0..1), needs_escalation. Example:
```agent-dispatch-result
{"status": "completed", "summary": "...", "changed_files": [], "tests": {"command": "pytest", "result": "passed"}, "confidence": 0.9, "needs_escalation": false}
```
