# Scenario: two weeks on the RG-2 gripper and DU-1 drive unit

This is the written scenario the Slack export is generated from. It names every
planted case, the day it happens, who is in the thread, the graph change it makes,
and who should hear about it. The graph it refers to is `data/graph_seed.json`.

## Calendar

| Day | Date | Weekday |
| --- | --- | --- |
| 1 | 2026-03-02 | Mon |
| 2 | 2026-03-03 | Tue |
| 3 | 2026-03-04 | Wed |
| 4 | 2026-03-05 | Thu |
| 5 | 2026-03-06 | Fri |
| 6 | 2026-03-09 | Mon (gripper EVT gate passes) |
| 7 | 2026-03-10 | Tue |
| 8 | 2026-03-11 | Wed |
| 9 | 2026-03-12 | Thu |
| 10 | 2026-03-13 | Fri |

## Cast

| ID | Name | Role | Works on |
| --- | --- | --- | --- |
| maya | Maya Chen | Mechanical engineer | Gripper |
| diego | Diego Alvarez | Mechanical engineer | Drive unit |
| priya | Priya Raman | Electrical engineer | Gripper |
| sam | Sam Okafor | Electrical engineer | Drive unit |
| lena | Lena Fischer | Firmware engineer | Both |
| tom | Tom Becker | Supply chain lead | Both |
| rachel | Rachel Kim | Engineering manager | Both |
| omar | Omar Haddad | Product manager | Gripper |

## Channels

`#general`, `#gripper`, `#drive-unit`, `#hw-electrical`, `#supply-chain`.

## Story

The gripper team is finishing EVT. The jaw finger material is still undecided on
Monday, the motor driver board has a short, and the gate review is booked for
day 6. It passes. In week two the gripper team moves into DVT. Then a customer
asks for more grip force and a tighter ingress rating, and that reopens the
mass budget and the design freeze date.

The drive unit team is in DVT throughout. On day 3 a customer raises the
continuous torque requirement from 15 to 22 Nm. The motor gets resized, the
thermal analysis and the power stage are redone, and motor and gearbox sourcing
slip.

Lena, Tom and Rachel work across both products, so changes that cross product
lines reach them most often.

## How "affected" is defined

Each case's affected list is what the core `fan_out` rules produce on the graph
as it stands at that point in the scenario, minus the thread participants:

- For a task: the task owner (after the change is applied), the owner of the
  next stage in the same process, and the owners of tasks one handoff downstream.
- For a requirement: the requirement owner and the owners of the requirements it
  affects (`requirement_links`, followed from `requirement_id` to
  `affected_requirement_id` only).

People who arguably should hear about a change but whom the core rules miss,
such as the upstream owner or the previous owner after a reassignment, are
left out on purpose. They show where the core falls short and are not counted
against it.

The proposed delta types are `status_change`, `deadline_change`,
`owner_change` and `value_change`. Contradiction cases are marked
`contradicts: true` in the gold labels.

## Planted cases

Case ID prefixes: **X** cross-team change (8), **R** requirement change with
linked requirements (3), **I** implicit decision (5), **K** contradiction with
recorded state (4), **A** same part under a different name (4), **N** change
that reaches the next stage owner (3), **L** loud signal with no state change (6).

### Day 1, Mon 2026-03-02

- **L1** `#general` (rachel, then everyone). Rachel posts "PIZZA IN THE KITCHEN
  IN 5 MIN!!!" in capitals. Fourteen replies and a lot of emoji. No change.

### Day 2, Tue 2026-03-03

- **X1** `#gripper` (maya, rachel). Maya says the finger CAD needs one more day
  to rework the root fillet for the new material. T01 deadline 2026-03-03 to
  2026-03-04. **Affected: tom, priya.** Tom's machining order (T03) is one
  handoff downstream, and he is not in the thread.
- **I1** `#gripper` (maya, tom, rachel). Tom says 7075 bar stock is eight weeks
  out and 6061 is on the shelf. Maya: "FEA margin is fine either way." Rachel:
  "ok, go with the 6061." REQ-G03 value `Al 7075-T6` to `Al 6061-T6`.
  **Affected: omar** (owner of the gripper mass requirement, REQ-G02, which
  REQ-G03 links to).
- **K2** `#gripper` (lena, maya). Lena: "the control loop's been done since last
  week, I'm on close-time characterization now." The graph has T06 in progress.
  T06 status `in_progress` to `done`, contradicts recorded state.
  **Affected: rachel** (next stage owner).
