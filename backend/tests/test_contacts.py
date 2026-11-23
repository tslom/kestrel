"""What gets handed to osascript, and what never reaches it at all.

No test here invokes osascript: `_run` is stubbed everywhere, so the suite
says nothing about whether Contacts is reachable on this machine (that's what
`python -m app.contacts check` is for). What it does pin is the argv layout —
the create script walks argv in triples, and an off-by-one there writes a
phone number into a label field on a card that then syncs to the phone.
"""

import pytest

from app import contacts


@pytest.fixture
def calls(monkeypatch):
    """Capture osascript argv instead of running it. Returns the recorded
    calls; the script itself is dropped since it's a module constant."""
    recorded = []

    def fake_run(script, *args):
        recorded.append(list(args))
        return "NEWID-1234:ABPerson"

    monkeypatch.setattr(contacts, "_run", fake_run)
    monkeypatch.setattr(contacts, "locate", lambda cid: "SOURCE-UUID")
    return recorded


@pytest.fixture
def no_addressbook(monkeypatch):
    """An AddressBook with nothing in it, so dedupe never blocks a create."""
    monkeypatch.setattr(contacts, "addressbook_records", lambda: iter(()))


# ---------- what never reaches osascript ----------

def test_a_card_with_no_name_is_refused(calls, no_addressbook):
    """Contacts will happily make a blank card; it's unfindable afterwards."""
    with pytest.raises(ValueError, match="at least a first name"):
        contacts.create(phones=["2025551111"])
    assert calls == []


def test_organization_alone_is_enough(calls, no_addressbook):
    """A vet's office or a landlord is a real card with no human name."""
    out = contacts.create(org="Falls Animal Hospital", phones=["2025551111"])
    assert out["name"] == "Falls Animal Hospital"


def test_an_email_without_an_at_sign_is_refused(calls, no_addressbook):
    with pytest.raises(ValueError, match="not an email"):
        contacts.create(first="Ada", emails=["ada.example.com"])
    assert calls == []


def test_a_phone_with_no_digits_is_refused(calls, no_addressbook):
    """normalize_handle would return '' and the CRM could never match it."""
    with pytest.raises(ValueError, match="no digits"):
        contacts.create(first="Ada", phones=["call the front desk"])
    assert calls == []


# ---------- argv layout ----------

def test_fixed_fields_come_first_and_always_all_four(calls, no_addressbook):
    """The script reads items 1-4 positionally, so an omitted org still has
    to occupy its slot as an empty string."""
    contacts.create(first="Ada", last="Lovelace", note="met at the fair")
    assert calls[0] == ["Ada", "Lovelace", "", "met at the fair"]


def test_identifiers_are_kind_label_value_triples(calls, no_addressbook):
    contacts.create(first="Ada", phones=["2025551111"], emails=["a@b.com"])
    assert calls[0][4:] == [
        "phone", "mobile", "2025551111",
        "email", "home", "a@b.com",
    ]


def test_labels_survive_when_given_as_tuples(calls, no_addressbook):
    contacts.create(first="Ada", phones=[("work", "2025551111")],
                    emails=[("work", "ada@corp.com")])
    assert calls[0][4:] == [
        "phone", "work", "2025551111",
        "email", "work", "ada@corp.com",
    ]


def test_multiple_identifiers_stay_in_order(calls, no_addressbook):
    """Order is the only thing distinguishing the primary number from the
    rest — Contacts shows the first one first."""
    contacts.create(first="Ada", phones=["2025551111", "2025552222"])
    assert calls[0][4:] == [
        "phone", "mobile", "2025551111",
        "phone", "mobile", "2025552222",
    ]


def test_argv_is_always_whole_triples(calls, no_addressbook):
    """The AppleScript `repeat while i < n` walks by 3 and would read past the
    end of argv on a partial tail."""
    contacts.create(first="Ada", phones=["2025551111", "2025552222"],
                    emails=["a@b.com"])
    assert (len(calls[0]) - 4) % 3 == 0


