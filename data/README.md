# data/

Local-only storage for raw provider payloads, exports, and scratch files.

**Everything in this directory except this README is gitignored.** Raw data may be large, and some
of it is subject to provider licensing terms that do not allow redistribution (see
[ADR-0008](../docs/adr/0008-data-sources-and-licensing.md)).

Planned layout (from Milestone 1):

```
data/
└── raw/<source>/<dataset>/<YYYY>/<MM>/<retrieved_at>.json.gz
```

Raw payloads are referenced from `ops.raw_payloads` so data can be reprocessed without refetching.
