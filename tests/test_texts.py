"""Texts made where no language is known: str() is the English text, values filled in."""

import pytest

from meshcore_home_gateway.texts import Text, TextError, fill


def test_a_text_reads_as_english_with_its_values() -> None:
    assert str(Text("no host in {text!r}", text="192.0.2.1:")) == "no host in '192.0.2.1:'"
    assert str(Text("at most {duty:.3g} % of the time", duty=12.5)) == "at most 12.5 % of the time"
    role = Text("target {alias}", alias="gate")
    assert str(Text("{key} ({roles})", key="AB12", roles=role)) == "AB12 (target gate)"
    roles = (Text("target {alias}", alias="gate"), Text("command sender"))
    assert (
        str(Text("{key} ({roles})", key="AB12", roles=roles))
        == "AB12 (target gate, command sender)"
    )
    assert str(Text("Removed")) == "Removed"


def test_values_that_are_not_texts_stay_as_given() -> None:
    assert str(Text("{names}", names=("a", "b"))) == "('a', 'b')"
    assert str(Text("{value!r}", value=Text("gate"))) == "'gate'"  # shown first, then converted


def test_fill_shows_texts_through_the_given_function() -> None:
    nested = Text("command sender")
    assert fill("{role}; {roles}", {"role": nested, "roles": (nested, nested)}, show=repr) == (
        "Text('command sender'); Text('command sender'), Text('command sender')"
    )


def test_texts_compare_by_msgid_and_values() -> None:
    assert Text("no node {label}", label="Alice") == Text("no node {label}", label="Alice")
    assert Text("no node {label}", label="Alice") != Text("no node {label}", label="Bob")
    assert Text("no node {label}", label="Alice") != Text("no contact {label}", label="Alice")
    assert Text("Removed") != "Removed"
    assert repr(Text("no node {label}", label="Alice")) == "Text('no node {label}', label='Alice')"


def test_a_text_error_is_a_value_error_with_english_text() -> None:
    error = TextError("command word must be a single word, got {word!r}", word="open gate")
    assert isinstance(error, ValueError)
    assert str(error) == "command word must be a single word, got 'open gate'"
    assert error.text == Text("command word must be a single word, got {word!r}", word="open gate")
    with pytest.raises(ValueError, match="single word, got 'open gate'"):
        raise error