def test_a_quote_in_a_name_is_an_argument_not_script(calls, no_addressbook):
    """The whole reason for `on run argv` — this text is never parsed."""
    contacts.create(first='Ada" & (do shell script "whoami") & "')
    assert calls[0][0] == 'Ada" & (do shell script "whoami") & "'


def test_blank_identifiers_are_dropped_not_passed_empty(calls, no_addressbook):
    """An empty --phone from a script shouldn't create a blank phone row."""
    contacts.create(first="Ada", phones=["", "  "], emails=[""])
    assert calls[0][4:] == []


# ---------- dedupe ----------

def _book(*records):
    """records are (card_id, name, {normalized identifiers}) — the shape
    providers.messages._addressbook_records yields."""
    return lambda: iter(records)


ADA = ("ADA-1:ABPerson", "Ada Lovelace", {"2025551111"})


def test_a_shared_number_blocks_the_create(calls, monkeypatch):
    monkeypatch.setattr(contacts, "addressbook_records", _book(ADA))
    with pytest.raises(contacts.DuplicateContact) as e:
        contacts.create(first="Ada", phones=["(202) 555-1111"])
    assert e.value.matches == [{"id": "ADA-1:ABPerson", "name": "Ada Lovelace",
                                "shared": ["2025551111"]}]
    assert calls == []


def test_dedupe_normalizes_both_sides(calls, monkeypatch):
    """+1 (202) 555-1111 and 2025551111 are the same phone; the CRM already
    treats them as one person, so creating a second card would split them."""
    monkeypatch.setattr(contacts, "addressbook_records", _book(ADA))
    with pytest.raises(contacts.DuplicateContact):
        contacts.create(first="Ada", phones=["+1 (202) 555-1111"])


def test_email_case_does_not_hide_a_duplicate(calls, monkeypatch):
    monkeypatch.setattr(contacts, "addressbook_records",
                        _book(("ADA-1:ABPerson", "Ada", {"ada@corp.com"})))
    with pytest.raises(contacts.DuplicateContact):
        contacts.create(first="Ada", emails=["Ada@Corp.com"])


def test_allow_duplicate_overrides(calls, monkeypatch):
    monkeypatch.setattr(contacts, "addressbook_records", _book(ADA))
    out = contacts.create(first="Ada", phones=["2025551111"],
                          allow_duplicate=True)
    assert out["id"] == "NEWID-1234:ABPerson"
    assert len(calls) == 1


def test_an_unrelated_card_does_not_block(calls, monkeypatch):
    monkeypatch.setattr(contacts, "addressbook_records",
                        _book(("X-1:ABPerson", "Someone Else", {"2025559999"})))
    contacts.create(first="Ada", phones=["2025551111"])
    assert len(calls) == 1


def test_a_card_can_be_excluded_from_its_own_dedupe(monkeypatch):
    """The wrinkle update() hits: re-labelling a number already on the card
    would otherwise collide with itself."""
    monkeypatch.setattr(contacts, "addressbook_records", _book(ADA))
    assert contacts.matches_for(phones=["2025551111"]) != []
    assert contacts.matches_for(phones=["2025551111"],
                                exclude_id="ADA-1:ABPerson") == []


def test_an_unreadable_addressbook_lets_the_create_through(calls, monkeypatch):
    """Same best-effort posture as messages.py: no FDA means no dedupe, not a
    refusal to write."""
    monkeypatch.setattr(contacts, "addressbook_records", lambda: iter(()))
    contacts.create(first="Ada", phones=["2025551111"])
    assert len(calls) == 1


# ---------- find ----------

def test_find_matches_a_name_substring(monkeypatch):
    monkeypatch.setattr(contacts, "addressbook_records",
                        _book(ADA, ("X-1:ABPerson", "Someone Else",
                                    {"2025559999"})))
    assert contacts.find("lovelace") == [
        {"id": "ADA-1:ABPerson", "name": "Ada Lovelace",
         "identifiers": ["2025551111"]}]


