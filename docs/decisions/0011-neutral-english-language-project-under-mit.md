---
status: accepted
date: 2026-10-18
decision-makers: maintainers
---

# A neutral, English-language project under the MIT license

## Context and Problem Statement

The bridge starts as a tool for one installation but should be shareable with others and may be
published. What does that require from content, language and license from the first commit on?

## Decision Drivers

* Publishing later must not require rewriting the history.
* The MeshCore community is international.
* Low barrier for reuse.

## Considered Options

* Neutral content, English, MIT license
* Content in the maintainers' language, decide on publication later
* A copyleft license (GPL-3.0)

## Decision Outcome

Chosen option: "neutral content, English, MIT", because it keeps the repository publishable at any
time, matches the language of the upstream project and its tools, and the MIT license matches the
MeshCore firmware and imposes the fewest obligations on users.

### Consequences

* Good, because the repository can be shared or published as it is.
* Neutral, because site-specific configuration lives outside the repository, in each
  installation.
* Bad, because contributors must keep personal and site data out of code, tests, examples and
  commit messages; `CONTRIBUTING.md` states the rule.

### Confirmation

Reviews check new content for site data. Examples use the documentation address ranges
(RFC 5737, `192.0.2.0/24`) and the `example.org` domain (RFC 2606).

## More Information

* MIT license text: <https://spdx.org/licenses/MIT.html>
* MeshCore firmware license (MIT): <https://github.com/meshcore-dev/MeshCore/blob/main/license.txt>
