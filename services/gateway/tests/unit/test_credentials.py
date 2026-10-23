import logging
from typing import Any

import pytest
from jarvis_core.config import ApiDependency
from jarvis_core.secretbox import AesGcmSecretBox
from jarvis_gateway.config.snapshot import ApiEntry, Snapshot
from jarvis_gateway.credentials import CredentialUnavailable, CredentialVault

pytestmark = pytest.mark.unit

BOX = AesGcmSecretBox(bytes(range(32)))
OTHER_BOX = AesGcmSecretBox(bytes(range(1, 33)))
PLAINTEXT = "sk-provider-PLAINTEXT-123"
LOGGER = "jarvis_gateway.credentials"


def _entry(
    api_id: str = "a1",
    secret_ref: str | None = "sec_1",
    mode: str = "bearer",
    plain: str = PLAINTEXT,
    box: AesGcmSecretBox = BOX,
    version: int = 1,
    seal: bool = True,
) -> ApiEntry:
    cred: dict[str, Any] = {"mode": mode}
    if mode == "header":
        cred["header_name"] = "x-token"
    if secret_ref and mode != "none":
        cred["secret_ref"] = secret_ref
    config = ApiDependency.model_validate(
        {"slug": api_id, "name": api_id, "base_url": "https://api.example.com", "credential": cred}
    )
    sealed = box.seal(plain, context=secret_ref) if seal and secret_ref else None
    return ApiEntry(api_id, "p1", api_id, version, config, sealed)


def _snap(*entries: ApiEntry) -> Snapshot:
    return Snapshot.empty("postgres", 1.0).with_changes(apis={e.api_id: e for e in entries})


@pytest.mark.req("REQ-4.5-7")
def test_a_sealed_secret_is_opened_for_its_api() -> None:
    vault = CredentialVault(BOX)
    entry = _entry()
    vault.refresh(_snap(entry))
    assert vault.secret_for(entry) == PLAINTEXT


def test_apis_that_need_no_secret_return_none() -> None:
    vault = CredentialVault(BOX)
    entry = _entry(mode="none", secret_ref=None)
    vault.refresh(_snap(entry))
    assert vault.secret_for(entry) is None


def test_a_credentialed_api_without_a_held_secret_is_unavailable() -> None:
    vault = CredentialVault(BOX)
    entry = _entry(seal=False)
    vault.refresh(_snap(entry))
    with pytest.raises(CredentialUnavailable):
        vault.secret_for(entry)


def test_a_record_sealed_for_another_row_is_unavailable() -> None:
    vault = CredentialVault(BOX)
    moved = _entry()
    other = BOX.seal(PLAINTEXT, context="sec_OTHER")
    entry = ApiEntry(moved.api_id, "p1", "a1", 1, moved.config, other)
    vault.refresh(_snap(entry))
    with pytest.raises(CredentialUnavailable):
        vault.secret_for(entry)


@pytest.mark.req("REQ-4.5-7")
def test_a_rotated_master_key_leaves_the_secret_unavailable_and_logs_once_without_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    vault = CredentialVault(OTHER_BOX)
    entry = _entry()
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        vault.refresh(_snap(entry))
        vault.refresh(_snap(entry))
    with pytest.raises(CredentialUnavailable):
        vault.secret_for(entry)
    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(records) == 1
    text = records[0].getMessage()
    assert "a1" in text
    assert entry.sealed is not None
    for secret in (PLAINTEXT, entry.sealed.key_id.encode().hex(), "sec_1"):
        assert secret not in text


def test_a_changed_secret_replaces_the_old_one_and_a_deleted_api_is_forgotten() -> None:
    vault = CredentialVault(BOX)
    old = _entry(plain="old-secret", secret_ref="sec_1", version=1)
    vault.refresh(_snap(old))
    new = _entry(plain="new-secret", secret_ref="sec_2", version=2)
    vault.refresh(_snap(new))
    assert vault.secret_for(new) == "new-secret"
    with pytest.raises(CredentialUnavailable):
        vault.secret_for(old)  # the replaced secret is gone
    vault.refresh(Snapshot.empty("postgres", 2.0))
    with pytest.raises(CredentialUnavailable):
        vault.secret_for(new)


def test_the_warning_repeats_after_a_fix_and_a_new_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    vault = CredentialVault(BOX)
    bad = _entry(box=OTHER_BOX)
    good = _entry()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        vault.refresh(_snap(bad))
        vault.refresh(_snap(bad))
        vault.refresh(_snap(good))
        vault.refresh(_snap(bad))
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_repr_has_no_secret() -> None:
    vault = CredentialVault(BOX)
    vault.refresh(_snap(_entry()))
    assert PLAINTEXT not in repr(vault)
    assert PLAINTEXT not in str(vault)