def test_find_returns_the_id_update_needs(monkeypatch):
    """find -> update is the whole workflow; without an id it's a dead end."""
    monkeypatch.setattr(contacts, "addressbook_records", _book(ADA))
    assert contacts.find("lovelace")[0]["id"] == "ADA-1:ABPerson"


def test_find_matches_an_identifier(monkeypatch):
    monkeypatch.setattr(contacts, "addressbook_records", _book(ADA))
    assert len(contacts.find("5551111")) == 1


def test_find_tolerates_an_unnamed_card(monkeypatch):
    """_addressbook_records yields None for a card with no name or org."""
    monkeypatch.setattr(contacts, "addressbook_records",
                        _book(("U-1:ABPerson", None, {"2025551111"})))
    assert contacts.find("2025551111") == [
        {"id": "U-1:ABPerson", "name": None, "identifiers": ["2025551111"]}]


def test_an_empty_query_finds_nothing_rather_than_everything(monkeypatch):
    monkeypatch.setattr(contacts, "addressbook_records", _book(ADA))
    assert contacts.find("") == []
    assert contacts.find("   ") == []


# ---------- update ----------

@pytest.fixture
def one_card(monkeypatch):
    """A card that exists, with one phone stored in pretty format — so the
    normalize-to-remove path has something real to match against."""
    monkeypatch.setattr(contacts, "addressbook_records", _book(ADA))
    monkeypatch.setattr(contacts, "_raw_identifiers", lambda cid: [
        {"kind": "phone", "value": "+1 (202) 555-1111",
         "normalized": "2025551111"}])


def test_updating_a_missing_card_is_an_error(calls, monkeypatch):
    monkeypatch.setattr(contacts, "addressbook_records", _book())
    with pytest.raises(contacts.ContactError, match="no card with id"):
        contacts.update("GONE:ABPerson", first="Ada")
    assert calls == []


def test_the_id_leads_and_ops_follow_in_triples(calls, one_card):
    contacts.update("ADA-1:ABPerson", first="Augusta")
    assert calls[0] == ["ADA-1:ABPerson", "first", "Augusta", ""]


def test_none_leaves_a_field_alone(calls, one_card):
    """The core semantic: an unspecified field must not be blanked."""
    contacts.update("ADA-1:ABPerson", first="Augusta")
    assert "last" not in calls[0]
    assert "org" not in calls[0]
    assert "note" not in calls[0]


def test_empty_string_clears_a_field(calls, one_card):
    """Distinct from None — this one is a deliberate blanking."""
    contacts.update("ADA-1:ABPerson", note="")
    assert calls[0] == ["ADA-1:ABPerson", "note", "", ""]


def test_a_removal_targets_the_stored_format_not_the_typed_one(calls, one_card):
    """AppleScript matches the literal string, so "2025551111" has to become
    the "+1 (202) 555-1111" actually on the card."""
    contacts.update("ADA-1:ABPerson", remove=["2025551111"])
    assert calls[0] == ["ADA-1:ABPerson", "delphone", "+1 (202) 555-1111", ""]


def test_removing_something_not_on_the_card_raises(calls, one_card):
    """A typo'd removal reporting success is worse than one that fails."""
    with pytest.raises(ValueError, match="not on this card"):
        contacts.update("ADA-1:ABPerson", remove=["2025559999"])
    assert calls == []


def test_adding_a_number_already_on_this_card_is_not_a_duplicate(calls, one_card):
    """Self-collision — the reason matches_for grew exclude_id."""
    contacts.update("ADA-1:ABPerson", add_phones=[("work", "2025551111")])
    assert calls[0][1:] == ["phone", "work", "2025551111"]


def test_adding_a_number_on_someone_elses_card_is_a_duplicate(calls, monkeypatch):
    monkeypatch.setattr(contacts, "addressbook_records",
                        _book(ADA, ("BOB-1:ABPerson", "Bob", {"2025559999"})))
    monkeypatch.setattr(contacts, "_raw_identifiers", lambda cid: [])
    with pytest.raises(contacts.DuplicateContact):
        contacts.update("ADA-1:ABPerson", add_phones=["2025559999"])
    assert calls == []


