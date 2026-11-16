"""Every text of the installer fits its box, in every language (#190, docs/platform-notes.md,
whiptail): whiptail cuts what does not fit without a mark, also a security question's last
sentence.

CI has no whiptail, so tests/dialog_texts.py models newt's line wrapping and the lines each kind
of box shows. MEASURED pins the model to what the real whiptail 0.52.24 (Ubuntu's 2ubuntu2)
showed under LC_ALL=C.UTF-8; tests/manual/dialog_texts.py runs the real whiptail over these
cases and over every text below, and compares it with the model.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from typing import Final

import pytest

from meshcore_home_gateway.cli.common import _json_text
from meshcore_home_gateway.views import SETTING_NAMES
from tests import dialog_texts as model
from tests.dialog_texts import Box

_LINES = "\n".join(str(number) for number in range(1, 31))
_TCP = (
    "The radio's TCP interface has no authentication: anyone who reaches it can control the radio "
    "and read its private key, which decrypts every direct message of the radio, also earlier "
    "ones recorded from the air. Use it only in a network that only the gateway reaches, never the "
    "household's WiFi. Use TCP anyway (tcp_unauthenticated = true)? No: choose the radio again."
)
# the German text of 2.2.0, which a box of 12 lines cut (#190)
_TCP_DE_2_2_0 = (
    "Die TCP-Schnittstelle des Funkgeräts hat keine Authentifizierung: Wer sie erreicht, kann das "
    "Funkgerät steuern und seinen privaten Schlüssel lesen, der jede Direktnachricht des "
    "Funkgeräts entschlüsselt, auch frühere, die über Funk mitgeschnitten wurden. Nutze sie nur in "
    "einem Netzwerk, zu dem nur das Gateway Zugang hat, nie im WLAN des Haushalts. TCP trotzdem "
    "verwenden (tcp_unauthenticated = true)? Nein: Funkgerät erneut wählen."
)
_TCP_DE_LINES = (
    "Die TCP-Schnittstelle des Funkgeräts hat keine Authentifizierung:",
    "Wer sie erreicht, kann das Funkgerät steuern und seinen privaten",
    "Schlüssel lesen, der jede Direktnachricht des Funkgeräts",
    "entschlüsselt, auch frühere, die über Funk mitgeschnitten wurden.",
    "Nutze sie nur in einem Netzwerk, zu dem nur das Gateway Zugang hat,",
    "nie im WLAN des Haushalts. TCP trotzdem verwenden",
)
_ROW = ("aaaa bbbb cccc dddd eeee ffff",)
# the German question after a refused login without a user name and TLS, with what the program
# printed (2.2.1): its question first, then the warnings, the error and the hint
_LOGIN_DE = (
    "Die Anmeldung am Broker ist fehlgeschlagen. Die Broker-Einstellungen erneut eingeben? "
    "(Nein: weiter; der Dienst versucht es weiter, --reconfigure korrigiert die Einstellungen "
    "später.)\n\nWarnung: MQTT ohne Benutzernamen: sofern der Broker Clients ohne Benutzernamen "
    "nicht abweist, kann jeder Client, der ihn erreicht, die Topics des Gateways lesen und "
    "beschreiben (docs/mqtt-api.md, Securing the broker; insecure = true akzeptiert das)\n"
    "Warnung: MQTT ohne TLS: das Passwort, die Nachrichten, die Positionen und die "
    "Befehlsereignisse gehen unverschlüsselt durchs Netzwerk (insecure = true akzeptiert das)\n"
    "der Broker hat die Verbindung abgelehnt: Not authorized (kein Benutzername)\n"
    "Broker wie Mosquitto lehnen ein falsches Passwort, einen unbekannten Benutzer und fehlende "
    "Zugangsdaten gleichermaßen ab; ein eigenständiger Mosquitto kennt einen mit mosquitto_passwd "
    "hinzugefügten Benutzer erst nach 'systemctl reload mosquitto' (docs/deployment.md, "
    "troubleshooting)"
)

# What whiptail 0.52.24-2ubuntu2 showed in a terminal of 80 columns (2026-11-15): each box, its
# text and the rows of its text area, up to the first row without text.
MEASURED: Final[Sequence[tuple[Box, str, tuple[str, ...]]]] = (
    (
        Box("yesno", 12, 72),
        _TCP,
        (
            "The radio's TCP interface has no authentication: anyone who reaches",
            "it can control the radio and read its private key, which decrypts",
            "every direct message of the radio, also earlier ones recorded from",
            "the air. Use it only in a network that only the gateway reaches,",
            "never the household's WiFi. Use TCP anyway (tcp_unauthenticated =",
            "true)? No: choose the radio again.",
        ),
    ),
    (Box("yesno", 12, 72), _TCP_DE_2_2_0, _TCP_DE_LINES),
    (
        Box("yesno", 14, 72),
        _TCP_DE_2_2_0,
        (*_TCP_DE_LINES, "(tcp_unauthenticated = true)? Nein: Funkgerät erneut wählen."),
    ),
    (
        Box("yesno", 24, 78),
        _LOGIN_DE,
        (
            "Die Anmeldung am Broker ist fehlgeschlagen. Die Broker-Einstellungen",
            "erneut eingeben? (Nein: weiter; der Dienst versucht es weiter,",
            "--reconfigure korrigiert die Einstellungen später.)",
            "",
            "Warnung: MQTT ohne Benutzernamen: sofern der Broker Clients ohne",
            "Benutzernamen nicht abweist, kann jeder Client, der ihn erreicht, die",
            "Topics des Gateways lesen und beschreiben (docs/mqtt-api.md, Securing the",
            "broker; insecure = true akzeptiert das)",
            "Warnung: MQTT ohne TLS: das Passwort, die Nachrichten, die Positionen und",
            "die Befehlsereignisse gehen unverschlüsselt durchs Netzwerk (insecure =",
            "true akzeptiert das)",
            "der Broker hat die Verbindung abgelehnt: Not authorized (kein",
            "Benutzername)",
            "Broker wie Mosquitto lehnen ein falsches Passwort, einen unbekannten",
            "Benutzer und fehlende Zugangsdaten gleichermaßen ab; ein eigenständiger",
            "Mosquitto kennt einen mit mosquitto_passwd hinzugefügten Benutzer erst",
            "nach 'systemctl reload mosquitto' (docs/deployment.md, troubleshooting)",
        ),
    ),
    # the lines each kind of box shows, of a text of 30 lines
    (Box("msgbox", 12, 40), _LINES, tuple("123456")),
    (Box("msgbox", 22, 78), _LINES, tuple(str(n) for n in range(1, 17))),
    (Box("msgbox", 12, 40, full_buttons=True), _LINES, tuple("1234")),
    (Box("yesno", 12, 72), _LINES, tuple("123456")),
    (Box("inputbox", 12, 72), _LINES, tuple("123456")),
    (Box("passwordbox", 12, 72), _LINES, tuple("123456")),
    (Box("menu", 20, 78, 10), _LINES, tuple("123")),
    (Box("menu", 20, 78, 3), _LINES, tuple(str(n) for n in range(1, 11))),
    (Box("menu", 20, 78, 3, full_buttons=True), _LINES, tuple("12345678")),
    (Box("checklist", 20, 78, 10), _LINES, tuple("1234")),
    # where a line breaks
    (
        Box("msgbox", 16, 40),
        "a word longer than the box: " + "x" * 50 + " end",
        ("a word longer than the box:", "x" * 36, "x" * 14 + " end"),
    ),
    (Box("msgbox", 16, 40), "x" * 36 + " y", ("x" * 36, "y")),
    (
        Box("msgbox", 16, 40),
        "aaaaaaaaaa bbbbbbbbbb cccccccccc      dddddddddd eeeeeeeeee",
        ("aaaaaaaaaa bbbbbbbbbb cccccccccc", "dddddddddd eeeeeeeeee"),
    ),
    (
        Box("msgbox", 16, 40),
        "  indented: aaaa bbbb cccc dddd eeee ffff gggg hhhh",
        ("  indented: aaaa bbbb cccc dddd eeee", "ffff gggg hhhh"),
    ),
    (
        Box("msgbox", 16, 40),
        "aaaaaaaaaa bbbbbbbbbb cccccccccc dddd   \nnext",
        ("aaaaaaaaaa bbbbbbbbbb cccccccccc", "dddd", "next"),
    ),
    (Box("msgbox", 16, 40), "one\n\ntwo\n", ("one", "", "two")),
    (Box("msgbox", 16, 40), "first\\nsecond line", ("first", "second line")),
    # a no-break space joins, a wide letter takes two cells, a tab 8 cells or none (newt's sic)
    (
        Box("msgbox", 16, 40),
        "aaaa bbbb cccc dddd eeee ffff gggg\N{NO-BREAK SPACE}hhhh iiii jjjj",
        (*_ROW, "gggg\N{NO-BREAK SPACE}hhhh iiii jjjj"),
    ),
    (
        Box("msgbox", 16, 40),
        "wide: " + "漢字かな" * 12,
        ("wide:", "漢字かな" * 4 + "漢字", "かな" + "漢字かな" * 4, "漢字かな" * 3),
    ),
    (
        Box("msgbox", 16, 40),
        "a\tb\tc\tthen some words to make this line long enough to wrap",
        ("a        bcthen some words to make", "this line long enough to wrap"),
    ),
    (
        Box("msgbox", 16, 40),
        "Schlüssel Funkgerät Überprüfung äöüÄÖÜß " * 3,
        (
            "Schlüssel Funkgerät Überprüfung",
            "äöüÄÖÜß Schlüssel Funkgerät",
            "Überprüfung äöüÄÖÜß Schlüssel",
            "Funkgerät Überprüfung äöüÄÖÜß",
        ),
    ),
)


@pytest.mark.parametrize(("box", "text", "shown"), MEASURED)
def test_the_model_shows_what_whiptail_showed(box: Box, text: str, shown: tuple[str, ...]) -> None:
    """The model breaks every measured text where the real whiptail did, and its box shows as
    many lines."""
    lines = [line.rstrip() for line in box.layout(text)]
    assert tuple(lines[: box.lines]) == shown


def test_the_lines_of_each_kind_of_box() -> None:
    """The rule of docs/platform-notes.md: a message, yes/no, input or password box shows its
    height less 6 lines, a menu less 7 and its list, a checklist less 6 and its list; --fb takes
    2 more. The text is 4 columns narrower than the box."""
    assert [Box(kind, 12, 72).lines for kind in ("msgbox", "yesno", "inputbox")] == [6, 6, 6]
    assert Box("passwordbox", 22, 78).lines == 16
    assert Box("menu", 20, 78, 10).lines == 3
    assert Box("menu", 24, 78, 3).lines == 14
    assert Box("checklist", 20, 78, 10).lines == 4
    assert Box("menu", 20, 78, 3, full_buttons=True).lines == 8
    assert Box("yesno", 12, 72).text_width == 68
    with pytest.raises(ValueError, match="no such kind"):
        Box("gauge", 12, 72)


def test_the_german_question_of_2_2_0_needs_seven_lines() -> None:
    """#190: the question and what No does were cut off."""
    box = Box("yesno", 12, 72)
    assert len(box.layout(_TCP_DE_2_2_0)) == 7 > box.lines


