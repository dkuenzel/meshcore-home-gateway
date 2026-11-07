"""What a configuration implies beyond its values (ARC-009): the contacts the gateway must keep,
and the risks that the start logs and `check-config` shows (SEC-004, OPS-017, ADR 0009)."""

from __future__ import annotations

from meshcore_home_gateway.config import Config
from meshcore_home_gateway.protocol.codec import radio_name
from meshcore_home_gateway.texts import Text


def protected_keys(config: Config) -> frozenset[str]:
    """Contacts the gateway must keep: configured contact targets and command senders
    (ADR 0017)."""
    targets = {t.public_key for t in config.targets if t.public_key is not None}
    return frozenset(targets | set(config.commands.senders))


def protected_names(config: Config) -> dict[str, str]:
    """The protected contacts, each with a name for the radio profile to add it under while the
    radio lacks it, until its next advert names it (ADR 0020)."""
    names = {k: f"sender-{k[:4]}" for k in config.commands.senders}
    targets = {t.public_key: t.alias for t in config.targets if t.public_key is not None}
    return names | {key: radio_name(alias) for key, alias in targets.items()}


def start_warnings(config: Config) -> list[Text]:
    """What the service logs at its start and `check-config` shows."""
    return (
        protected_look_alikes(config)
        + alias_pairs(config)
        + ignored_secrets(config)
        + broker_warnings(config)
        + log_level_warnings(config)
    )


def ignored_secrets(config: Config) -> list[Text]:
    """The code secrets of keys that are not command senders: removing a sender ends its
    commands, so its secret is ignored until `commands revoke` removes it (#80). While one is
    ignored, also each sender without a secret, whose commands are refused (code_missing): a
    sender's key corrected by hand leaves its secret behind, and the corrected key must not
    command without a code. Once no secret is ignored, such a sender commands without one, so
    the warning says to enrol a corrected key first."""
    commands = config.commands
    warnings = [
        # Translators: key is a full public key; command is the command line that removes it
        Text(
            "commands.codes_file: {key} has a code secret but is not in commands.senders: the "
            "secret is ignored; '{command}' removes it",
            key=key,
            command=f"meshcore-home-gateway commands revoke {key}",
        )
        for key in commands.ignored_secrets
    ]
    if not commands.ignored_secrets:
        return warnings
    return warnings + [
        # Translators: security text (docs/translations.md): sender is a full public key;
        # command is the command line that gives it a code secret; code_missing, commands
        # revoke, enrol and revoke stay as they are
        Text(
            "commands.senders: {sender} has no code secret while one is ignored: its commands "
            "are refused (code_missing) until '{command}' gives it one. Once no secret is "
            "ignored (commands revoke), it commands without a code: enrol a key corrected by "
            "hand first. Restart the service after enrol or revoke",
            sender=sender,
            command=f"meshcore-home-gateway commands enrol {sender}",
        )
        for sender in sorted(commands.senders)
        if commands.secret(sender) is None
    ]


def alias_pairs(config: Config) -> list[Text]:
    """Target aliases that differ only in '-' and '_' (`a-b`, `a_b`): both are valid, but Home
    Assistant makes the same entity IDs of them, so the notify entity of the target announced
    later gets `_2`, and nothing tells which is which. One warning per group."""
    groups: dict[str, list[str]] = {}
    for target in config.targets:
        groups.setdefault(target.alias.replace("-", "_"), []).append(target.alias)
    return [
        # Translators: aliases are two or more target aliases, such as "a-b, a_b"; _2 is the
        # end of an entity ID
        Text(
            "targets {aliases} differ only in '-' and '_': Home Assistant gives their entities "
            "the same IDs and numbers the later ones (_2); rename all but one",
            aliases=", ".join(sorted(group)),
        )
        for group in groups.values()
        if len(group) > 1
    ]


def protected_look_alikes(config: Config) -> list[Text]:
    """Warnings about protected keys with one 6-byte prefix: the radio names a sender by its
    prefix only, so it can hold one of them (ADR 0009); the managed profile adds them in key
    order. Logged in English, shown by `check-config` in the command line's language."""
    roles: dict[str, list[Text]] = {}
    for target in config.targets:
        if target.public_key is not None:
            roles.setdefault(target.public_key, []).append(
                Text("target {alias}", alias=target.alias)
            )
    for key in config.commands.senders:
        roles.setdefault(key, []).append(Text("command sender"))
    keys = sorted(roles)

    def short(key: str) -> str:
        return f"{key[:12]}…{key[-8:]}"

    def named(key: str) -> Text:
        # Translators: a contact's key, shortened, and its roles, such as "target gate".
        return Text("{key} ({roles})", key=short(key), roles=tuple(roles[key]))

    profile = config.radio.profile
    writes = profile.protect_contacts and profile.manage != "observe"
    warnings = []
    for i, a in enumerate(keys):
        for b in (k for k in keys[i + 1 :] if k[:12] == a[:12]):
            if writes:  # in key order, and never next to the other
                # Translators: first and second are contacts, each a shortened key and its roles;
                # key is the first one's key. A security warning: the radio may hold the wrong one.
                warning = Text(
                    "protected contacts {first} and {second} have the same first 6 bytes: the "
                    "radio names a sender by them only (ADR 0009); the managed profile, when it "
                    "writes, adds {key} unless the other is on the radio, never both",
                    first=named(a),
                    second=named(b),
                    key=short(a),
                )
            else:
                # Translators: first and second are contacts, each a shortened key and its roles.
                warning = Text(
                    "protected contacts {first} and {second} have the same first 6 bytes: the "
                    "radio names a sender by them only (ADR 0009); the gateway adds neither, and "
                    "refuses requests to add either",
                    first=named(a),
                    second=named(b),
                )
            warnings.append(warning)
    return warnings


def broker_warnings(config: Config) -> list[Text]:
    """What the broker connection lacks (SEC-004): the gateway's topics carry messages,
    positions and command events, and only the broker guards them. None once `insecure = true`
    accepts it. Logged in English, shown by `check-config` in the command line's language."""
    mqtt = config.mqtt
    warnings: list[Text] = []
    if mqtt.insecure:
        return warnings
    if mqtt.username is None:
        # Translators: security text (docs/translations.md); insecure = true is a setting
        warnings.append(
            Text(
                "MQTT without a user name: unless the broker refuses clients without one, any "
                "client that reaches it can read and write the gateway's topics (docs/mqtt-api.md, "
                "Securing the broker; insecure = true accepts it)"
            )
        )
    if not mqtt.tls and not mqtt.local:
        # Translators: security text (docs/translations.md); insecure = true is a setting
        warnings.append(
            Text(
                "MQTT without TLS: the password, the messages, the positions and the command "
                "events cross the network in the clear (insecure = true accepts it)"
            )
        )
    return warnings


def log_level_warnings(config: Config) -> list[Text]:
    """`debug` logs what the gateway sees: texts, positions, keys, every frame (OPS-017)."""
    if config.diagnostics.log_level != "debug":
        return []
    return [
        Text(
            "log level debug: the journal gets the texts of the messages, the positions, the keys "
            "and every frame of the radio, which journald keeps for weeks; set it back to info "
            "after the search (docs/configuration.md, diagnostics)"
        )
    ]
