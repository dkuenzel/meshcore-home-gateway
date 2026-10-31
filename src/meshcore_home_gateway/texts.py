"""Texts for people made where no language is known (errors, warnings): an English msgid and
its values. str() is the English text, for logs, the journal, MQTT and tests; the command line
shows the same text translated (i18n.Translator.render). The msgid is a string literal at the
call, so that `make translations` finds it (docs/development.md, Translations)."""

from __future__ import annotations

from collections.abc import Callable, Mapping


class Text:
    """An English msgid and the values of its fields."""

    __slots__ = ("msgid", "values")

    msgid: str
    values: dict[str, object]

    def __init__(self, msgid: str, /, **values: object) -> None:
        self.msgid = msgid
        self.values = values

    def __str__(self) -> str:
        return fill(self.msgid, self.values)

    def __repr__(self) -> str:
        values = "".join(f", {key}={value!r}" for key, value in self.values.items())
        return f"Text({self.msgid!r}{values})"

    def __eq__(self, other: object) -> bool:  # for tests
        if not isinstance(other, Text):
            return NotImplemented
        return (self.msgid, self.values) == (other.msgid, other.values)

    __hash__ = None  # type: ignore[assignment]  # equal by value, but not immutable


def fill(template: str, values: Mapping[str, object], show: Callable[[object], str] = str) -> str:
    """template.format_map with each value shown: a Text through `show`, a tuple of Texts joined
    with ", ", anything else as given (conversions and format specs such as {value!r} and
    {duty:.3g} apply to the shown value)."""
    return template.format_map({key: _shown(value, show) for key, value in values.items()})


def _shown(value: object, show: Callable[[object], str]) -> object:
    if isinstance(value, Text):
        return show(value)
    if isinstance(value, tuple) and all(isinstance(item, Text) for item in value):
        return ", ".join(show(item) for item in value)
    return value


class TextError(ValueError):
    """A ValueError whose message is a Text: str() is English, `.text` is for the command line."""

    text: Text

    def __init__(self, msgid: str, /, **values: object) -> None:
        self.text = Text(msgid, **values)
        super().__init__(str(self.text))