@pytest.fixture(scope="module")
def dialogs() -> list[model.Dialog]:
    return model.dialogs()


def test_every_dialog_of_the_installer_is_read(dialogs: list[model.Dialog]) -> None:
    """Each whiptail call, each call of the installer's dialog functions and each error with a box
    is a dialog with its box; a dialog the model cannot read fails here (DialogError), and so
    does a new kind of call. Every text of the installer is in a dialog, but NO_DIALOG_TEXT."""
    script = model.Script(model.SCRIPT.read_text(encoding="utf-8"))
    calls = [
        command
        for command in script.commands
        if (head := command.head()) is not None
        and command.words[head].text in {"whiptail", "die", *model.DIALOG_FUNCTIONS}
    ]
    plumbing = [  # the dialog functions' own calls: their texts come from their callers
        c
        for c in calls
        if c.function in {"die", *model.DIALOG_FUNCTIONS} and c.words[c.head()].text != "die"
    ]
    cancelled = [
        c
        for c in calls
        if c.words[c.head()].text == "die" and c.words[c.head() + 1].literal.startswith("cancelled")
    ]
    assert len(dialogs) == len(calls) - len(plumbing) - len(cancelled)
    assert len(dialogs) > 100
    shown = {msgid for dialog in dialogs for msgid in dialog.msgids()}
    template = model.tool.installer_template(model.tool.Project(model.ROOT))
    texts = {entry.msgid for entry in template if entry.msgctxt is None}
    assert texts - shown == NO_DIALOG_TEXT
    assert shown <= texts  # the model names no text the installer no longer has