def test_a_bad_email_never_reaches_the_card(calls, one_card):
    with pytest.raises(ValueError, match="not an email"):
        contacts.update("ADA-1:ABPerson", add_emails=["nope"])
    assert calls == []


def test_removals_are_ordered_before_additions(calls, one_card):
    """Swapping a number for a new one: deleting after adding could match and
    remove the addition instead."""
    contacts.update("ADA-1:ABPerson", remove=["2025551111"],
                    add_phones=["2025552222"])
    ops = calls[0][1:]
    assert ops.index("delphone") < ops.index("phone")


def test_a_dry_run_writes_nothing_but_reports_the_change(calls, one_card):
    out = contacts.update("ADA-1:ABPerson", first="Augusta", dry_run=True)
    assert calls == []
    assert out["applied"] is False
    assert out["changed"] == ["first='Augusta'"]
    assert out["after"] is None


def test_an_update_with_no_fields_is_a_no_op(calls, one_card):
    out = contacts.update("ADA-1:ABPerson")
    assert calls == []
    assert out["changed"] == []
    assert out["applied"] is False


def test_a_card_that_vanished_between_read_and_write_raises(one_card, monkeypatch):
    monkeypatch.setattr(contacts, "_run", lambda script, *a: "NOTFOUND")
    with pytest.raises(contacts.ContactError, match="Contacts has no card"):
        contacts.update("ADA-1:ABPerson", first="Augusta")


def test_applied_update_reports_before_and_after(calls, one_card):
    out = contacts.update("ADA-1:ABPerson", first="Augusta")
    assert out["applied"] is True
    assert out["before"]["name"] == "Ada Lovelace"
    assert out["after"]["id"] == "ADA-1:ABPerson"


# ---------- delete ----------

def test_delete_reports_true_on_ok(monkeypatch):
    monkeypatch.setattr(contacts, "_run", lambda script, *a: "OK")
    assert contacts.delete("X:ABPerson") is True


def test_deleting_an_already_gone_card_is_success(monkeypatch):
    """The user beat us to it by hand; the end state is the one we wanted."""
    monkeypatch.setattr(contacts, "_run", lambda script, *a: "NOTFOUND")
    assert contacts.delete("X:ABPerson") is True


def test_delete_reports_false_on_anything_else(monkeypatch):
    monkeypatch.setattr(contacts, "_run", lambda script, *a: "")
    assert contacts.delete("X:ABPerson") is False


# ---------- availability ----------

def test_unavailable_off_darwin_without_shelling_out(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("should not have run osascript")

    monkeypatch.setattr(contacts.sys, "platform", "linux")
    monkeypatch.setattr(contacts, "_run", explode)
    assert contacts.available() is False


def test_unavailable_when_automation_is_denied(monkeypatch):
    monkeypatch.setattr(contacts.sys, "platform", "darwin")

    def denied(*a, **k):
        raise contacts.ContactError("Not authorized to send Apple events")

    monkeypatch.setattr(contacts, "_run", denied)
    assert contacts.available() is False


# ---------- locate ----------

def test_locate_names_the_source_directory(tmp_path, monkeypatch):
    """The account-routing answer: which of the five sources took the card."""
    import sqlite3

    src = tmp_path / "Sources" / "SOURCE-UUID"
    src.mkdir(parents=True)
    db_path = src / "AddressBook-v22.abcddb"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE ZABCDRECORD (ZUNIQUEID TEXT)")
    con.execute("INSERT INTO ZABCDRECORD VALUES ('ABC-123:ABPerson')")
    con.commit()
    con.close()

    monkeypatch.setattr(contacts, "ADDRESSBOOK_GLOB",
                        str(tmp_path / "Sources" / "*" / "AddressBook-v22.abcddb"))
    assert contacts.locate("ABC-123:ABPerson") == "SOURCE-UUID"
    assert contacts.locate("NOPE:ABPerson") is None