- **L2** `#gripper` (maya, diego, priya). A long, heated thread about CAD file
  naming conventions. Twenty replies, ends with "let's talk Friday." No change.

### Day 3, Wed 2026-03-04

- **R1** `#drive-unit` (rachel, diego). Rachel relays a customer request that
  continuous torque must be 22 Nm. Diego confirms. REQ-D01 value `15 Nm` to
  `22 Nm`. It links to drive mass (REQ-D02, Diego's) and winding temperature
  (REQ-D03, Sam's). **Affected: sam.**
- **A1** `#hw-electrical` (sam, priya). Sam: "MDB rev A is shorting at the gate
  driver, Priya's board is dead until rev B." "MDB" means the gripper motor
  driver board. T04 status `in_progress` to `blocked`. **Affected: lena** (next
  stage owner).
- **X8** `#gripper` (maya, omar). Maya says FEA has to be rerun for 6061 and
  will land Thursday. T02 deadline 2026-03-04 to 2026-03-05.
  **Affected: priya.** Her EVT sample validation (T08) is one handoff
  downstream, and she is on the electrical team.

### Day 4, Thu 2026-03-05

- **X2** `#drive-unit` (diego, lena). Diego: "motor sizing is done, it's the
  90 mm frame." T17 status `in_progress` to `done`. **Affected: tom, sam.**
  Motor sourcing (T21) and the power stage (T20) are downstream, and neither
  owner is in the thread.
- **X3** `#hw-electrical` (priya, sam). Priya: with the board dead, current-limit
  tuning moves to Monday. T05 deadline 2026-03-05 to 2026-03-09.
  **Affected: lena** (grip control loop, T06, is downstream).
- **X4** `#hw-electrical` (sam, priya). Sam: the 22 Nm motor needs the 48 V
  MOSFETs, so the power stage layout is redone and slips to the 11th. T20
  deadline 2026-03-06 to 2026-03-11. **Affected: tom, lena.** Lena's torque
  loop tuning (T24) is downstream.
- **N1** `#drive-unit` (sam, diego). Sam says the thermal analysis has to be
  redone at 22 Nm and will slip to Tuesday. T18 deadline 2026-03-06 to
  2026-03-10. **Affected: tom.** He owns sourcing, the next drive DVT stage,
  which is his only path to this change.
- **L3** `#hw-electrical` (sam, priya, lena). "URGENT: who has the 4-channel
  scope??" Answered in-thread three messages later. No change.

### Day 5, Fri 2026-03-06

- **I2** `#hw-electrical` (priya, sam). They go back and forth on thermal
  headroom for the gripper actuator. Priya: "fine, 2.8 it is." REQ-G04 value
  `<= 3.0 A` to `<= 2.8 A`. It links to close time (REQ-G05, Lena's).
  **Affected: lena.**
- **K1** `#supply-chain` (tom, sam). Tom: "drive motors land on the 17th, as
  planned." The graph says 2026-03-10. T21 deadline 2026-03-10 to 2026-03-17,
  contradicts recorded state. **Affected: lena** (next stage owner).
- **A3** `#gripper` (maya, priya). Maya: "fingertips are back from the shop, all
  ten in tolerance." "Fingertips" means the EVT jaw fingers. T03 status `open`
  to `done`. **Affected: tom** (task owner, not in the thread).

### Day 6, Mon 2026-03-09: gripper EVT gate

- **A2** `#drive-unit` (diego, sam). Diego: "can't place the HD-20 order until
  finance signs off on the new price." "HD-20" means the DVT gearbox. T22
  status `open` to `blocked`. **Affected: tom, lena.**
- **L4** `#general` (everyone). A gate celebration thread with photos, cake and
  thirty emoji replies. No change. This is a separate thread from Rachel's gate
  announcement.

### Day 7, Tue 2026-03-10

- **X5** `#gripper` (rachel, maya, diego). Rachel moves the wrist seal design to
  Diego because Maya is now on DVT freeze. T15 owner `maya` to `diego`.
  **Affected: tom, omar.** Omar's packaging (T13) is downstream of the seal.
- **R2** `#gripper` (omar, maya). EVT customer feedback: grip force must be
  55 N. Maya agrees. REQ-G01 value `40 N` to `55 N`. It links to actuator
  current (REQ-G04, Priya's) and gripper mass (REQ-G02, Omar's).
  **Affected: priya.**

### Day 8, Wed 2026-03-11

- **X6** `#gripper` (maya, omar). Maya: the freeze package needs the 55 N
  rework and moves to Friday. T10 deadline 2026-03-11 to 2026-03-13.
  **Affected: tom.** DVT finger stock sourcing (T11) is downstream.
- **I3** `#supply-chain` (tom, priya). Tom shares the second-source motor test
  data. Priya: "numbers look good to me, let's run with it." T12 status `open`
  to `done`. **Affected: omar** (next stage owner).
- **K3** `#drive-unit` (diego, lena). Diego: "mass budget is Sam's now, since
  the heatsink change." The graph has Diego as owner, and no reassignment was
  ever announced. T19 owner `diego` to `sam`, contradicts recorded state.
  **Affected: sam, tom.**
- **N2** `#hw-electrical` (priya, sam). Priya: the DVT validation plan needs
  55 N test cases and moves to next Wednesday. T14 deadline 2026-03-13 to
  2026-03-18. **Affected: rachel**, as owner of reliability testing, the next
  gripper DVT stage.
- **L5** `#supply-chain` (tom, diego, rachel). Tom rants about a vendor's rude
  email. Many replies. He ends with "dates unaffected." No change.

### Day 9, Thu 2026-03-12

- **X7** `#drive-unit` (lena, diego). Lena: the encoder vendor's SDK has a CRC
  bug, and she is stuck until they patch it. T23 status `open` to `blocked`.
  **Affected: sam.** Drive sample validation (T25) is downstream.
- **R3** `#gripper` (omar, rachel). A customer site needs washdown, so the
  ingress rating goes to IP65. REQ-G06 value `IP54` to `IP65`. It links to
  gripper mass (REQ-G02, Omar's). **Affected: maya** (requirement owner, not in
  the thread).
- **I4** `#drive-unit` (rachel, diego). Talking about the late motors, Rachel
  says "fine, push sample validation to the 20th then." T25 deadline
  2026-03-13 to 2026-03-20. **Affected: sam** (task owner).
- **K4** `#gripper` (maya, priya). Maya mentions that "the seal design is due
  the 20th." The graph says 2026-03-12. T15 deadline 2026-03-12 to 2026-03-20,
  contradicts recorded state. **Affected: diego, tom, omar.** Diego has owned
  T15 since day 7.
- **N3** `#drive-unit` (lena, rachel). Lena: torque loop tuning waits on the
  late motors and moves to next Tuesday. T24 deadline 2026-03-12 to
  2026-03-17. **Affected: sam** (owner of validating samples, the next drive DVT
  stage).

### Day 10, Fri 2026-03-13

- **A4** `#drive-unit` (sam, diego). Sam: "AksIM readings are clean now, Lena's
  workaround did it." "AksIM" means the drive encoder. T23 status `blocked` to
  `done`. **Affected: lena** (task owner).
- **I5** `#gripper` (omar, rachel). Omar posts the final packaging render.
  Rachel: "looks right to me, send it to the vendor." T13 status `open` to
  `done`. **Affected: priya** (next stage owner).
- **L6** `#hw-electrical` (priya, sam). "BLOCKER? anyone seen the spare probe
  tips" is answered with "nvm, found them." No change.

## Background changes (not planted)

These ordinary state changes keep the story consistent and bring the state
change share to about 30%. They are labelled in gold like any other change.

| Day | Channel | Participants | Change | Affected |
| --- | --- | --- | --- | --- |
| 1 | `#drive-unit` | diego | T19 status `open` to `in_progress` | tom |
| 3 | `#gripper` | maya | T01 status `in_progress` to `done` | priya, tom |
| 6 | `#gripper` | priya, maya | T08 status `open` to `done` | rachel |
| 6 | `#general` | rachel | T09 status `open` to `done` (EVT gate passed) | none |
| 7 | `#gripper` | maya | T10 status `open` to `in_progress` | tom |
| 7 | `#supply-chain` | tom | T21 status `open` to `in_progress` | lena |
| 9 | `#gripper` | rachel | T16 status `open` to `in_progress` | none |
| 10 | `#hw-electrical` | sam | T20 status `in_progress` to `done` | tom, lena |

## Totals

- Planted cases: 33 (27 state changes, 6 loud noise threads).
- Background state changes: 8.
- State changes overall: 35. Against about 120 threads, that is about 29%. The
  realized ratio will be reported once the messages exist.

## Open point

The gate pass on day 6 is recorded as T09 moving to `done`. Deltas can only
target tasks and requirements, so the `processes.status` column keeps its seeded
value (`gripper-evt` active, `gripper-dvt` planned). A3 will need to work out
the active process from gate dates and the run date, or deltas will need a
`process` target kind.