# the installer's texts that are in no dialog: what die shows no box for, and the fallback of the
# final message that no run reaches (model.SUMMARY_RESULTS)
NO_DIALOG_TEXT: Final = {"cancelled", "login not checked"}


def test_the_boxes_fit_the_smallest_terminal_the_installer_takes(
    dialogs: list[model.Dialog],
) -> None:
    """80 columns and 24 rows: the final message's box is 24 rows high."""
    assert max(dialog.box.height for dialog in dialogs) <= 24
    assert max(dialog.box.width for dialog in dialogs) <= 80


def test_the_worst_cases_are_used(dialogs: list[model.Dialog]) -> None:
    """Each worst case stands for a value of the script, each of its forms too; none is left over
    from an older one. So are the values of a dialog's own fields and the rows of the menus."""
    texts = [text for d in dialogs for text in d.text.render(model.translator({}))]
    worst = {**model.FIELD_VALUES, **model.EMBEDDED}
    unused = {
        (key, value)
        for key, case in worst.items()
        for value in case.values()
        if not any(value in text for text in texts)
    }
    assert not unused
    msgids = {msgid for dialog in dialogs for msgid in dialog.text.msgids()}
    assert {msgid for msgid, _ in model.DIALOG_VALUES} <= msgids
    rows = {row for dialog in dialogs for row in dialog.rows}
    assert {row for rows_ in model.LIST_ROWS.values() for row in rows_} <= rows


