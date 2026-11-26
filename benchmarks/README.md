# Offline benchmark

Run without network or paid APIs:

```bash
python benchmarks/run_offline.py
```

Measures synthetic infrastructure costs on a temporary SQLite database:

- message/memory search latency and result count;
- context-build latency and message count;
- task state-machine transition latency;
- scheduler latency for 20 no-op jobs;
- recovery latency and recovered task count;
- peak Python memory during context build.

It does not measure model quality or token savings. Live A/B scenarios are documented in
`docs/PHASE2_9.md`; they must not be run until the owner approves a budget and quota is available.
