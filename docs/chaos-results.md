<!-- GENERATED FILE — do not edit by hand.
     Regenerate with: uv run python scripts/run_chaos_harness.py -->

# Chaos harness results

**100/100 rounds stayed correct under injected failure.** Every round runs real purchases through the real gateway while failures are injected underneath them: slow and timing-out provider calls, crashes between the charge and the record, Postgres backends terminated mid-transaction, and processes SIGKILLed after the money moved. Each round then reconciles and checks that the money still adds up.

| Measure | Value |
|---|---|
| Rounds | 100 |
| Rounds that stayed correct | 100 |
| Purchases attempted | 3200 |
| Orders the provider actually charged | 2649 |
| Money charged | ₹657,768.30 |
| Revenue booked after reconciliation | ₹657,768.30 |
| Database backends killed mid-flight | 476 |
| Processes SIGKILLed | 200 |
| Divergences repaired by reconciliation | 1548 |
| Records left in flight (killed claimants) | 100 |
| Mid-run reads of the books, all netting zero | 302141 |

## Faults injected

| Fault | Times |
|---|---|
| `CRASH_AFTER_CALL` | 425 |
| `DB_BACKEND_KILL` | 502 |
| `LATENCY` | 411 |
| `OK` | 904 |
| `PROCESS_KILL_AFTER_CHARGE` | 100 |
| `PROCESS_KILL_BEFORE_CHARGE` | 100 |
| `TIMEOUT_AFTER_CALL` | 415 |
| `TIMEOUT_BEFORE_CALL` | 343 |

## What each round asserts

1. No receipt is charged twice.
2. Revenue booked equals what the provider actually charged.
3. Nothing is left in suspense.
4. The books and the purchase records do not disagree.
5. The ledger re-adds to zero, and so did every mid-run read.
6. The audit hash chain verifies.
7. A record is COMPLETED if and only if its receipt was charged once.

## Rounds

