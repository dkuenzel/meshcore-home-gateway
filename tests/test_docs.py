"""Documentation: every relative link points to an existing file and heading.

External links are not fetched (the tests run offline); the documents cite them as sources.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD_OUTPUT = {"build", "dist"}  # generated files, e.g. dist/release-notes.md of `make release`
DOCUMENTS = sorted(
    p
    for p in ROOT.rglob("*.md")
    if not any(part.startswith(".") or part in BUILD_OUTPUT for part in p.relative_to(ROOT).parts)
)
CODE_BLOCK = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6} (.+)$", re.MULTILINE)


def slug(heading: str) -> str:
    """GitHub's heading anchor: lower case, punctuation removed, spaces to hyphens."""
    text = heading.strip().lower().replace("`", "")
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def headings(path: Path) -> set[str]:
    text = CODE_BLOCK.sub("", path.read_text(encoding="utf-8"))
    return {slug(match) for match in HEADING.findall(text)}


def test_the_documentation_set_is_found() -> None:
    names = {p.relative_to(ROOT).as_posix() for p in DOCUMENTS}
    assert {"README.md", "docs/design.md", "docs/decisions/README.md"} <= names


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_relative_links_resolve(document: Path) -> None:
    text = CODE_BLOCK.sub("", document.read_text(encoding="utf-8"))
    broken = []
    for target in LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path, _, anchor = target.partition("#")
        destination = (document.parent / path).resolve() if path else document
        if not destination.exists():
            broken.append(f"{target}: no such file")
        elif anchor and destination.suffix == ".md" and anchor not in headings(destination):
            broken.append(f"{target}: no such heading")
    assert not broken, broken


# CODE-006: the code names the document that holds a rule, as `design §6.6` (or
# `docs/design.md §6.6`) and `ADR 0025`; the numbered decisions of a working document that is
# not in the repository leave a reader nothing to open.
CODE = sorted(
    path
    for folder in ("src", "tests", "tools")
    for path in (ROOT / folder).rglob("*.py")
    if "firmware" not in path.relative_to(ROOT).parts  # the C++ sources' citations are other
)
DESIGN_SECTION = re.compile(r"design(?:\.md)? ?§ ?(\d+(?:\.\d+)*)")
DECISION = re.compile(r"\bADR (\d{4})\b")
# a working document's own numbering: a design or slice named by a number and a letter, its
# open points, and its decisions, findings and goals (D, F or G and a number) as a reference
WORKING_DOCUMENT = re.compile(
    r"\bdesign \d+[a-z]\b|\bslice \d+[a-z]\b|\b\d+[a-z]'s open point"
    r"|(?:\(|, |; |design |# |\"\"\")[DFG]\d{1,2}\b(?![ \t][0-9A-F]{2}\b)"
)


