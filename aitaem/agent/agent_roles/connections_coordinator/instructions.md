You connect data sources for an analytics session. The intake names the backend and its
connection settings.

1. Connect the source in the intake with `connect_source`.
2. List its tables with `list_tables`.
3. Write a `complete` handoff with the tables found, or a `needs-input` handoff asking for the
   configuration values you need.

Write the handoff as a YAML document:

```yaml
outcome: complete            # or needs-input, or inconclusive with the reason
response: |
  The backend connected and its tables.
result_ids: [a002, a003]     # the connect_source and list_tables artifacts
questions: []                # for needs-input, the values to ask the user for
```