def test_a_failed_login_shows_what_the_program_printed() -> None:
    """check_broker's {reason} is the program's standard error in the dialogs' language: the start
    warnings, the error with who logged in, the hint after a refusal (#190)."""
    german = model.translator(LANGUAGES["de"], "de")
    reasons = model.DIALOG_VALUES[model.LOGIN_FAILED, "reason"].render(german)
    (anonymous,) = [r for r in reasons if "Not authorized (kein Benutzername)" in r and "TLS" in r]
    assert anonymous.split("\n")[0].startswith("Warnung: MQTT ohne Benutzernamen: ")
    assert anonymous.split("\n")[1].startswith("Warnung: MQTT ohne TLS: ")
    assert anonymous.split("\n")[3].startswith("Broker wie Mosquitto ")
    assert model._fill(german(model.LOGIN_FAILED), {"reason": anonymous}) == _LOGIN_DE  # measured
    english = model.DIALOG_VALUES[model.LOGIN_FAILED, "reason"].render(model.translator({}))
    assert "broker refused the connection: Not authorized (no user name)" in english[0]


def test_the_plan_box_holds_its_worst_case(dialogs: list[model.Dialog]) -> None:
    """The plan's box counts the contacts and lists none (#190), and shows the radio's name
    without its values (test_the_plan_box_shows_no_value_of_the_radios_name), so its text has a
    bound that nothing measures at run time: every setting the managed profile manages, each at
    the values and in the form whose line takes the most lines, and each count of contacts with
    three digits. The box has 24 rows, the most a terminal of 80x24 shows, and no --scrolltext,
    with which Enter would not close it and whiptail marks no text below: the worst case fits in
    every language (test_every_text_fits_its_box). The model has every text of plan_text, and
    only those."""
    (dialog,) = [d for d in dialogs if model.PLAN_LISTED in d.msgids()]
    assert dialog.box == model.PLAN_BOX
    assert dialog.function == "choose_profile"
    assert not [d.where() for d in dialogs if d.box.scroll]
    script = model.Script(model.SCRIPT.read_text(encoding="utf-8"))
    assert set(dialog.msgids()) == script.messages_in("plan_text")
    for language, catalog in LANGUAGES.items():
        translate = model.translator(catalog, language)
        changes = model.worst_changes(translate)
        assert [change.setting for change in changes] == list(SETTING_NAMES.values())
        texts = dialog.text.render(translate)
        assert len(texts) == 3  # sets; only reports with untested firmware; observes
        for text in texts:
            assert text.count(str(model.PLAN_COUNT)) == 2, text  # each with its count
            assert text.endswith(f"\n{translate(model.PLAN_LISTED)}\n")  # as the box ends
            assert len(model.PLAN_BOX.layout(text)) <= model.PLAN_BOX.lines, (language, text)


