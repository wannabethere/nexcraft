# Example Slice: Employee → TrainingAssignment → LateCompletion → OverdueRisk

This file walks through one end-to-end slice of the causal ontology, with every
card the slice depends on shown in full. The slice covers:

- An employee assigned a training course.
- The assignment progressing (or not) toward its due date.
- The deterministic detection of late completion when the assignment slips.
- The causal chain from low progress to overdue risk to compliance gap.

Twenty-two cards, organized by layer. Card IDs are stable identifiers; the
prose under each is what would be embedded in the vector store.

---

## Semantic Layer

### Object types

```
---
id: employee
layer: semantic
kind: object_type
version: 3
extends: [trainable, auditable]
markings: [contains_pii]
refs: [department, role, manager, training_assignment]
---
An Employee is a person who works at the organization. Each employee is
identified by an employee_id, which is PII and propagates that marking to any
derived field.

Employees belong to exactly one department, report to one manager (who is
themselves an employee), and hold one role. Their employment_status is one of
active, on leave, or terminated. Only active employees can be assigned new
training — this is enforced as a precondition on the AssignTraining action.

Employees are sourced from the CSOD employee table. The employee_id field maps
to EmployeeID; department_id and manager_id resolve to other Employee and
Department cards via foreign keys.

Employees implement the Trainable interface (because they can receive training
assignments) and the Auditable interface (every action on them is logged). They
participate in causal reasoning through OverdueRisk, ComplianceGap, and
PhishingRisk causal nodes whenever those nodes need a per-employee scope.
```

```
---
id: training_assignment
layer: semantic
kind: object_type
version: 4
refs: [employee, course, learning_activity, late_completion, overdue_assignment]
---
A TrainingAssignment is the link between an employee and a course at a specific
point in time, with a due date and a tracked completion state. It is the unit
that learning, compliance, and risk reasoning all pivot around.

Each assignment carries an assigned_date (when it was created), a due_date
(when it must be completed by), and a completed_date (null until completion).
Its status is one of pending, in_progress, completed, or overdue — derived
from the dates and from progress signals on the linked LearningActivity rows.

Assignments link upward to the Employee they are assigned to and the Course
they target, and downward to LearningActivity rows that record progress events.
Two derived states attach to assignments under the right conditions:
LateCompletion (when completed_date > due_date) and OverdueAssignment (when
the due_date has passed and completed_date is still null).

TrainingAssignments are the primary input to the OverdueRisk and ComplianceGap
causal nodes. Their state changes trigger recompute of the parent Employee's
compliance state and the Department's compliance roll-up.
```

```
---
id: course
layer: semantic
kind: object_type
version: 2
refs: [curriculum, course_category]
---
A Course is a unit of training content — a self-contained learning object that
can be assigned to employees. Each course belongs to a curriculum, has a
course_category that places it in a domain (Cybersecurity, Compliance,
Leadership, Technical Skills, Onboarding), and carries an is_mandatory flag.

Mandatory cybersecurity courses are the highest-priority objects in the
compliance graph. When a course has category = Cybersecurity and is_mandatory
= true, the system attaches a CybersecurityTrainingCriticality concept to
every TrainingAssignment that targets it, which feeds into the
PrivilegedRoleIncreasesTrainingCriticality causal edge.

Course domain classification is performed by the classify_course_domain
function, which uses an LLM because course names and descriptions vary too
much across customers for deterministic rules to work cleanly.
```

### Link types

```
---
id: assigned_to_employee
layer: semantic
kind: link_type
version: 1
derivation: structural
cardinality: many_to_one
confidence: 0.99
refs: [training_assignment, employee]
---
Every TrainingAssignment is assigned to exactly one Employee. The link is
structural: training_assignment.employee_id joins to employee.employee_id, both
sourced from CSOD.

The cardinality is many-to-one — an employee can have many assignments, but
each assignment belongs to one employee. Confidence is 0.99 because the
relationship derives from a foreign key constraint, not inference. The 0.01
slack accounts for soft-deleted employees whose assignments may briefly point
to nothing during retention windows.

This link is traversed by every causal query that needs employee-scoped
reasoning over assignment state, including OverdueRisk computation and
ComplianceGap roll-ups.
```

