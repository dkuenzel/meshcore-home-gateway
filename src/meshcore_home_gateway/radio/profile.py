"""Applies the managed radio profile at every connect (ADR 0020).

Before the contact table is read, the settings are reconciled (core/profile.py) and written where
the plan says so, then read back; after it, the protected contacts are made sure to be on the
radio as favourites. The result is reported for the `settings` document.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from meshcore_home_gateway.config import ProfileConfig
from meshcore_home_gateway.core import profile as policy
from meshcore_home_gateway.core.contacts import look_alike
from meshcore_home_gateway.core.profile import Applied, Mode, ProfileReport, Value
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import ContactFlag, ErrorCode
from meshcore_home_gateway.radio.session import CommandFailedError

if TYPE_CHECKING:
    from meshcore_home_gateway.radio.session import CompanionSession
    from meshcore_home_gateway.state import ProfileRecords

log = logging.getLogger(__name__)

UNSUPPORTED = ErrorCode.UNSUPPORTED_CMD.name.lower()
MAX_REPORTS = 10  # reports kept for the bridge while connects keep failing
_WHY = {
    "initial": "the first connect of this radio",
    "configured": "configured",
    "drift": "changed on the radio, set back",
}


def live_values(session: CompanionSession) -> dict[str, Value | None]:
    """The managed settings as the radio reports them; None for what this firmware lacks."""
    info = session.info
    assert info is not None  # noqa: S101 - read at the start of the session
    me = info.self_info
    return {
        policy.MANUAL_ADD: me.manual_add_contacts,
        policy.AUTOADD: session.autoadd.flags if session.autoadd else None,
        policy.TELEMETRY: me.telemetry_modes,
        policy.ADVERT_LOCATION: me.advert_location,
        policy.AIRTIME_FACTOR: session.tuning.airtime_factor_milli if session.tuning else None,
        policy.NAME: me.name,
    }


class RadioProfile:
    def __init__(
        self,
        config: ProfileConfig,
        records: ProfileRecords | None = None,
        *,
        protected: Mapping[str, str] | None = None,
    ) -> None:
        """`protected`: key -> name to give a protected contact the radio does not hold."""
        self._config = config
        self._records = records
        self._protected = dict(protected or {})
        self._mode = Mode(config.manage)
        self._applied: dict[str, Applied] = {}  # radio key -> this run's memory
        self._reports: list[ProfileReport] = []  # not yet taken by the bridge
        self.report: ProfileReport | None = None  # of the last connect

    def take_reports(self) -> list[ProfileReport]:
        """The reports of the connects since the last call, oldest first: a connect that failed
        after the profile wrote still has its report published."""
        reports, self._reports = self._reports, []
        return reports

    async def before_contacts(self, session: CompanionSession) -> None:
        self.report = None  # until this connect has one: after_contacts needs its own
        info = session.info
        assert info is not None  # noqa: S101
        radio, firmware = info.public_key, info.device.firmware_code
        want = policy.desired(
            info.self_info.frequency_khz,
            name=self._config.name,
            duty_cycle_percent=self._config.duty_cycle_percent,
        )
        applied = self._applied.get(radio)
        if applied is None:
            applied = self._records.load(radio) if self._records else Applied()
        live = live_values(session)
        plan = policy.plan(want, applied, live, mode=self._mode, firmware_code=firmware)
        failed: dict[str, bool] = {}
        for key, value in plan.writes.items():
            reason = plan.reasons[key]
            log.log(
                logging.WARNING if reason == "drift" else logging.INFO,
                "radio profile: %s %r -> %r (%s)", key, live[key], value, _WHY[reason],
            )  # fmt: skip
        sent: dict[str, Value] = {}
        for keys, frame in _frames(session, plan.writes):
            # counted as not taken until read back, also if the radio goes away at this setter;
            # the setters after it were not sent and do not count
            sent.update({key: plan.writes[key] for key in keys})
            self._remember(radio, policy.pending(applied, sent, firmware_code=firmware))
            try:
                await session.write_setting(", ".join(keys), frame)
            except CommandFailedError as exc:
                failed.update(dict.fromkeys(keys, exc.reason == UNSUPPORTED))
                log.warning("radio profile: %s not written: %s", ", ".join(keys), exc.reason)
        if plan.writes:
            await session.reread_settings()
        after = live_values(session)
        not_taken = [
            k for k, v in plan.writes.items() if k not in failed and not policy.same(k, after[k], v)
        ]
        for key in not_taken:
            log.warning("radio profile: %s reads back as %r after writing %r",
                        key, after[key], plan.writes[key])  # fmt: skip
        for key, value in plan.drift.items():
            log.info("radio profile: %s is %r on the radio, configured %r; kept",
                     key, value, want.values[key])  # fmt: skip
        for key in plan.not_converging:
            log.warning("radio profile: %s does not take %r after %d connects; no longer "
                        "written", key, want.values[key], policy.NOT_CONVERGING_AFTER)  # fmt: skip
        self._log_observing(plan, info.device.version, firmware)
        self._remember(
            radio, policy.record(applied, want, plan, after, failed, firmware_code=firmware)
        )
        me = session.info.self_info if session.info else info.self_info
        unsupported = {*plan.unsupported, *(k for k, by_firmware in failed.items() if by_firmware)}
        taken = [k for k in plan.writes if k not in failed and k not in not_taken]
        report = ProfileReport(
            mode=self._mode.value, firmware_code=firmware,
            tested=firmware in policy.TESTED_FIRMWARE, observing=plan.observing, live=after,
            desired=dict(want.values), written={k: plan.writes[k] for k in taken},
            reasons={k: r for k, r in plan.reasons.items() if k not in unsupported},
            failed=tuple(k for k in failed if k not in unsupported), not_taken=tuple(not_taken),
            drift=dict(plan.drift), not_converging=plan.not_converging,
            unsupported=tuple(sorted(unsupported)), own_position=me.position is not None,
            radio=radio,
        )  # fmt: skip
        self._keep(report)

    async def after_contacts(self, session: CompanionSession) -> bool:
        """Protected contacts on the radio, as favourites; True when the table changed."""
        report = self.report
        if not self._config.protect_contacts or not self._protected or report is None:
            return False
        records = session.contact_records
        added: list[str] = []
        favoured: list[str] = []
        missing: list[str] = []  # while observing: not on the radio
        refused: list[str] = []
        plain: list[str] = []  # while observing: on the radio, no favourite
        for key, name in sorted(self._protected.items()):
            record = records.get(key)
            if record is not None and record.flags & ContactFlag.FAVOURITE:
                continue
            if report.observing:
                (missing if record is None else plain).append(key)
                continue
            others = look_alike(key, {*records, *added})
            if record is None and others:  # the radio could not tell them apart (ADR 0009)
                refused.append(key)
                log.warning("radio profile: protected contact %s not added: %s on the radio has "
                            "the same prefix; remove it", key[:12], others[0][:12])  # fmt: skip
                continue
            try:
                if record is None:
                    await session.write_contact(
                        codec.new_contact(bytes.fromhex(key), name, ContactFlag.FAVOURITE)
                    )
                    added.append(key)
                    log.info("radio profile: protected contact %s (%r) added", key[:12], name)
                else:
                    flags = record.flags | ContactFlag.FAVOURITE
                    await session.write_contact(codec.add_update_contact(record, flags))
                    favoured.append(key)
                    log.info("radio profile: protected contact %r made a favourite", record.name)
            except CommandFailedError as exc:
                refused.append(key)
                full = " (the contact table is full)" if exc.reason == "table_full" else ""
                log.warning("radio profile: protected contact %s not written: %s%s",
                            key[:12], exc.reason, full)  # fmt: skip
        self._keep(replace(
            report, contacts_added=tuple(added), contacts_favoured=tuple(favoured),
            contacts_missing=tuple(missing), contacts_failed=tuple(refused),
            contacts_not_favourite=tuple(plain),
        ), same_connect=True)  # fmt: skip
        return bool(added or favoured)

    def _remember(self, radio: str, applied: Applied) -> None:
        self._applied[radio] = applied
        if self._records is not None:
            self._records.save(radio, applied)

    def _keep(self, report: ProfileReport, *, same_connect: bool = False) -> None:
        """The report of a connect; `same_connect`: it completes the one kept last."""
        if same_connect and self._reports:
            self._reports[-1] = report
        else:
            self._reports = [*self._reports, report][-MAX_REPORTS:]
        self.report = report

    def _log_observing(self, plan: policy.Plan, version: str, firmware: int) -> None:
        if not plan.observing:
            return
        if self._mode is Mode.OBSERVE:
            log.info("radio profile: observed only (radio.profile.manage = observe)")
        else:
            log.warning(
                "radio profile: firmware %s (code %d) is not tested; observed only "
                "(radio.profile.manage = always writes anyway)", version, firmware,
            )  # fmt: skip


_OTHER = (policy.MANUAL_ADD, policy.TELEMETRY, policy.ADVERT_LOCATION)  # one command sets them


def _frames(
    session: CompanionSession, writes: Mapping[str, Value]
) -> list[tuple[list[str], bytes]]:
    """The setter frames for the planned writes: one per command, so the radio rewrites its
    preferences file once per command."""
    info = session.info
    assert info is not None  # noqa: S101
    me, frames = info.self_info, []
    other = [key for key in _OTHER if key in writes]
    if other:
        current = {
            policy.MANUAL_ADD: me.manual_add_contacts, policy.TELEMETRY: me.telemetry_modes,
            policy.ADVERT_LOCATION: me.advert_location,
        }  # fmt: skip
        values = {**current, **{key: writes[key] for key in other}}
        frame = codec.set_other_params(
            bool(values[policy.MANUAL_ADD]), int(values[policy.TELEMETRY]),
            int(values[policy.ADVERT_LOCATION]),
        )  # fmt: skip
        frames.append((other, frame))
    if policy.AUTOADD in writes:
        frames.append(([policy.AUTOADD], codec.set_autoadd_config(int(writes[policy.AUTOADD]))))
    if policy.AIRTIME_FACTOR in writes and session.tuning is not None:
        factor = int(writes[policy.AIRTIME_FACTOR])  # the receive delay written back as read
        frame = codec.set_tuning_params(session.tuning.rx_delay_base_milli, factor)
        frames.append(([policy.AIRTIME_FACTOR], frame))
    if policy.NAME in writes:
        frames.append(([policy.NAME], codec.set_advert_name(str(writes[policy.NAME]))))
    return frames
