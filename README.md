## Autonomous SaaS Bug Triager

An autonomous Level 1 support engineer. It reads an inbound ticket, searches real
service logs and a real source tree, states a root cause, drafts a candidate
patch, and either escalates it to a tracker or answers the customer and closes.

It is built around one constraint: **nothing reaches engineering, or the
customer, that the system cannot quote.**


---

## The problem

An L1 triage agent produces two outputs that cost real money: an **escalation**,
which spends engineering time, and a **resolution**, which closes a customer's
ticket. A language model produces the wrong one in exactly the same confident
register as the right one.

The two errors are not symmetric.

**Escalating a ghost** — a detailed, plausible RCA blaming a file that has
nothing to do with the symptom, costs a developer an afternoon. It is expensive,
and it is *self-correcting*: they open it, find nothing, and say so.

**Closing a real defect** — a help article, a polite reply, a shut ticket — costs
almost nothing today. It is also *terminal*. Nobody looks again. The bug persists
and the only report of it has been destroyed.

**So this system treats both of its own conclusions as untrusted, and spends its
verification budget on the irreversible one.** No escalation is allowed without
citations that resolve against real files and demonstrably connect to the
symptom. No ticket is closed as "working as intended" until a different model,
from a different lab, has tried and failed to prove a defect is there.

---

## Backend Demonstration

![Two runs: an escalation with verified evidence, and a refusal](assets/demo.svg)


## Try it out!

| [Live &rarr;](https://rhain-r.github.io/autonomous-saas-triager/assets/showcase.html) | A guided walkthrough  |
| --- | --- |
| [Console &rarr;](https://rhain-r.github.io/autonomous-saas-triager/assets/console.html) | The work behind the scene |

---

## Architecture

![Agent architecture](assets/architecture.svg)

<details>
<summary>Text version</summary>

```
[ Ticket ]
     │
     ▼
[ Classify ]  Claude → bug | user_error | how_to | feature_request
     │
     ▼
[ Gather ]  deterministic, always runs, no model in the loop
     ├─ search_error_logs(user, ±6h)        the reporter's own failures
     ├─ read_log_window(anchor)             the INFO lines the error omits
     ├─ search_error_logs(code, all users)  "is it just them?" → blast radius
     └─ search_codebase · read_file · git_log · search_docs
     │
     ▼
[ Hypothesise ]  Claude → statement + event_ids[] + file_quotes[]
     │
     ▼
[ Evidence gate ]  deterministic
     ├─ cite_log / cite_code   ─► not in the file ⇒ recorded as a fabrication
     ├─ joint 1  log → code    ─► error-signature tokens ∩ code tokens
     └─ joint 2  ticket → log  ─► reporter's user id, or shared vocabulary
     │
     ▼
[ Route ]  bug + proof ⇒ escalate · user_error + proof ⇒ resolve
           anything unproven ⇒ needs_human
     │
     ▼
[ Challenger ]  Gemini, on auto-closes only, burden inverted
     ├─ overturned (through the same gate) ⇒ escalate
     ├─ upheld                             ⇒ resolve
     └─ error / unprovable                 ⇒ needs_human
     │
     ▼
[ Patch gate ]  old_text located in the real file ⇒ diff generated here
     │
     ▼
[ Deliver ]  Jira payload · customer reply · risk score · full trace
```

</details>

---

## Challenges Solved

* **Confident misattribution.** A model that greps `auth`, lands on
  `src/auth/session.ts`, and quotes it *accurately* has produced a perfect
  citation for an innocent file.
  * *Solution:* **A two-joint evidence chain.** Real citations are not enough.
    The error signature's tokens must intersect the cited code's tokens
    (`redirect_uri` in a log matches `redirectUri` in source), *and* the cited
    events must tie back to this ticket. Both computed in Python; a model can
    assert a connection but cannot assert the overlap that proves one.
<br></br>
* **The lazy close.** The cheapest action is to match a help-centre article and
  shut the ticket — and the article is often the *specification the product is
  violating*, not the answer.
  * *Solution:* **An adversarial challenger** on a different provider, told to
    assume the first agent was lazy. It gets a wider log window, unfiltered by
    user, and reads the code path the customer described. Its own citations go
    back through the same gate.
<br></br>
* **Patches that apply to nothing.** Models hallucinate diff context and
  mis-count hunk offsets, and a plausible non-applying patch looks like work
  until an engineer tries it.
  * *Solution:* **Models never emit diffs.** They emit `old_text` / `new_text`;
    `agent/patcher.py` locates the anchor in the real file, refuses it if missing
    or ambiguous, and generates the diff itself. It applies by construction.
<br></br>
* **Self-contradictory output.** Agents cheerfully return `disposition=escalate`
  with no evidence, or `intent=bug` alongside `disposition=resolve`.
  * *Solution:* **Schema-enforced integrity.** Pydantic v2 with `extra="forbid"`
    raises on logically impossible combinations. Closing a confirmed defect is
    not a judgement call the system is allowed to make.

---

## Evaluation

| Classifier | Closure policy | P | R | F1 | FP | **Silent closures** | Right file |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Keyword-only | closes unguarded | 0.667 | 0.500 | 0.571 | 1 | **2** | 1/2 |
| Keyword-only | **+ challenger** | 0.750 | 0.750 | 0.750 | 1 | **1** | 2/3 |
| Signal-aware | closes unguarded | 0.800 | 1.000 | 0.889 | 1 | **0** | 2/4 |
| Signal-aware | **+ challenger** | 0.800 | 1.000 | 0.889 | 1 | **0** | 2/4 |


---

## Tech stack

| Component | Choice | Why |
| --- | --- | --- |
| Classify · investigate · patch | Anthropic Claude | The reasoning surface |
| Challenge | Google Gemini | Different lab, so the cross-check is real |
| Validation | Pydantic v2 | `extra="forbid"` everywhere |
| Orchestration | Purpose-built `asyncio` | See below |
| Tracker | Jira REST (`JiraSink`), file sink by default | Opt-in via `--live` |
| Tooling | `uv`, `pytest`, `ruff`, `typer`, `rich` | |
| Built with | Claude Code | See [build plan](docs/build-plan.md) |

## Documentation

| Doc | Contents |
| --- | --- |
| [architecture.md](docs/architecture.md) | Component map, the two joints, why the challenger is asymmetric, limitations |
| [agent-tools.md](docs/agent-tools.md) | Every tool and gate, with the failures that shaped them |
| [setup-guide.md](docs/setup-guide.md) | Install, configure, run, troubleshoot |
| [build-plan.md](docs/build-plan.md) | Build log, including the bugs found mid-build, and the backlog |