```
---
id: for_course
layer: semantic
kind: link_type
version: 1
derivation: structural
cardinality: many_to_one
confidence: 0.99
refs: [training_assignment, course]
---
Every TrainingAssignment targets exactly one Course. The link is structural:
training_assignment.course_id joins to course.course_id.

Many-to-one cardinality — a course can be assigned to many employees, but a
given assignment row references only one course. The link is the bridge that
lets risk reasoning ask domain-specific questions: when OverdueRisk fires for
a Cybersecurity course versus a Leadership course, the downstream
compliance and risk implications differ, and that branching depends on this
link.
```

### Property types

```
---
id: progress_percent
layer: semantic
kind: property_type
version: 1
base_type: float
range: [0.0, 1.0]
units: ratio
semantics: completion_ratio
refs: [learning_activity, training_assignment]
---
The progress_percent property records how far an employee has progressed
through a training assignment, expressed as a ratio from 0.0 (not started) to
1.0 (fully complete).

Values are sourced from LearningActivity rows, which emit progress updates as
the employee moves through the course. The most recent LearningActivity for an
assignment defines its current progress; we do not aggregate across activities
of different types because completion semantics vary.

This property feeds directly into the LowLearningProgress causal node — when
progress < 0.3 and fewer than 7 days remain until due_date, the
low_progress_rule fires and the LowLearningProgress state attaches to the
assignment. From there it activates the
low_progress_increases_overdue_risk causal edge.
```

### Interface

```
---
id: trainable
layer: semantic
kind: interface
version: 1
required_properties: [employee_id, role_id]
required_links: [assigned_to_training_assignment]
---
The Trainable interface is implemented by any object type that can receive
training assignments. Currently only Employee implements it, but the interface
exists because future object types — contractors, partners, system service
accounts subject to operational training — will share the same compliance
reasoning surface.

Implementing Trainable means the object can be the target of an
AssignTraining action, can carry a compliance_state derived field, and can
participate as a scope for OverdueRisk, ComplianceGap, and PhishingRisk
causal nodes.
```

### Concepts and causal nodes

```
---
id: late_completion
layer: semantic
kind: concept
version: 2
parent_concepts: [compliance_event]
embedding_ref: qdrant://concepts_l1/late_completion
refs: [training_assignment, compliance_gap]
---
LateCompletion is the derived state attached to a TrainingAssignment when its
completed_date is strictly later than its due_date. It is a deterministic
event — the late_completion_rule produces it whenever both dates are present
and the inequality holds.

LateCompletion is more than a status flag. It is a compliance event with
downstream consequences: it increases the ComplianceGap for the employee's
department, factors into the manager's training-effectiveness signal, and is
counted in quarterly compliance attestations. The
late_completion_increases_compliance_gap causal edge formalizes the first of
these.

Late completion also feeds back into the Weight Learner as ground-truth for
the OverdueRisk model. Each LateCompletion is an observation that confirms a
prior overdue prediction was correct, tightening the CI on the
low_progress_increases_overdue_risk edge over time.
```

```
---
id: low_learning_progress
layer: semantic
kind: causal_node
version: 1
variable_type: observed_binary
intervenable: false
observable_via: [low_progress_rule]
refs: [training_assignment, progress_percent]
---
LowLearningProgress is the causal node that captures whether an employee is
behind on a training assignment in a way that meaningfully predicts overdue
risk. It is a binary observed variable: true when the low_progress_rule fires
on the assignment, false otherwise.

The node is observable but not intervenable. Managers cannot directly
"intervene" on an employee's progress; they can only intervene through
Kinetic Layer actions like SendReminder or RequestFollowup, which then
indirectly shift progress. This distinction matters for causal queries:
LowLearningProgress is a valid conditioning variable but not a valid target
of a do-operation.

The node has one outbound causal edge — low_progress_increases_overdue_risk —
and conditions on Role and ManagerEngagement as confounders.
```