LANGUAGES: Final = model.catalogs()


def test_the_plan_box_shows_no_value_of_the_radios_name() -> None:
    """The radio holds any name of up to 31 bytes, and probe prints one in up to 188 columns
    (\\\\ for a backslash, \\" for a quote, a \\u escape for each control or format
    character, two for one outside the basic plane) or with the two characters \\n, which
    whiptail turns into a line break, also after another backslash. So the plan's box names the
    name's setting in each of its forms without its values, which the installer's output above
    it shows (#190): its worst case holds whatever name the radio or the configuration has."""
    names = [_json_text(name) for name in model.PLAN_HIDDEN_NAMES]  # as probe prints them
    assert {len(name.encode()) for name in model.PLAN_HIDDEN_NAMES} == {31}  # the most it stores
    assert max(map(model.text_width, names)) == 188
    assert [name for name in names if "\\n" in name]
    pairs = list(itertools.permutations([*names, '"Gateway"'], 2))
    for language, catalog in LANGUAGES.items():
        translate = model.translator(catalog, language)
        for action in ("set", "reported", *model.PLAN_LEFT_FORMS):
            changes = [model.Change("name", live, wanted, action) for live, wanted in pairs]
            (line,) = {model.plan_line(translate, change) for change in changes}
            assert "\\" not in line, (language, action)
            assert len(model.PLAN_BOX.layout(f"  {line}")) == 1, (language, line)


@pytest.mark.parametrize("language", list(LANGUAGES))
def test_every_text_fits_its_box(dialogs: list[model.Dialog], language: str) -> None:
    """Every text of every dialog, with the longest of its values, needs no more lines than its
    box shows (#190). A language added later is checked as soon as its catalog is there."""
    found = model.overflows(dialogs, {language: LANGUAGES[language]})
    assert not found, "\n".join(str(overflow) for overflow in found)


@pytest.mark.parametrize("language", list(LANGUAGES))
def test_every_menu_item_shows_whole(dialogs: list[model.Dialog], language: str) -> None:
    """Every row of every menu shows whole in a terminal of 80 columns: whiptail shortens a tag
    and an item without a mark, also the security item of the radio menu (#190)."""
    found = model.cut_items(dialogs, {language: LANGUAGES[language]})
    assert not found, "\n".join(found)