@pytest.mark.parametrize("module", CODE, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_design_references_in_the_code_resolve(module: Path) -> None:
    text = module.read_text(encoding="utf-8")
    sections = {
        heading.split(" ", 1)[0]
        for heading in HEADING.findall((ROOT / "docs" / "design.md").read_text(encoding="utf-8"))
    }
    decisions = {path.name[:4] for path in (ROOT / "docs" / "decisions").glob("[0-9]*.md")}
    unresolved = [
        f"design §{number}" for number in DESIGN_SECTION.findall(text) if number not in sections
    ]
    unresolved += [f"ADR {number}" for number in DECISION.findall(text) if number not in decisions]
    unresolved += WORKING_DOCUMENT.findall(text)
    assert not unresolved, unresolved


def test_attestation_checks_name_the_release_workflow_and_the_tag() -> None:
    """`--repo` alone accepts an attestation from any workflow and ref of the repository."""
    commands = [
        line
        for path in DOCUMENTS
        for line in re.sub(r"\\\n\s*", " ", path.read_text(encoding="utf-8")).splitlines()
        if "gh attestation verify" in line and "--repo" in line
    ]
    assert commands
    for command in commands:
        assert "--signer-workflow" in command, command
        assert "--source-ref" in command, command


DECISIONS = ROOT / "docs" / "decisions"
INDEX_ROW = re.compile(r"^\| \[(\d{4})\]\(([^)]+)\) \| ([^|]+?) \| ([^|]+?) \|$", re.MULTILINE)


def test_the_index_of_the_decisions_lists_every_record_and_who_amends_it() -> None:
    """Each record has a row in docs/decisions/README.md with its number, its file and the title
    of its heading, and a status that says `amended by` a record only where the amended record
    links to that later one: ADR 0031 amends 0015 and 0021, and each of them ends in a line that
    links to it."""
    records = {path.name: path for path in DECISIONS.glob("[0-9]*.md")}
    rows = INDEX_ROW.findall((DECISIONS / "README.md").read_text(encoding="utf-8"))
    assert sorted(name for _, name, _, _ in rows) == sorted(records)
    by_number = {number: name for number, name, _, _ in rows}
    for number, name, title, status in rows:
        text = records[name].read_text(encoding="utf-8")
        assert name.startswith(f"{number}-"), name
        heading = re.search(r"^# (.+)$", text, re.MULTILINE)
        assert heading is not None, name
        assert heading[1] == title, name
        for amended in re.findall(r"amended by (\d{4}(?:(?:, | and )\d{4})*)", status):
            for later in re.findall(r"\d{4}", amended):
                assert f"]({by_number[later]})" in text, (name, later)
    for name in (
        "0015-provider-neutral-documents-on-the-bus.md",
        "0021-generated-dashboards-for-desktop-and-mobile.md",
    ):
        (status,) = [s for _, n, _, s in rows if n == name]
        assert "amended by 0031" in status, name


def _load_index_tool() -> ModuleType:
    path = ROOT / "tools" / "platform_notes_index.py"
    spec = importlib.util.spec_from_file_location("platform_notes_index", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["platform_notes_index"] = module
    spec.loader.exec_module(module)
    return module


def test_the_index_of_the_platform_notes_lists_every_fact() -> None:
    """Run `python3 tools/platform_notes_index.py` after adding or changing a fact."""
    tool = _load_index_tool()
    text = tool.NOTES.read_text(encoding="utf-8")
    assert tool.with_index(text) == text
    leads = [lead for _, section in tool.facts(text) for lead in section]
    assert len(leads) > 100
    assert all(f"\n* {lead}\n" in text for lead in leads)


# --- the controls the security documents promise (#105) ---------------------------------------

UNIT = ROOT / "deploy" / "systemd" / "meshcore-home-gateway.service"
INSTALLER = ROOT / "deploy" / "proxmox" / "meshcore-home-gateway-lxc.sh"
# what describes the gateway as it is: not the history (the changelog, the audit, the decision
# records, whose text stays as it was and whose amendments correct it)
CURRENT = [
    path
    for path in DOCUMENTS
    if path.name != "CHANGELOG.md"
    and not {"audit", "decisions"} & set(path.relative_to(ROOT).parts)
]


def _words(text: str) -> str:
    return " ".join(text.split())


def test_a_code_is_not_said_to_bind_the_text() -> None:
    """#105: `core.commands._check_code` compares the last word with the codes of the time steps
    and hands the rest back unchecked (`test_a_code_does_not_bind_the_text`). A code shows that
    the sender's app made it in that window: whoever reads it first can send another text with
    it, once. The guide said that a code "authenticates the text"."""
    claim = re.compile(r"codes? (?:\w+ )?authenticates? (?:the text|commands)", re.IGNORECASE)
    sources = [*CURRENT, ROOT / "src" / "meshcore_home_gateway" / "core" / "commands.py"]
    said = [p.name for p in sources if claim.search(_words(p.read_text(encoding="utf-8")))]
    assert said == []
    adr = ROOT / "docs" / "decisions" / "0009-inbound-commands-from-allowlisted-contacts.md"
    decided = _words(adr.read_text(encoding="utf-8"))
    assert "which authenticates the text" in decided  # as decided; an amendment corrects it
    assert "Amended on 2026-11-07 (#105): a code does not bind the text" in decided
    guide = _words((ROOT / "docs" / "configuration.md").read_text(encoding="utf-8"))
    codes = guide.partition("**Codes.**")[2].partition("```")[0]
    assert "does not bind the text" in codes
    assert "(design.md#74-residual-risks)" in codes


def test_a_secret_is_said_to_be_a_credential_only_with_the_drop_in() -> None:
    """#105: neither the unit nor the installer sets `LoadCredential=`, and the installer writes
    the broker password 0640 `root:meshcore-home-gateway`, which the service user reads. The
    threat model listed a credential that root alone reads as a control of T9 and B6."""
    assert "LoadCredential" not in UNIT.read_text(encoding="utf-8")
    assert "LoadCredential" not in INSTALLER.read_text(encoding="utf-8")
    assert re.search(r"^PASSWORD_FILE=", INSTALLER.read_text(encoding="utf-8"), re.MULTILINE)
    model = (ROOT / "docs" / "threat-model.md").read_text(encoding="utf-8")
    [t9] = [line for line in model.splitlines() if line.startswith("| T9 |")]
    b6 = _words(model.partition("### B6")[2].partition("\n### ")[0])
    for text in (t9, b6):
        assert "only with a drop-in" in text, text
        assert "(deployment.md#4-operations)" in text, text


def test_the_unit_is_not_said_to_wait_for_a_synchronised_clock() -> None:
    """#105: `time-sync.target` is passive. It waits for a synchronised clock only where a
    service like `systemd-time-wait-sync` or `chrony-wait` is enabled, neither is by default, and
    `systemd-time-wait-sync` skips containers (platform notes). The unit is ordered after it and
    pulls nothing in; its comment, the design and the guide said that it waits. Every paragraph,
    table row and comment that names the target names the services too."""
    unit = UNIT.read_text(encoding="utf-8")
    assert re.search(r"^After=.*\btime-sync\.target\b", unit, flags=re.MULTILINE)
    assert not re.search(r"^(?:Wants|Requires)=.*time", unit, flags=re.MULTILINE)
    comment = re.search(r"((?:^#.*\n)+)After=.*time-sync", unit, flags=re.MULTILINE)
    assert comment is not None
    blocks = [_words(comment[1])]
    for path in CURRENT:
        text = CODE_BLOCK.sub("", path.read_text(encoding="utf-8"))
        blocks += [_words(b) for b in re.split(r"\n\s*\n|\n(?=\|)", text)]
    naming = [b for b in blocks if "time-sync.target" in b]
    assert naming
    for block in naming:
        assert "systemd-time-wait-sync" in block, block
        assert "chrony-wait" in block, block


# --- the user documents describe 2.0 (#106) ---------------------------------------------------


def test_the_privacy_page_names_the_event_topics_the_gateway_has() -> None:
    """#106: the privacy page named `message` events, a topic the gateway never had; received
    messages go to `received` and `contacts/<key>/received`. Every topic it names as events is
    one the gateway publishes events on."""
    from meshcore_home_gateway.mqtt.topics import Topics

    topics = Topics("meshcore", "radio1")
    events = {
        topic.removeprefix(topics.root + "/")
        for topic in (
            topics.received,
            topics.command,
            topics.delivery,
            topics.rx,
            topics.contact_event,
            topics.node_event,
            topics.setting_event,
            topics.telemetry_event,
            topics.contact_received("<key>"),
        )
    }
    privacy = _words((ROOT / "docs" / "privacy.md").read_text(encoding="utf-8"))
    named = set(re.findall(r"`([\w/<>]+)`(?:,| and| or)? (?:\S+ )?events\b", privacy))
    listed = re.search(r"\| Events on the broker \(([^)]*)\)", privacy)
    assert listed is not None
    named |= set(re.findall(r"`([\w/<>]+)`", listed[1]))
    assert {"received", "contacts/<key>/received"} <= named
    assert named <= events, named - events


def test_the_readme_says_which_messages_are_published() -> None:
    """#106: "Receive every message as JSON": since 2.0.0 only the channels of targets are
    published by default, and a sender or a channel that floods is cut to bursts."""
    from meshcore_home_gateway.config import MessagesConfig
    from meshcore_home_gateway.inbox import RECEIVED_PER_CHANNEL_BURST, RECEIVED_PER_SENDER_BURST

    assert MessagesConfig().publish_channels == "targets"
    readme = _words((ROOT / "README.md").read_text(encoding="utf-8"))
    receive = readme.partition("- **Receive**")[2].partition(" - **")[0]
    assert "every message" not in receive
    assert "the channels that are a target" in receive
    assert f"bursts of {int(RECEIVED_PER_SENDER_BURST)} per sender" in receive
    assert f"{int(RECEIVED_PER_CHANNEL_BURST)} per channel" in receive


def test_the_roll_back_to_2_0_clears_every_retained_topic_that_2_1_added() -> None:
    """2.0 neither publishes nor clears the retained topics 2.1 added: `pending`, `config` and the
    drafts. The note on the roll back in deployment.md clears each the way the changelog since
    2.0.0 does, the topics the gateway's retained guard keeps now less those it kept in 2.0."""
    from meshcore_home_gateway.mqtt.retained import RetainedTopics
    from meshcore_home_gateway.mqtt.topics import Topics
    from tests.test_retained import Bus

    topics, bus = Topics("meshcore", "radio1"), Bus()
    guard = RetainedTopics(topics, bus.publish, bus.call_later, lambda: bus.now, drafts=True)
    kept_by_2_0 = {  # RetainedTopics in v2.0.0's mqtt/retained.py
        topics.availability,
        topics.state,
        topics.stats,
        topics.contacts,
        topics.nodes,
        topics.history,
        topics.settings,
        topics.contact("+"),
    }
    added = set(guard.filters) - kept_by_2_0
    assert added == {topics.pending, topics.config, topics.draft_subscription}
    deployment = _words((ROOT / "docs" / "deployment.md").read_text(encoding="utf-8"))
    note = deployment.partition("* **Roll back to 2.0**")[2].partition(" * **")[0]
    # the entries since 2.0.0: under Unreleased until the release, then under 2.1.0
    since = _words((ROOT / "CHANGELOG.md").read_text(encoding="utf-8").partition("\n## [2.0.0]")[0])
    for topic in sorted(added - {topics.draft_subscription}):
        command = f"mosquitto_pub -r -n -t {topic}"
        assert command in note, topic
        assert command in since, topic
    drafts = topics.draft_subscription.removesuffix("+/+")
    assert f"mosquitto_sub -t '{drafts}#' --retained-only --remove-retained" in note
    assert f"mosquitto_pub -r -n -t {drafts}" in since
    assert "meshcore-home-gateway dashboard" in note


def test_the_guide_quotes_the_home_assistant_the_dashboard_needs() -> None:
    """`meshcore-home-gateway dashboard` ends with the Home Assistant release the dashboard needs,
    newer than the gateway's own floor; the guide's Requirements and its paragraph on making the
    dashboard name the same release, and the line as the command prints it."""
    from meshcore_home_gateway.dashboard import HOME_ASSISTANT

    guide = _words((ROOT / "docs" / "home-assistant.md").read_text(encoding="utf-8"))
    requirements = guide.partition("## Requirements")[2].partition("## ")[0]
    assert f"needs Home Assistant {HOME_ASSISTANT} or newer" in requirements
    make = guide.partition("**Make it**")[2].partition("**Add it**")[0]
    assert f'"This dashboard needs Home Assistant {HOME_ASSISTANT} or later."' in make


def test_the_update_note_says_whom_telemetry_asks() -> None:
    """#106: the note on 2.0.0's privacy defaults said that telemetry asks only the companions
    the household includes; `ask = "household"` also asks the contact targets and the command
    senders (configuration.md), at once after the update."""
    configuration = _words((ROOT / "docs" / "configuration.md").read_text(encoding="utf-8"))
    ask = configuration.partition("| `ask` |")[2].partition(" | `")[0]
    assert "the contact targets and command senders" in ask
    deployment = _words((ROOT / "docs" / "deployment.md").read_text(encoding="utf-8"))
    note = deployment.partition("**The privacy defaults after an update to 2.0.0:**")[2]
    assert "and the contact targets and command senders, at once" in note.partition(" * **")[0]


def test_the_guide_names_the_sensors_and_the_entity_prefix() -> None:
    """#106: the guide said that *Telemetry* and *Telemetry state* are for companions only, while
    `[telemetry] ask_types` gives sensors both, and that contact entity IDs start with `mesh_`,
    while `[homeassistant] contact_entity_prefix` chooses the start."""
    from meshcore_home_gateway.config import HomeAssistantConfig, TelemetryConfig

    assert TelemetryConfig(ask_types=frozenset({"chat", "sensor"})).node_types != frozenset(
        TelemetryConfig().node_types
    )
    assert HomeAssistantConfig().contact_entity_prefix == "mesh"
    guide = (ROOT / "docs" / "home-assistant.md").read_text(encoding="utf-8")
    telemetry = ("| `switch.mesh_alice_telemetry`", "| `sensor.mesh_alice_telemetry_state`")
    rows = {row.split(" | ")[0]: row for row in guide.splitlines() if row.startswith(telemetry)}
    assert len(rows) == 2
    for row in rows.values():
        assert "companions only" not in row, row
        assert "ask_types`" in row, row
    ids = _words(guide).partition("* **Entity IDs**")[2].partition(" * **")[0]
    assert "contact_entity_prefix`" in ids


def test_the_release_check_r1_expects_what_the_installer_prints() -> None:
    """Since the installer prints doctor's report after `self-test passed` (the warnings a
    passed self-test has, #80), its log no longer ends with that line: R1 names the line, the
    report after it and the final message. The row names the installer by a link to the
    update in deployment.md, not by the repository's address."""
    testing = (ROOT / "docs" / "testing.md").read_text(encoding="utf-8")
    (r1,) = [line for line in testing.splitlines() if line.lstrip().startswith("| R1 |")]
    check, expected = r1.strip().strip("|").split(" | ")[1:]
    assert "ends with" not in expected
    assert "`self-test passed`" in expected
    assert "doctor" in expected
    assert "`Self-test: passed`" in expected
    assert "://" not in r1
    assert "(deployment.md#update-reconfigure-remove)" in check
    assert "--update <id>" in check
    installer = (ROOT / "deploy" / "proxmox" / "meshcore-home-gateway-lxc.sh").read_text(
        encoding="utf-8"
    )
    passed = installer.partition('log "self-test passed (')[2].partition("\n  else")[0]
    assert "printf '%s\\n' \"$report\"" in passed  # the report follows the line
    assert "Self-test:    {selftest}" in installer


# a placeholder of the dashboard's template, a JSON list's with its brackets and quotes
PLACEHOLDER = r'\[?"?@\w+@"?\]?'


def test_the_design_names_every_placeholder_of_the_dashboard_template() -> None:
    """#106: the design's bullet on the template named neither `@contact@`, `["@controls@"]`
    nor the second conditional, which `dashboard.py` filled in and kept. Since 2.1 the lists of
    chats, switches and remove buttons repeat lines (`# @each`), `["@controls@"]` is gone, and
    so is `@contact@`, which no card of 2.1 uses: the cards find the contacts by their devices.
    The bullet names what `dashboard.py` fills in now, and nothing it no longer does."""
    from meshcore_home_gateway.dashboard import LISTS

    module = (ROOT / "src" / "meshcore_home_gateway" / "dashboard.py").read_text(encoding="utf-8")
    described = module.partition('"""')[2].partition('"""')[0]
    placeholders = set(re.findall(PLACEHOLDER, described))
    flags = set(re.findall(r"# @if (\w+)", described))
    design = _words((ROOT / "docs" / "design-home-assistant.md").read_text(encoding="utf-8"))
    bullet = design.partition("* **The template** is")[2].partition(" * **")[0]
    assert {'["@chats@"]', '["@models@"]'} <= placeholders
    assert set(re.findall(rf"`({PLACEHOLDER})`", bullet)) == placeholders
    for flag in flags | {"trackers"}:
        assert f"`# @if {flag}`" in bullet, flag
    assert "`# @each <list>`" in bullet
    for name in LISTS:
        assert f"`{name}`" in bullet, name
    for gone in ('["@controls@"]', "# @if controls", "@contact@"):
        assert gone not in described
        assert gone not in bullet, gone


def test_the_design_says_which_conditionals_of_the_template_have_an_else() -> None:
    """#106 review: the design said that each conditional of the template has `# @else` and
    `# @end`, where `# @if controls` has no `# @else`, which `dashboard.py` allows. While a
    conditional of the shipped template has none, the design says that `# @else` is optional."""
    template = (ROOT / "src" / "meshcore_home_gateway" / "dashboard.yaml").read_text(
        encoding="utf-8"
    )
    blocks = re.findall(r"^# @if (\w+)\n(.*?)^# @end$", template, flags=re.MULTILINE | re.DOTALL)
    without = [flag for flag, block in blocks if "\n# @else\n" not in f"\n{block}"]
    design = _words((ROOT / "docs" / "design-home-assistant.md").read_text(encoding="utf-8"))
    bullet = design.partition("* **The template** is")[2].partition(" * **")[0]
    if without:
        assert "each with `# @else` and `# @end`" not in bullet, without
        assert re.search(r"optional `# @else`|`# @else`[^.;]* optional", bullet), without


# --- the triage log of the review (#104) ------------------------------------------------------

TRIAGE = ROOT / "docs" / "audit" / "2026-11-security-architecture-review" / "13-triage-log.md"


def test_the_triage_log_counts_its_rows_and_lists_what_waits() -> None:
    """#104: the log's state said "2026-11-04 … #37 to #64" while its rows cite pull requests up
    to #72 and decisions of 2026-11-05 and 06, and its "162 fixed" counted six rows that defer a
    part. Its state comes from its rows, and every deferred item, deferred part and unsent draft
    for MeshCore is listed to get an issue of its own."""
    text = TRIAGE.read_text(encoding="utf-8")
    kinds = ("fix", "accept", "reject", "defer", "split")
    rows = [
        row
        for row in (line.split(" | ") for line in text.splitlines() if line.startswith("| "))
        if re.fullmatch(r"\| [A-Z]+-\d+", row[0]) and len(row) > 1 and row[1] in kinds
    ]
    decisions = [row[1] for row in rows]
    before, _, after = text.partition("**State on ")
    state = _words(after.partition("\n\n")[0])
    on = state.partition(":")[0]  # not before the newest date of the rest, nor after today
    dates = re.findall(r"\b20\d\d-\d\d-\d\d\b", before + after.partition("\n\n")[2])
    assert max(dates) <= on <= datetime.now(UTC).date().isoformat(), on
    assert f"All {len(rows)} items have a decision" in state
    for word, decision in (
        ("fixed", "fix"),
        ("accepted", "accept"),
        ("deferred", "defer"),
        ("rejected", "reject"),
    ):
        assert f"{decisions.count(decision)} {word}" in state, word
    cited = sorted({int(n) for row in rows for n in re.findall(r"/pull/(\d+)\)", " ".join(row))})
    assert f"(#{cited[0]} to #{cited[-1]})" in state
    partly = {
        row[0].strip("| ")
        for row in rows
        if row[1] == "fix" and re.search(r"deferred|upstream", row[2])
    }
    assert f"{len(partly)} of them with a part deferred" in state
    waiting = text.partition("\n## Deferred, to be tracked\n")[2].partition("\n## ")[0]
    listed = set(re.findall(r"^\| ([A-Z]+-\d+)\b", waiting, flags=re.MULTILINE))
    assert {row[0].strip("| ") for row in rows if row[1] == "defer"} | partly == listed
    upstream = (ROOT / "docs" / "upstream" / "README.md").read_text(encoding="utf-8")
    for draft in re.findall(r"^\| \[[^\]]+\]\(([\w-]+\.md)\)", upstream, flags=re.MULTILINE):
        assert f"(../../upstream/{draft})" in waiting, draft