```
---
id: overdue_risk
layer: semantic
kind: causal_node
version: 2
variable_type: latent_continuous
intervenable: false
prior: { dist: Beta, a: 2, b: 8 }
refs: [training_assignment, low_learning_progress, compliance_gap]
---
OverdueRisk is the latent continuous variable representing the probability
that a given training assignment will become overdue. It is a per-assignment
random variable, with a Beta(2, 8) prior chosen to reflect the empirical base
rate of overdue assignments across CSOD tenants (roughly 20%) before any
predictors are observed.

OverdueRisk is latent — we never observe it directly. We observe the realized
outcome (Overdue or NotOverdue) once the due date passes, and we observe
predictors (LowLearningProgress, Role, ManagerEngagement, course difficulty)
along the way.

The node has two parents (LowLearningProgress, ManagerEngagement) and one
realized child (OverdueAssignment). Its weights are learned from outcome data
and live in the causal_edge cards that point into it.
```

```
---
id: compliance_gap
layer: semantic
kind: causal_node
version: 1
variable_type: latent_continuous
intervenable: false
scope: department
refs: [late_completion, overdue_assignment, department]
---
ComplianceGap is the department-scoped latent variable that aggregates
compliance debt — the cumulative impact of LateCompletion and
OverdueAssignment events across the department's employees over a rolling
window. It is the variable enterprise compliance officers most often want to
intervene on.

The gap is not directly intervenable, but a wide range of upstream actions
indirectly reduce it: ManagerFollowup, ReminderSent, AssignmentRescheduled,
TrainingPolicyTightened. Each of these is the source of a causal edge into
ComplianceGap, and the Shapley attribution over those edges tells a compliance
officer which lever has the largest expected effect for their context.
```

### Causal edges

```
---
id: low_progress_increases_overdue_risk
layer: semantic
kind: causal_edge
version: 4
effect: increases
functional_form: noisy_or
weight: { value: 0.62, ci: [0.48, 0.74], n: 14820, source: learned }
identifiability: backdoor_admissible
confounders: [role, manager_engagement]
refs: [low_learning_progress, overdue_risk]
---
Low learning progress increases the risk of an assignment becoming overdue.
When progress drops below 30% with fewer than seven days until the due date,
the likelihood of the assignment slipping past its deadline rises sharply.

Effect strength is 0.62 with a 95% confidence interval of 0.48 to 0.74,
learned from 14,820 observations across four quarters of CSOD data. The
relationship is identifiable via backdoor adjustment, with Role and
ManagerEngagement as the confounders that need conditioning. Functional form
is noisy-OR — this edge combines with other parents of OverdueRisk
multiplicatively in log-odds space.

Earlier hypothesized weight (0.55) was promoted to learned in version 3 once
n exceeded 5,000 and the CI tightened below 0.3. Version 4 (current) reflects
a refit on Q1 2026 data with no significant directional change.
```

```
---
id: late_completion_increases_compliance_gap
layer: semantic
kind: causal_edge
version: 2
effect: increases
functional_form: linear_additive
weight: { value: 0.18, ci: [0.14, 0.23], n: 6240, source: learned }
identifiability: trivially_identifiable
confounders: []
refs: [late_completion, compliance_gap]
---
Each LateCompletion event contributes a small additive increase to the
ComplianceGap of the employee's department. The contribution is per-event,
linear, and decays over a 90-day rolling window.

Effect size is 0.18 standardized units per event with a tight CI (0.14 to
0.23), learned from 6,240 LateCompletion observations across 47 departments.
The edge is trivially identifiable — there are no confounders because
LateCompletion is an event we observe directly, and ComplianceGap is its
aggregator. The causal direction is fixed by the temporal ordering: the gap
exists *because* the late completions occurred.

This edge is one of several into ComplianceGap. Shapley attribution over the
full set tells compliance officers which event types (late completions vs
overdue assignments vs expired certifications) are driving the largest share
of their gap in any given quarter.
```

