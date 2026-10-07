"""The write engine against the fake service, over plain HTTP (no browser)."""
from __future__ import annotations

import copy

import pytest

from conftest import direct_api
from innoslate_mcp.plan import PlanError, Planner, has_rel

REQ_PLAN = {
    "entities": [
        {"key": "sr", "class": "Artifact", "number": "SR.2", "name": "MP. Stakeholder Requirements",
         "labels": ["Requirements Document"], "description": "Derived from UN.1."},
        {"key": "r1", "class": "Requirement", "number": "SR.2.1", "name": "Live view",
         "description": "The system shall display live activity for each area.", "labels": ["Functional Requirement"],
         "attributes": {"Rationale": "UN.1.1", "Status": "Draft"}, "document": "sr",
         "relations": {"traced from": [{"number": "UN.1.1", "document": 2001}]}},
        {"key": "r2", "class": "Requirement", "number": "SR.2.2", "name": "Rule alerts",
         "description": "The system shall raise an alert for each rule violation it receives.",
         "labels": ["Functional Requirement"], "document": "sr",
         "relations": {"traced from": [{"number": "UN.1.2", "document": {"number": "UN.1", "name": "MP. User Needs Document"}}]}},
        {"key": "iss", "class": "Issue", "name": "Alert latency is TBD", "relations": {"caused by": ["r2"]}},
    ],
}


async def run(api, plan, commit=False):
    p = Planner(api, api.p.pid)
    c = await p.compile(copy.deepcopy(plan))
    rep = p.report(c)
    res = await p.commit(c) if commit else None
    return rep, res


async def test_dry_run_writes_nothing(api, fake):
    before = len(fake.store.posts)
    rep, _ = await run(api, REQ_PLAN)
    s = rep["summary"]
    assert (s["create"], s["update"], s["unchanged"]) == (4, 0, 0)
    assert s["relations_added"] == 5     # 2 sourced by, 2 traced from, 1 caused by
    assert "UN.1.1 Live view" in rep["existing_entities_touched_for_relations"]
    assert len(fake.store.posts) == before
    assert not fake.store.allocated


async def test_commit_writes_both_sides_and_verifies(api, fake):
    rep, res = await run(api, REQ_PLAN, commit=True)
    assert res["verify"]["ok"], res["verify"]["problems"]
    assert len(fake.store.posts) == 1
    E = fake.store.E
    r1 = next(e for e in E.values() if e["number"] == "SR.2.1")
    doc = next(e for e in E.values() if e["number"] == "SR.2")
    assert has_rel(r1, 78, doc["id"]) and has_rel(doc, 77, r1["id"])           # sourced by / source of
    assert has_rel(r1, 81, 2101) and has_rel(E[2101], 82, r1["id"])           # traced from / to
    assert not has_rel(r1, 81, 1101)                                          # never the senior team's UN.1.1
    assert r1["attrs"]["52"] == "UN.1.1" and r1["labelIds"] == [103]
    assert r1["description"] == "<p>The system shall display live activity for each area.</p>"
    iss = next(e for e in E.values() if e["name"] == "Alert latency is TBD")
    r2 = next(e for e in E.values() if e["number"] == "SR.2.2")
    assert has_rel(iss, 3, r2["id"]) and has_rel(r2, 4, iss["id"])


async def test_rerun_is_idempotent(api, fake):
    await run(api, REQ_PLAN, commit=True)
    n = len(fake.store.E)
    plan = copy.deepcopy(REQ_PLAN)
    plan["entities"] = plan["entities"][:3]           # the issue has no number, it would be created again
    rep, res = await run(api, plan, commit=True)
    assert rep["summary"]["create"] == 0 and rep["summary"]["unchanged"] == 3
    assert res["saved"] == 0
    assert len(fake.store.E) == n


async def test_update_existing_changes_text_only_when_asked(api, fake):
    await run(api, REQ_PLAN, commit=True)
    plan = copy.deepcopy(REQ_PLAN)
    plan["entities"] = plan["entities"][:2]
    plan["entities"][1]["description"] = "The system shall display live video for each of the four areas."
    rep, _ = await run(api, plan, commit=True)
    r1 = next(e for e in fake.store.E.values() if e["number"] == "SR.2.1")
    assert "live activity" in r1["description"]                           # not changed without update_existing
    plan["update_existing"] = True
    rep, res = await run(api, plan, commit=True)
    assert rep["summary"]["update"] == 2 and res["verify"]["ok"]
    r1 = next(e for e in fake.store.E.values() if e["number"] == "SR.2.1")
    assert "live video" in r1["description"] and r1["version"] == 2


