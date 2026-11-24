"""lists.py's user-list routes over the db — the layer the Lists card talks to.

Design: DESIGN.md → `app/lists.py`: user lists, test-pinned.
"""

import pytest

from app import db, lists


@pytest.fixture
def client(router_client):
    return router_client(lists.router)


def _lists(client, **params):
    r = client.get("/api/lists", params=params)
    assert r.status_code == 200
    return r.json()["lists"]


def test_shopping_is_seeded_so_the_card_is_never_empty_of_lists(client):
    """v41 seeds one list."""
    assert [l["name"] for l in _lists(client)] == ["Shopping"]


def test_a_list_carries_its_items_and_open_count_in_one_payload(client):
    shopping = _lists(client)[0]["id"]
    client.post(f"/api/lists/{shopping}/items", json={"name": "milk",
                                                      "category": "Groceries"})
    r = client.post(f"/api/lists/{shopping}/items", json={"name": "eggs"})
    client.post(f"/api/list-items/{r.json()['id']}/done", json={"done": True})

    [lst] = _lists(client)
    assert [i["name"] for i in lst["items"]] == ["milk"]   # done rows excluded
    assert lst["open"] == 1

    [lst] = _lists(client, include_done=True)
    assert {i["name"] for i in lst["items"]} == {"milk", "eggs"}
    assert lst["open"] == 1        # still the count of what's outstanding


def test_lists_hold_their_own_items(client):
    """Two lists side by side don't leak into each other."""
    shopping = _lists(client)[0]["id"]
    packing = client.post("/api/lists", json={"name": "College packing"}).json()
    client.post(f"/api/lists/{shopping}/items", json={"name": "milk"})
    client.post(f"/api/lists/{packing['id']}/items", json={"name": "desk lamp"})

    got = {l["name"]: [i["name"] for i in l["items"]] for l in _lists(client)}
    assert got == {"Shopping": ["milk"], "College packing": ["desk lamp"]}


def test_new_lists_land_after_the_ones_already_there(client):
    """Chip order is creation order."""
    for name in ("College packing", "Party"):
        client.post("/api/lists", json={"name": name})
    assert [l["name"] for l in _lists(client)] == ["Shopping", "College packing", "Party"]


def test_a_duplicate_name_is_refused_case_insensitively(client):
    r = client.post("/api/lists", json={"name": "shopping"})
    assert r.status_code == 400 and "already exists" in r.json()["detail"]
    assert len(_lists(client)) == 1


def test_a_rename_cannot_collide_either(client):
    packing = client.post("/api/lists", json={"name": "College packing"}).json()
    r = client.patch(f"/api/lists/{packing['id']}", json={"name": "SHOPPING"})
    assert r.status_code == 400
    assert client.patch(f"/api/lists/{packing['id']}",
                        json={"name": "Packing"}).json()["name"] == "Packing"


def test_deleting_a_list_takes_its_items_with_it(client):
    """Deleting a list cascades to its items."""
    packing = client.post("/api/lists", json={"name": "College packing"}).json()
    item = client.post(f"/api/lists/{packing['id']}/items",
                       json={"name": "desk lamp"}).json()

    assert client.delete(f"/api/lists/{packing['id']}").status_code == 200
    assert [l["name"] for l in _lists(client)] == ["Shopping"]
    with db.session() as con:
        assert con.execute("SELECT COUNT(*) FROM list_items WHERE id=?",
                           (item["id"],)).fetchone()[0] == 0


def test_an_item_can_be_put_back_on_the_list(client):
    shopping = _lists(client)[0]["id"]
    item = client.post(f"/api/lists/{shopping}/items", json={"name": "milk"}).json()
    client.post(f"/api/list-items/{item['id']}/done", json={"done": True})
    r = client.post(f"/api/list-items/{item['id']}/done", json={"done": False})
    assert r.json()["done"] == 0 and r.json()["done_at"] is None
    assert [i["name"] for i in _lists(client)[0]["items"]] == ["milk"]


def test_missing_rows_404_rather_than_silently_doing_nothing(client):
    assert client.delete("/api/lists/404").status_code == 404
    assert client.patch("/api/lists/404", json={"name": "x"}).status_code == 404
    assert client.delete("/api/list-items/404").status_code == 404
    assert client.post("/api/list-items/404/done", json={"done": True}).status_code == 404
    r = client.post("/api/lists/404/items", json={"name": "milk"})
    assert r.status_code == 400 and "no list 404" in r.json()["detail"]


def test_an_item_needs_a_name(client):
    shopping = _lists(client)[0]["id"]
    r = client.post(f"/api/lists/{shopping}/items", json={"name": "   "})
    assert r.status_code == 400
    assert _lists(client)[0]["items"] == []