---

## Kinetic Layer

### Action types

```
---
id: assign_training
layer: kinetic
kind: action_type
version: 2
inputs: [employee, course, due_date]
output: training_assignment
audit: required
refs: [employee, course, training_assignment]
---
The AssignTraining action creates a new training assignment, linking an
employee to a course with a specified due date.

The action can only be invoked when the target employee's employment_status is
active and the actor holds the training.assign permission for that employee's
marking class. Both preconditions are checked before any write — a
non-active employee or insufficient permission causes the action to reject
with a structured error rather than partially executing.

On success, the action writes a new TrainingAssignment row with status
"pending", emits an assignment_created event for downstream listeners, and
invalidates the cached compliance_state for both the employee and their
department. Every invocation is audited — the actor, full input set, and
resulting assignment ID are written to the audit log before the response
returns.

The action is idempotent on (employee, course, assigned_date) within a
24-hour window — a duplicate invocation in that window returns the existing
assignment rather than creating a second one.
```

### Derivation rules

```
---
id: late_completion_rule
layer: kinetic
kind: derivation_rule
version: 1
produces: late_completion_state
trigger: training_assignment.completed_date set
refs: [training_assignment, late_completion]
---
A training assignment has caused a late completion when its completed_date is
strictly later than its due_date. This is a deterministic state derivation —
whenever both dates are present and the inequality holds, the LateCompletion
state attaches to the assignment, and a "creates" link is emitted from the
assignment to the LateCompletion concept node.

The rule fires every time a TrainingAssignment row's completed_date is
written or updated. If the resulting comparison is false, no state attaches.
If true, the LateCompletion record is created with a pointer back to the
assignment and a timestamp equal to the completed_date.

LateCompletion records are inputs to the
late_completion_increases_compliance_gap causal edge and to compliance gap
roll-ups at the department level. They are also fed back as labeled
observations to the Weight Learner, which uses them to refine the CI on
low_progress_increases_overdue_risk.
```

```
---
id: low_progress_rule
layer: kinetic
kind: derivation_rule
version: 2
produces: low_learning_progress_state
trigger: learning_activity update OR daily_at_midnight
refs: [training_assignment, learning_activity, progress_percent, low_learning_progress]
---
A training assignment exhibits low learning progress when its current
progress_percent is below 0.30 and fewer than seven days remain until its
due_date. The rule is evaluated whenever a LearningActivity update arrives
for the assignment, and also nightly to catch the case where time passes
without any new activity.

Both conditions must hold. An assignment at 0.20 progress with fourteen days
remaining is not flagged — there is still ample time. An assignment at 0.50
progress with three days remaining is also not flagged — progress is high
enough that on-time completion is plausible. Only the intersection — low
progress AND short remaining time — fires the rule.

When the rule fires, the LowLearningProgress state attaches to the
assignment, which in turn activates the
low_progress_increases_overdue_risk causal edge. The state can also clear
later: if a subsequent LearningActivity raises progress above 0.30, the state
detaches and the edge stops contributing to OverdueRisk for that assignment.
```

### Validation rule

```
---
id: completion_after_assignment
layer: kinetic
kind: validation_rule
version: 1
target: training_assignment
on_violation: reject
refs: [training_assignment]
---
A TrainingAssignment's completed_date, when set, must be greater than or
equal to its assigned_date. A completed_date earlier than the assigned_date
is impossible — the assignment did not exist yet — and indicates either a
data entry error or a clock skew between source systems.

The rule applies on every write to completed_date. Violations cause the write
to reject with an error code that points to this rule's ID, so the source
system can correct or escalate. There is no soft-violation mode for this
rule; the inequality is foundational to every downstream temporal derivation.

A separate companion rule (not shown here) requires due_date >=
assigned_date with the same enforcement.
```