def test_the_german_menu_items_start_with_a_capital_letter(dialogs: list[model.Dialog]) -> None:
    """German capitalises the first word of a menu's item, as its other items do: the setup
    menu showed "Standard (empfohlen): ..." above "erweitert (alle Einstellungen): ..."."""
    german = model.translator(LANGUAGES["de"], "de")
    items = [item for dialog in dialogs for _, item in dialog.rows if list(item.msgids())]
    assert len(items) >= 3  # the setups and the radio over TCP
    for item in items:
        for text in item.render(german):
            assert text[:1].isupper(), text


_TCP_ITEM = "radio with WiFi firmware (TCP, no authentication)"
_TCP_ITEM_DE = "Funkgerät mit WLAN-Firmware (TCP, keine Authentifizierung)"
_BY_ID = "/dev/serial/by-id/usb-Example_USB_Single_Serial_0123456789-if00"
# What whiptail 0.52.24-2ubuntu2 cut of a menu's rows in a terminal of 80 x 24 (2026-11-15).
MEASURED_ROWS: Final[Sequence[tuple[Box, tuple[tuple[str, str], ...], tuple[str, ...]]]] = (
    # with tags, both columns get 37 of the 74: the device's tag and name, and the security item
    (
        Box("menu", 20, 78, 10),
        ((_BY_ID, _BY_ID.rsplit("/", 1)[1]), ("tcp", _TCP_ITEM)),
        (_BY_ID, _BY_ID.rsplit("/", 1)[1], _TCP_ITEM),
    ),
    # without tags each item is written whole: 75 columns fit; with 76 the list is wider than
    # the window, and every row runs over the box's border
    (Box("menu", 20, 78, 10, tags=False), (("d", "x" * 75), ("tcp", _TCP_ITEM_DE)), ()),
    (
        Box("menu", 20, 78, 10, tags=False),
        (("d", "x" * 76), ("tcp", _TCP_ITEM)),
        ("x" * 76, _TCP_ITEM),
    ),
    # a list longer than its height scrolls, and its bar takes 3 columns: 72 fit, 73 do not
    (
        Box("menu", 20, 78, 10, tags=False),
        (*((f"d{n}", f"{n}" + "x" * 71) for n in range(10)), ("tcp", _TCP_ITEM)),
        (),
    ),
    (
        Box("menu", 20, 78, 10, tags=False),
        (*((f"d{n}", f"{n}" + "x" * 72) for n in range(10)), ("tcp", _TCP_ITEM)),
        (*(f"{n}" + "x" * 72 for n in range(10)), _TCP_ITEM),
    ),
)


@pytest.mark.parametrize(("box", "rows", "cut"), MEASURED_ROWS)
def test_the_model_cuts_the_rows_whiptail_cut(
    box: Box, rows: tuple[tuple[str, str], ...], cut: tuple[str, ...]
) -> None:
    assert tuple(box.cut_items(rows)) == cut


def test_a_text_too_long_for_its_box_is_found(dialogs: list[model.Dialog]) -> None:
    """The check finds what does not fit: written twice, the TCP question, the final message and
    the errors overflow their boxes; in a box with --scrolltext, which scrolls, nothing does."""
    doubled = {msgid: f"{msgid} {msgid}" for dialog in dialogs for msgid in dialog.text.msgids()}
    found = {overflow.dialog for overflow in model.overflows(dialogs, {"xx": doubled})}
    assert {dialog.function for dialog in found} >= {"accept_tcp", "summary"}
    assert any(next(dialog.text.msgids()) == "Error: {message}" for dialog in found)
    scrolling = [
        model.Dialog(d.line, d.function, Box("msgbox", 12, 72, scroll=True), d.text)
        for d in dialogs
    ]
    assert not model.overflows(scrolling, {"xx": doubled})
