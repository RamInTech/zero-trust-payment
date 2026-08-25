# Zero-Trust Payment Authorization for AI Agents on Razorpay

**Track 01 — AI Growth & Agentic Commerce (Razorpay AI Buildathon)**

A safety layer that makes it possible for a merchant to accept AI-agent-initiated
payments at all.

---

## The problem

AI agents are starting to transact on behalf of people. A merchant asked to
accept one of those payments has no good options today:

- **Approve every transaction manually** — which defeats the entire point of
  handing the task to an agent.
- **Trust the agent** — which is unacceptable when real money moves, and
  especially unacceptable to the merchant, who eats the disputed charges.

So merchants do the rational thing: they keep agents out of checkout, or cap what
an agent may spend so low that automation isn't worth building against.

The blocker isn't that agents are useless. It's that nobody can currently
guarantee three specific things about an agent-initiated payment:

1. It cannot exceed a boundary the merchant agreed to in advance.
2. It cannot be executed twice, even if the agent retries, replays, or crashes
   mid-transaction.
3. Every decision — approved or denied — can be explained afterward.

## The idea

Don't try to make the agent trustworthy. Make the checkout infrastructure safe no
matter what the agent does.

This project sits between an AI agent and Razorpay's payment APIs as an
authorization boundary. The agent proposes; it never decides. All authorization
and money-safety logic lives outside the agent's reach, in deterministic code
that behaves identically whether the agent is well-behaved, buggy, or actively
adversarial.

That inversion is the whole thesis. Every guarantee below holds without assuming
anything about the quality of the AI making the request.

---

## How it works

Four ideas, each doing one job.

**A mandate is a spending boundary agreed in advance.** Before an agent can spend
anything, the merchant defines what it may spend it on: a maximum per
transaction, an allowlist of purchasable items, an expiry, and a velocity limit
capping how many purchases fit in a window. Every request is checked against that
mandate before any money moves. A denial always names the specific rule that was
broken — never a bare "denied," because an unexplainable denial is nearly as
useless as an unexplainable charge.

**An idempotency key makes a purchase execute exactly once.** Each purchase
intent carries a key claimed exactly once. A retry with the same key returns the
original saved result instead of charging again. A retry with the same key but a
*changed* amount is rejected outright as a conflict, rather than quietly
executing the new one. Crucially, this holds under genuinely concurrent
requests — several threads firing the identical key at the same instant still
produce exactly one charge — because the guarantee comes from a database unique
constraint, not application-level locking that races can slip past.

**The human confirms, and the policy engine checks anyway.** Natural language is
parsed into a structured request and shown to a person: *confirm — buy X for ₹Y?*
Only on explicit confirmation does it proceed. But confirmation is not approval.
The policy engine still evaluates the confirmed request independently and can
still deny it. The two gates catch different failures: confirmation catches the
model misreading *what* was meant; policy catches anything *unsafe* even when the
intent was read perfectly. A person who confirms a purchase without realizing the
mandate's cap is already spent still gets denied.

**Every decision is written to an append-only log.** Approvals, denials, replays,
conflicts, and money actions all land in a permanent record using a fixed
vocabulary of named event types. Entries are never updated or deleted. The test
of this isn't that a log exists — it's that someone holding only the log, without
reading the code, can correctly explain why any given request was approved or
denied.

---

## What it refuses to do

The invariants matter more than the features, so they're stated plainly:

- **The AI layer is untrusted input, never an authority.** It may propose a
  purchase. It cannot approve one, bypass the policy engine, or write to the
  audit log or the idempotency store. A request that talks the model into
  "just approving this" is still denied downstream, because the model's output
  was never what granted permission.
- **A stuck request never blocks forever.** If a process crashes holding a key,
  a staleness timeout lets a fresh attempt reclaim it — while a request that is
  genuinely still in flight is *not* reclaimed, so the exactly-once guarantee
  isn't quietly traded away for liveness.
- **An unknown outcome is never guessed.** When a payment's true state genuinely
  cannot be determined — a timeout with no confirmed answer either way — it is
  recorded as pending verification and reconciled later. The system never claims
  a payment succeeded when it does not actually know that yet.
- **A confirmed price is the price that executes.** The amount is re-validated
  against the catalog at confirmation time, so a price that changed (or a
  tampered value) is rejected rather than silently charged.

---

## Honest limits

- **Capture is simulated.** Order creation is a real server-to-server call
  against a live Razorpay test-mode account. Capture requires a `payment_id`
  that standard Razorpay Checkout only produces through a browser-based customer
  step, and no headless test-mode path was found. The authorization wrapper
  behaves identically either way — and the wrapper is what this project asserts
  is correct — but the capture leg is simulated, and that is stated here rather
  than buried.
- **Single instance.** Coordinating the idempotency store across multiple
  instances is out of scope.
- **Not covered:** live browser checkout, multi-currency, production-grade
  secrets management.

---

## Where this fits

Conversational checkout, agent-readable catalogs, upsell agents — the agentic
commerce products people are building all share one unstated dependency:
something has to make it safe for a merchant to accept a money action from an
agent in the first place.

This is that dependency. It isn't a checkout experience; it's the authorization
boundary a checkout experience would sit on top of, and the piece that has to be
correct before any of them can responsibly move real money.