### Causal rule

```
---
id: late_progress_to_overdue_activation
layer: kinetic
kind: causal_rule
version: 1
edge_ref: low_progress_increases_overdue_risk
contribution_fn: shapley_attribution
log_to: cce_evidence_store
refs: [low_progress_rule, low_learning_progress, overdue_risk]
---
This rule controls when the low_progress_increases_overdue_risk causal edge
participates in OverdueRisk computation, and how its contribution is
attributed.

Activation: the edge participates whenever the LowLearningProgress state is
attached to the assignment in question. The activation condition is the
output of the low_progress_rule, so the two are tightly coupled — when the
derivation rule attaches the state, this causal rule lights up the edge.

Contribution: when multiple parents of OverdueRisk are simultaneously
active, each parent's contribution to the resulting probability is computed
via Shapley attribution over the noisy-OR functional form. The attribution is
logged to the CCE evidence store so that downstream queries can ask "what
share of this assignment's overdue risk is attributable to low progress
versus manager disengagement?".
```

---

## Dynamic Layer

### Marking

```
---
id: pii_marking
layer: dynamic
kind: marking
version: 1
classification: restricted
propagation: transitive
refs: [employee]
---
The PII marking applies to any field that contains personally identifiable
information about an employee — names, employee_ids, email addresses, manager
relationships when they identify a specific person, and any link or derived
field that resolves back to one of these.

Propagation is transitive: any field derived from a PII-marked field
inherits the marking automatically. A compliance roll-up at the department
level that counts overdue employees does not inherit PII because the count
is aggregate; a list of the specific overdue employees does inherit it
because individuals are identifiable.

Roles without explicit PII clearance see PII-marked fields as redacted in
query results, with a marker indicating that fields exist they cannot read.
This applies uniformly to the natural-language and structured KnowQL
surfaces — the Dynamic Layer filter runs on all results before they return
to the caller.
```

### Role and permission

```
---
id: compliance_analyst
layer: dynamic
kind: role
version: 1
grants:
  - read.training_assignment
  - read.late_completion
  - read.compliance_gap
  - run.causal_query.compliance_scope
refs: [permission, training_assignment, compliance_gap]
---
The ComplianceAnalyst role is granted to users who need read access to
training compliance state and the ability to run causal queries scoped to
compliance variables, but who do not need PII clearance or the ability to
write to training assignments.

The role intentionally does not include training.assign or training.modify
permissions. Compliance analysts can see that an assignment was completed
late, can ask "what reduced the compliance gap last quarter", and can
attribute the gap to specific causal edges — but they cannot create or
modify the underlying assignments. That separation of read-and-analyze from
write is by design.

Without the PII marking clearance, queries that would return individual
employee identities are filtered to aggregates. A query like "which employees
are currently overdue" returns counts by department; "what is the compliance
gap by department" returns full results.
```

```
---
id: read_employee_compliance
layer: dynamic
kind: permission
version: 1
object_type: employee
action: read
filter: { fields: [compliance_state, department, role] }
refs: [employee, compliance_analyst]
---
The read.employee.compliance permission grants read access to an employee's
compliance_state, department, and role — but not to identifying fields like
name, employee_id, or email. It is the narrowest permission that enables
compliance reasoning without exposing individual identity.

The permission is scoped by field rather than by row. A holder of this
permission can read compliance_state on every employee in the tenant, but
each result is automatically projected to only the three readable fields.
Attempts to retrieve other fields return a structured error indicating the
fields are not in the permission's filter.

This permission is bundled into the ComplianceAnalyst role and is also
available standalone for service accounts that compute department roll-ups.
```

### Lineage