async def test_bare_number_that_repeats_is_refused(api):
    plan = {"entities": [{"key": "x", "class": "Requirement", "number": "SR.9.1", "name": "x",
                          "relations": {"traced from": ["UN.1.1"]}}]}
    with pytest.raises(PlanError) as e:
        await run(api, plan)
    msg = str(e.value)
    assert "2 entities match" in msg and "1101" in msg and "2101" in msg


async def test_ambiguous_document_number_is_refused(api):
    plan = {"entities": [{"key": "x", "class": "Statement", "number": "UN.1.9", "name": "x",
                          "document": {"number": "UN.1"}}]}
    with pytest.raises(PlanError, match="2 entities match"):
        await run(api, plan)


async def test_new_document_with_existing_number_warns(api):
    plan = {"entities": [{"key": "d", "class": "Artifact", "number": "SR.1", "name": "MP. Stakeholder Requirements",
                          "labels": ["Requirements Document"]}]}
    rep, _ = await run(api, plan)
    assert rep["summary"]["create"] == 1
    assert any("already use number 'SR.1'" in w and "1002" in w for w in rep["warnings"])


async def test_bad_names_are_all_reported_with_suggestions(api):
    plan = {"entities": [
        {"key": "a", "class": "Requirment", "name": "x"},
        {"key": "b", "class": "Requirement", "name": "y", "labels": ["Functionl Requirement"],
         "attributes": {"Rationle": "z", "Quality Score": 90}, "relations": {"tracd from": [2101]}},
        {"key": "c", "class": "Artifact", "name": "z", "attributes": {"File": "abc"}},
    ]}
    with pytest.raises(PlanError) as e:
        await run(api, plan)
    msg = str(e.value)
    assert "Unknown class 'Requirment'" in msg
    # stops at the class error first; fix it and the rest are listed together
    plan["entities"][0]["class"] = "Requirement"
    with pytest.raises(PlanError) as e:
        await run(api, plan)
    msg = str(e.value)
    for frag in ("Functional Requirement", "Rationale", "computed by Innoslate", "innoslate_attach_file", "traced from"):
        assert frag in msg, frag


async def test_label_on_wrong_class_warns(api):
    rep, _ = await run(api, {"entities": [{"key": "a", "class": "Statement", "name": "x",
                                           "labels": ["Functional Requirement"]}]})
    assert any("not defined for class Statement" in w for w in rep["warnings"])


async def test_nested_items_use_decomposes_and_match_on_rerun(api, fake):
    plan = {"entities": [
        {"key": "n", "class": "Statement", "number": "UN.1.2.1", "name": "Alert sound", "labels": ["User Need"],
         "description": "Supervisors need an audible alert.", "document": 2001,
         "parent": {"number": "UN.1.2", "document": 2001}},
    ]}
    rep, res = await run(api, plan, commit=True)
    assert res["verify"]["ok"]
    n = next(e for e in fake.store.E.values() if e["number"] == "UN.1.2.1")
    assert has_rel(n, 10, 2102) and has_rel(fake.store.E[2102], 11, n["id"])
    assert not has_rel(n, 78, 2001)                       # deeper items join the parent, not the document
    rep, res = await run(api, plan, commit=True)
    assert rep["summary"]["unchanged"] == 1 and res["saved"] == 0


async def test_reference_into_document_created_by_same_plan(api, fake):
    plan = copy.deepcopy(REQ_PLAN)
    plan["entities"].append({"key": "v", "class": "Requirement", "number": "SR.2.3", "name": "Check view",
                             "document": "sr", "relations": {"traced from": [{"number": "SR.2.1", "document": "sr"}]}})
    rep, res = await run(api, plan, commit=True)
    assert res["verify"]["ok"]
    v = next(e for e in fake.store.E.values() if e["number"] == "SR.2.3")
    r1 = next(e for e in fake.store.E.values() if e["number"] == "SR.2.1")
    assert has_rel(v, 81, r1["id"]) and has_rel(r1, 82, v["id"])


async def test_unlink_removes_both_sides(api, fake):
    await run(api, REQ_PLAN, commit=True)
    r1 = next(e for e in fake.store.E.values() if e["number"] == "SR.2.1")
    rep, res = await run(api, {"unlink": [{"from": r1["id"], "relation": "traced from", "to": 2101}]}, commit=True)
    assert rep["summary"]["relations_removed"] == 1 and res["verify"]["ok"]
    assert not has_rel(fake.store.E[r1["id"]], 81, 2101) and not has_rel(fake.store.E[2101], 82, r1["id"])
    rep, _ = await run(api, {"unlink": [{"from": r1["id"], "relation": "traced from", "to": 2101}]})
    assert any("did not exist" in w for w in rep["warnings"])


