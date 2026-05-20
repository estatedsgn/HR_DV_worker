from app.models.inbound_event import InboundEvent


def test_inbound_event_has_account_and_dialog_foreign_keys() -> None:
    account_fk = list(InboundEvent.__table__.c.account_id.foreign_keys)
    dialog_fk = list(InboundEvent.__table__.c.dialog_id.foreign_keys)

    assert len(account_fk) == 1
    assert account_fk[0].target_fullname == "accounts.id"
    assert account_fk[0].ondelete == "SET NULL"

    assert len(dialog_fk) == 1
    assert dialog_fk[0].target_fullname == "dialogs.id"
    assert dialog_fk[0].ondelete == "SET NULL"