| Seed | Purchases | Charged | Repairs | In flight | Result |
|---|---|---|---|---|---|
| `437415201` | 32 | 27 | 15 | 1 | SURVIVED |
| `785793268` | 32 | 25 | 15 | 1 | SURVIVED |
| `356272083` | 32 | 27 | 14 | 1 | SURVIVED |
| `964129673` | 32 | 27 | 14 | 1 | SURVIVED |
| `484200546` | 32 | 25 | 16 | 1 | SURVIVED |
| `1060631618` | 32 | 26 | 17 | 1 | SURVIVED |
| `214195885` | 32 | 24 | 17 | 1 | SURVIVED |
| `1042830222` | 32 | 25 | 15 | 1 | SURVIVED |
| `1023431345` | 32 | 24 | 19 | 1 | SURVIVED |
| `791310477` | 32 | 25 | 16 | 1 | SURVIVED |
| `274971949` | 32 | 26 | 19 | 1 | SURVIVED |
| `602580064` | 32 | 26 | 12 | 1 | SURVIVED |
| `870784059` | 32 | 23 | 18 | 1 | SURVIVED |
| `396991265` | 32 | 28 | 17 | 1 | SURVIVED |
| `756668243` | 32 | 25 | 18 | 1 | SURVIVED |
| `802395012` | 32 | 28 | 16 | 1 | SURVIVED |
| `637065981` | 32 | 24 | 14 | 1 | SURVIVED |
| `85526816` | 32 | 25 | 18 | 1 | SURVIVED |
| `266061344` | 32 | 25 | 15 | 1 | SURVIVED |
| `230717369` | 32 | 30 | 11 | 1 | SURVIVED |
| `949171429` | 32 | 26 | 19 | 1 | SURVIVED |
| `632374974` | 32 | 28 | 16 | 1 | SURVIVED |
| `316359394` | 32 | 28 | 14 | 1 | SURVIVED |
| `813717716` | 32 | 27 | 19 | 1 | SURVIVED |
| `983256458` | 32 | 28 | 21 | 1 | SURVIVED |
| `244011606` | 32 | 27 | 13 | 1 | SURVIVED |
| `994453124` | 32 | 26 | 17 | 1 | SURVIVED |
| `70850508` | 32 | 30 | 16 | 1 | SURVIVED |
| `954134678` | 32 | 26 | 16 | 1 | SURVIVED |
| `596375178` | 32 | 26 | 13 | 1 | SURVIVED |
| `339915003` | 32 | 29 | 17 | 1 | SURVIVED |
| `1063121041` | 32 | 26 | 15 | 1 | SURVIVED |
| `626621957` | 32 | 23 | 18 | 1 | SURVIVED |
| `115045467` | 32 | 26 | 16 | 1 | SURVIVED |
| `448982211` | 32 | 28 | 17 | 1 | SURVIVED |
| `873872711` | 32 | 28 | 14 | 1 | SURVIVED |
| `305661567` | 32 | 28 | 13 | 1 | SURVIVED |
| `247450713` | 32 | 27 | 15 | 1 | SURVIVED |
| `141721063` | 32 | 25 | 14 | 1 | SURVIVED |
| `633849988` | 32 | 26 | 14 | 1 | SURVIVED |
| `90350328` | 32 | 29 | 12 | 1 | SURVIVED |
| `535301394` | 32 | 27 | 18 | 1 | SURVIVED |
| `470466443` | 32 | 23 | 17 | 1 | SURVIVED |
| `153007939` | 32 | 26 | 14 | 1 | SURVIVED |
| `934267336` | 32 | 30 | 17 | 1 | SURVIVED |
| `12128143` | 32 | 26 | 14 | 1 | SURVIVED |
| `909785441` | 32 | 29 | 12 | 1 | SURVIVED |
| `183927371` | 32 | 30 | 10 | 1 | SURVIVED |
| `921999686` | 32 | 23 | 20 | 1 | SURVIVED |
| `870822229` | 32 | 26 | 17 | 1 | SURVIVED |
| `687773488` | 32 | 28 | 12 | 1 | SURVIVED |
| `867750181` | 32 | 29 | 16 | 1 | SURVIVED |
| `90846977` | 32 | 26 | 14 | 1 | SURVIVED |
| `132155279` | 32 | 25 | 14 | 1 | SURVIVED |
| `683355836` | 32 | 26 | 17 | 1 | SURVIVED |
| `71988290` | 32 | 27 | 11 | 1 | SURVIVED |
| `433202926` | 32 | 29 | 16 | 1 | SURVIVED |
| `150631093` | 32 | 29 | 14 | 1 | SURVIVED |
| `83255965` | 32 | 25 | 17 | 1 | SURVIVED |
| `866812877` | 32 | 22 | 21 | 1 | SURVIVED |
| `573241743` | 32 | 27 | 13 | 1 | SURVIVED |
| `633563575` | 32 | 25 | 13 | 1 | SURVIVED |
| `1013529090` | 32 | 29 | 9 | 1 | SURVIVED |
| `907793184` | 32 | 28 | 14 | 1 | SURVIVED |
| `675050134` | 32 | 28 | 16 | 1 | SURVIVED |
| `730464745` | 32 | 30 | 15 | 1 | SURVIVED |
| `1001328277` | 32 | 28 | 15 | 1 | SURVIVED |
| `224971774` | 32 | 23 | 17 | 1 | SURVIVED |
| `877133210` | 32 | 27 | 15 | 1 | SURVIVED |
| `443280749` | 32 | 29 | 14 | 1 | SURVIVED |
| `316660048` | 32 | 25 | 15 | 1 | SURVIVED |
| `952509465` | 32 | 23 | 17 | 1 | SURVIVED |
| `914141987` | 32 | 19 | 18 | 1 | SURVIVED |
| `294725780` | 32 | 27 | 19 | 1 | SURVIVED |
| `532637137` | 32 | 30 | 11 | 1 | SURVIVED |
| `1011835727` | 32 | 26 | 18 | 1 | SURVIVED |
| `931200829` | 32 | 27 | 18 | 1 | SURVIVED |
| `439221557` | 32 | 29 | 11 | 1 | SURVIVED |
| `790958737` | 32 | 26 | 14 | 1 | SURVIVED |
| `121516598` | 32 | 27 | 17 | 1 | SURVIVED |
| `348098284` | 32 | 27 | 13 | 1 | SURVIVED |
| `842265349` | 32 | 29 | 12 | 1 | SURVIVED |
| `987227786` | 32 | 28 | 11 | 1 | SURVIVED |
| `178038221` | 32 | 27 | 22 | 1 | SURVIVED |
| `960327573` | 32 | 28 | 13 | 1 | SURVIVED |
| `415721337` | 32 | 24 | 20 | 1 | SURVIVED |
| `4287621` | 32 | 27 | 15 | 1 | SURVIVED |
| `222885752` | 32 | 25 | 17 | 1 | SURVIVED |
| `595334056` | 32 | 26 | 12 | 1 | SURVIVED |
| `387726522` | 32 | 26 | 17 | 1 | SURVIVED |
| `142524204` | 32 | 26 | 16 | 1 | SURVIVED |
| `42590775` | 32 | 27 | 19 | 1 | SURVIVED |
| `1050150812` | 32 | 28 | 15 | 1 | SURVIVED |
| `249559191` | 32 | 26 | 15 | 1 | SURVIVED |
| `1039113789` | 32 | 23 | 21 | 1 | SURVIVED |
| `489585088` | 32 | 27 | 13 | 1 | SURVIVED |
| `903805272` | 32 | 28 | 16 | 1 | SURVIVED |
| `504656440` | 32 | 24 | 18 | 1 | SURVIVED |
| `462560046` | 32 | 26 | 16 | 1 | SURVIVED |
| `615986966` | 32 | 26 | 12 | 1 | SURVIVED |