async def test_explicit_id_update_and_class_check(api, fake):
    rep, res = await run(api, {"entities": [{"id": 2103, "name": "Camera status", "add_labels": ["Evidence"]}]}, commit=True)
    assert res["verify"]["ok"] and rep["summary"]["update"] == 1
    assert fake.store.E[2103]["name"] == "Camera status" and fake.store.E[2103]["labelIds"] == [120, 121]
    with pytest.raises(PlanError, match="is a Statement, not Requirement"):
        await run(api, {"entities": [{"id": 2103, "class": "Requirement", "name": "x"}]})


async def test_plan_shape_errors(api):
    for bad, frag in [([], "JSON object"), ({"entities": {}}, "must be a list"), ({"entitys": []}, "Unknown top-level"),
                      ({"entities": [{"class": "Issue"}]}, "'name' is required"),
                      ({"entities": [{"key": "a", "class": "Issue", "name": "a"}, {"key": "a", "class": "Issue", "name": "b"}]}, "used twice"),
                      ({"relations": [{"from": 2101, "to": 2102}]}, "needs 'from', 'relation' and 'to'"),
                      ({"relations": [{"from": 2101, "relation": "traced to", "to": 2101}]}, "itself"),
                      ({"relations": [{"from": 999999, "relation": "traced to", "to": 2101}]}, "no entity with id 999999"),
                      ({"entities": [{"key": "a", "class": "Issue", "name": "a", "colour": "red"}]}, "unknown fields"),
                      ({}, "empty"), ({"relations": {}}, "must be a list")]:
        with pytest.raises(PlanError, match=frag):
            await run(api, bad)


async def test_one_way_save_is_rejected_by_the_service(api):
    """Sanity check of the fake: a relation on one side only is refused, as on the real service."""
    from innoslate_mcp.browser import InnoslateError
    e = await api.get(2101)
    e["rels"].append({"relationId": 81, "targetId": 2102, "attrs": {}})
    with pytest.raises(InnoslateError, match="one-way"):
        await api.save([e])


async def test_lookups_work_when_search_ignores_numbers(api_nonum, fake_nonum):
    api = api_nonum
    plan = {"entities": [
        {"key": "r", "class": "Requirement", "number": "SR.5.1", "name": "Health",
         "description": "The system shall show the state of each camera.",
         "relations": {"traced from": [{"number": "UN.1.3", "class": "Statement"}]},
         "document": {"number": "UN.1", "name": "MP. User Needs Document"}},
    ]}
    rep, res = await run(api, plan, commit=True)
    assert res["verify"]["ok"]
    r = next(e for e in fake_nonum.store.E.values() if e["number"] == "SR.5.1")
    assert has_rel(r, 81, 2103)
    docs = await api.find_documents(number="SR.1")
    assert [d["id"] for d in docs] == [1002]


async def test_document_members_walks_the_tree(api):
    doc = await api.get(2001)
    m = {e["number"]: d for d, e in await api.document_members(doc)}
    assert m == {"UN.1.1": 0, "UN.1.2": 0, "UN.1.3": 0, "UN.1.1.1": 1}


async def test_paging_past_500(fake):
    from fake_innoslate import _ent
    for i in range(1205):
        e = _ent(50000 + i, "Statement", f"BULK.{i}", f"bulk {i}")
        fake.store.E[e["id"]] = e
    api = direct_api(fake)
    hits = await api.search_all("class:Statement")
    assert len(hits) == 1205 + 6   # 6 Statements in the seed


async def test_shipped_example_plan_runs(api, fake):
    import json
    from pathlib import Path
    plan = json.loads((Path(__file__).parent.parent / "examples" / "stakeholder_requirements_plan.json").read_text())
    rep, res = await run(api, plan, commit=True)
    assert res["verify"]["ok"], res["verify"]["problems"]
    assert rep["summary"]["create"] == 5
    assert any("already use number 'SR.1'" in w for w in rep["warnings"])     # the seed's senior copy
    child = next(e for e in fake.store.E.values() if e["number"] == "SR.1.1.1")
    parent = next(e for e in fake.store.E.values() if e["number"] == "SR.1.1" and e["name"] == "Open alert list")
    assert has_rel(child, 10, parent["id"])
