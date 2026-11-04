---
status: proposed
date: YYYY-MM-DD
decision-makers: maintainers
---

# Short title of the solved problem and the chosen solution

## Context and Problem Statement

What is the problem, in two or three sentences, and why does it need a decision now?

## Decision Drivers

* driver 1
* driver 2

## Considered Options

* option 1
* option 2

## Decision Outcome

Chosen option: "option 1", because it is the only one that meets driver 1 and ...

### Consequences

* Good, because ...
* Bad, because ...

### Trust boundaries

Which boundaries of the [threat model](../threat-model.md) the decision touches (B1 the air, B2
the radio's link, B3 the broker, B4 Home Assistant, B5 its users, B6 the container, B7 the
project), what it trusts at each and what it does not; "none" with the reason if it touches none.
A decision that moves a boundary or adds an entry point updates the threat model in the same pull
request.

### Confirmation

How we check that the decision is implemented and still holds (test, review, metric).

## Pros and Cons of the Options

### option 1

* Good, because ...
* Bad, because ...

### option 2

* Good, because ...
* Bad, because ...

## More Information

Sources, related decisions, and the condition under which this decision should be revisited.
