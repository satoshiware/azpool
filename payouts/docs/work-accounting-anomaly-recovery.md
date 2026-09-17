# Work accounting anomalies

Credit allocation refuses source deltas with positive accepted-share counts and zero work, non-finite or negative work totals, and work outside the existing NUMERIC(38,18) ledger capacity. Raw snapshots and deltas remain available for diagnosis. The collector decodes JSON fractional counters directly as Decimal and subtracts counters with sufficient decimal precision; this does not restore precision already lost inside a vendor floating-point counter.

## Evidence required before correction

1. Identify the exact deployed pool/translator binary and dependency revision. Correlate pool, client, channel, connection lifetime, job, and sequence numbers.
2. Preserve the raw monitoring snapshots, interval rows, pool submissions and acceptances, assigned target history, job activation messages, and relevant block headers. Hash the evidence files.
3. Reconstruct share headers from submitted version, nonce, ntime, extranonce, coinbase, merkle branch, and previous block hash. Verify them against available pool log hashes and chain headers. Use the target pinned to the job, not a later channel target printed by monitoring.
4. Check every later interval through channel closure. A large binary floating-point total can lose each ordinary increment, yielding positive accepted shares with zero work. Do not assume subtracting one bad increment repairs subsequent intervals.
5. Reconcile previously sent executions against transaction outputs and receiving-node accounting before proposing catch-up amounts. Do not resend an already paid execution.

## Block-found target mismatch

The incident fixture contains a valid network block whose hash did not meet its much harder assigned share target. The vendor's network-block acceptance branch nevertheless credited the assigned difficulty. The assigned difficulty was approximately 7.517875309884746e22; the network difficulty was approximately 11,428,310.021619824. The acknowledgment saturated to the maximum uint64, while the pool cumulative f64 counter stopped reflecting later shares.

`work_evidence.verify_block_work` verifies the header and reports both targets. It never writes credits or silently selects a replacement. An explicitly approved effective-acceptance-target policy can use `difficulty(max(assigned_target, network_target))`, with a documented precision and rounding rule. This leaves ordinary easier-target shares unchanged. Choosing network difficulty, achieved hash difficulty, the previous share's work, or zero without a declared rule and sufficient evidence is not an auditable repair.

## Applying an approved correction

Pause payout automation and ensure no payout process is active. Back up the database using the existing database service account. Preview the exact original rows and corrected values first. Use one transaction, acquire the collector advisory lock, recheck row identities and values, reject overlap with already credited coverage, and preserve every original row in an append-only correction audit. Repeated execution must verify the existing correction rather than apply it again. Preserve raw snapshots; do not widen ledger columns or delete shares.

After correction, verify collector freshness, allocation previews, reward coverage, prior paid executions, and receiving-node allocations. Resume live payout execution only under separate authorization. A vendor counter that is still poisoned must remain under evidence-based review; decimal parsing alone cannot make it reliable.