```
---
id: lineage_employee_compliance_state_emp_47
layer: dynamic
kind: lineage_edge
version: 1
derived: employee.compliance_state(emp_47)
inputs:
  - training_assignment.status(ta_991)
  - training_assignment.due_date(ta_991)
  - training_assignment.completed_date(ta_991)
rule_ref: late_completion_rule@v1
timestamp: 2026-04-15T22:11:03Z
---
The compliance_state value for employee emp_47 was derived at 22:11 UTC on
2026-04-15 from three inputs on training assignment ta_991 — its status,
due_date, and completed_date. The derivation was performed by version 1 of
the late_completion_rule, which detected that completed_date (2026-04-14)
was later than due_date (2026-04-10) and attached a LateCompletion state to
the assignment.

The LateCompletion state then triggered a recompute of the parent employee's
compliance_state via the effect declared on the
late_completion_increases_compliance_gap causal edge. The new
compliance_state value reflects the addition of one LateCompletion event to
the rolling 90-day window for emp_47.

This lineage edge is the answer to "why did this employee's compliance state
change last Wednesday" — a single retrieval points at the rule, the inputs,
and the timestamp without any further traversal needed.
```

---

## Walking the Slice with a Query

A compliance analyst asks:

> *"Which of our cybersecurity training assignments are at high risk of going
> overdue this week, and what's driving the risk?"*

Here is how the cards above are traversed.

**Step 1 — Retrieval.** The KnowQL planner embeds the question and pulls the
top relevant cards from the vector store. Likely hits:
`overdue_risk`, `low_learning_progress`, `low_progress_increases_overdue_risk`,
`training_assignment`, `course`, `late_progress_to_overdue_activation`.

**Step 2 — Plan.** The planner sees that the question requires (a) filtering
TrainingAssignments by linked Course category = Cybersecurity, (b) computing
OverdueRisk for each, and (c) attributing the risk to causal edges. This is
not answerable from card prose alone — it needs structured execution. The
planner compiles to:

```
MATCH (ta:TrainingAssignment)-[:for_course]->(c:Course {category: "Cybersecurity"})
WHERE ta.status IN ["pending", "in_progress"] AND ta.due_date <= today + 7
WITH ta
COMPUTE overdue_risk(ta)
ATTRIBUTE overdue_risk BY causal_edge
ORDER BY overdue_risk DESC
LIMIT 50
```

**Step 3 — Execute.** The pattern half hits the warehouse via the
`assigned_to_employee` and `for_course` link cards. The compute half hits the
causal engine, which loads the `low_progress_increases_overdue_risk` weight,
checks identifiability against the card's metadata (backdoor admissible with
Role and ManagerEngagement), and returns a ranked list with Shapley
attributions per assignment.

**Step 4 — Filter.** Results pass through the Dynamic Layer. The
ComplianceAnalyst role does not hold PII clearance, so individual employee
identities are projected out. The result becomes a list of (assignment_id,
department, course, overdue_risk, top_attributed_edge) tuples.

**Step 5 — Synthesize.** The planner returns prose with citations to the
relevant cards:

> 47 cybersecurity assignments are at elevated overdue risk this week. The
> dominant driver across them is **low learning progress** (cite:
> low_progress_increases_overdue_risk) — 31 of the 47 have progress below
> 30% with under seven days remaining. Effect strength on this edge is
> 0.62 with a 95% CI of 0.48 to 0.74, identifiable via backdoor adjustment
> on Role and ManagerEngagement. Department-level attribution and the full
> ranked list follow…

**Step 6 — Trail.** Every claim in the response is traceable to a card. The
attribution is traceable to a causal_edge card. The causal_edge is traceable
to its weight history. The weight history is traceable to the lineage edges
of the observations that produced it. The chain is unbroken from prose
response back to source rows.

That is the slice. Twenty-two cards, four layers, one walkable trail from
question to answer to evidence.
