"""False-pass regressions for caller-authored lifecycle contracts."""

from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from jev_frontend_qa.core.assertions import capture_network_variables, evaluate_step
from jev_frontend_qa.core.evidence import EvidenceRecord
from jev_frontend_qa.core.models import Assertion, NetworkAssertion, Policy, Scenario

ASSERTION = TypeAdapter(Assertion)


def exchange(*, path="/api/todos", method="POST", status=201, title="run-owned", **changes):
    fields = {
        "method": method,
        "url": f"http://127.0.0.1:8767{path}",
        "status": status,
        "request_body": {"title": title},
        "response_body": {"todo": {"id": "owned-123", "title": title, "completed": False}},
    }
    fields.update(changes)
    return EvidenceRecord(**fields)


def check(assertion, *, evidence=(), fresh=None, dom=None, complete=True):
    class ObservedDom:
        def evaluate_js(self, expression):
            return dom

    return evaluate_step(
        step_id="contract",
        step_goal="caller expectation",
        assertions=[ASSERTION.validate_python(assertion)],
        evidence=evidence,
        fresh_page_payloads=fresh or {},
        transport=ObservedDom() if dom is not None else None,
        policy=Policy(),
        evidence_complete=complete,
    )[0]


def persisted(criteria, **extra):
    return {
        "kind": "persistence",
        "method": "GET",
        "path": "/api/todos",
        "records_path": "$.todos",
        "contains": criteria,
        **extra,
    }


def network(**extra):
    return {
        "kind": "network",
        "method": "POST",
        "path": "/api/todos",
        "expected_status": 201,
        "payload_contains": {"title": "run-owned"},
        "response_contains": {"todo": {"title": "run-owned", "completed": False}},
        **extra,
    }


def test_wrong_submitted_payload_cannot_hide_behind_correct_response():
    result = check(network(), evidence=[exchange(request_body={"title": "different"})])
    assert not result.passed
    assert result.observed["payload"] == {"title": "different"}


def test_response_fields_cannot_be_combined_across_records():
    body = {"todo": [{"title": "run-owned", "completed": True}, {"title": "other", "completed": False}]}
    assert not check(network(), evidence=[exchange(response_body=body)]).passed


def test_endpoint_prefix_does_not_satisfy_exact_item_contract():
    assertion = network(method="PATCH", path="/api/todos/1", expected_status=200)
    assert not check(assertion, evidence=[exchange(method="PATCH", path="/api/todos/10", status=200)]).passed


def test_second_submission_does_not_mask_ambiguous_first_write():
    records = [exchange(status=500), exchange()]
    assert not check(network(), evidence=records).passed


def test_empty_204_is_valid_delete_evidence():
    assertion = {"kind": "network", "method": "DELETE", "path": "/api/todos/owned-123", "expected_status": 204}
    record = exchange(method="DELETE", path="/api/todos/owned-123", status=204, request_body=None, response_body=None)
    assert check(assertion, evidence=[record]).passed


@pytest.mark.parametrize("changes", [{"incomplete": True}, {"error": "connection lost"}, {"status": None}])
def test_unusable_exchange_never_passes_network_contract(changes):
    assert not check(network(), evidence=[exchange(**changes)]).passed


def test_old_write_response_cannot_replace_fresh_persistence():
    assert not check(persisted({"title": "run-owned"}), evidence=[exchange()]).passed


def test_fresh_read_for_another_path_cannot_certify_persistence():
    fresh = {"/api/archived-todos": {"todos": [{"title": "run-owned"}]}}
    assert not check(persisted({"title": "run-owned"}), fresh=fresh).passed


def test_fresh_record_must_match_all_fields_on_same_row():
    fresh = {
        "/api/todos": {
            "todos": [
                {"id": "owned-123", "title": "run-owned", "completed": True},
                {"id": "other", "title": "other", "completed": False},
            ]
        }
    }
    assert not check(persisted({"id": "owned-123", "title": "run-owned", "completed": False}), fresh=fresh).passed


def test_nested_unrelated_fields_cannot_satisfy_record_criteria():
    fresh = {"/api/todos": {"todos": [{"title": "run-owned", "metadata": {"completed": False}}]}}
    assert not check(persisted({"title": "run-owned", "completed": False}), fresh=fresh).passed


def test_persistence_compares_exact_titles_not_prefixes():
    fresh = {"/api/todos": {"todos": [{"title": "run-owned-extra", "completed": False}]}}
    assert not check(persisted({"title": "run-owned"}), fresh=fresh).passed


def test_numeric_zero_is_not_boolean_false():
    fresh = {"/api/todos": {"todos": [{"title": "run-owned", "completed": 0}]}}
    assert not check(persisted({"title": "run-owned", "completed": False}), fresh=fresh).passed


def test_delete_absence_ignores_unrelated_rows_but_detects_owned_row():
    assertion = persisted({"id": "owned-123"}, absent=True)
    unrelated = {"id": "unrelated", "title": "Leave this alone"}
    assert check(assertion, fresh={"/api/todos": {"todos": [unrelated]}}).passed
    assert not check(assertion, fresh={"/api/todos": {"todos": [unrelated, {"id": "owned-123"}]}}).passed


@pytest.mark.parametrize("payload", [None, {}, {"error": "unavailable"}, {"todos": None}, {"todos": [None]}])
def test_absence_requires_a_valid_fresh_collection(payload):
    assert not check(persisted({"id": "owned-123"}, absent=True), fresh={"/api/todos": payload}).passed


def test_empty_successful_collection_proves_absence():
    assert check(persisted({"title": ""}, absent=True), fresh={"/api/todos": {"todos": []}}).passed


def test_no_request_requires_a_complete_observation_window():
    assertion = {"kind": "no_request", "method": "POST", "path": "/api/todos"}
    assert check(assertion, complete=True).passed
    assert not check(assertion, complete=False).passed
    assert not check(assertion, complete=None).passed
    assert not check(assertion, evidence=[exchange()]).passed


def test_lost_unrelated_exchange_invalidates_negative_assertion():
    assertion = {"kind": "no_request", "method": "POST", "path": "/api/todos"}
    assert not check(assertion, evidence=[exchange(method="GET", path="/api/health", incomplete=True)]).passed


def test_dom_text_requires_one_precise_element_not_first_matching_row():
    assertion = {"kind": "equals", "selector": {"testid": "todo-title"}, "expected": "run-owned"}
    assert not check(assertion, dom=["run-owned", "unrelated"]).passed
    assert check(assertion, dom=["run-owned"]).passed


def test_missing_dom_element_cannot_equal_null_or_contain_empty_text():
    assert not check({"kind": "equals", "selector": {"css": ".missing"}, "expected": None}, dom=[]).passed
    assert not check({"kind": "contains", "selector": {"css": ".missing"}, "expected": ""}, dom=[]).passed


def test_dom_absence_and_completion_attribute_are_explicit_checks():
    assert check({"kind": "count", "selector": {"css": "[data-todo-id='owned-123']"}, "expected": 0}, dom=[]).passed
    completed = {
        "kind": "attribute",
        "selector": {"css": "[data-todo-id='owned-123']"},
        "name": "data-completed",
        "expected": "true",
    }
    assert not check(completed, dom=["false"]).passed
    assert check(completed, dom=["true"]).passed


def test_capture_requires_verified_unique_safe_record_identity():
    assertion = NetworkAssertion.model_validate(network(capture={"todo_id": "$.todo.id"}))
    assert capture_network_variables([assertion], [exchange()]) == {"todo_id": "owned-123"}
    with pytest.raises(ValueError):
        capture_network_variables([assertion], [exchange(), exchange()])
    with pytest.raises(ValueError):
        capture_network_variables([assertion], [exchange(request_body={"title": "other"})])
    with pytest.raises(ValueError):
        capture_network_variables(
            [assertion], [exchange(response_body={"todo": {"title": "run-owned", "completed": False}})]
        )
    with pytest.raises(ValueError):
        capture_network_variables(
            [assertion],
            [exchange(response_body={"todo": {"id": "../other", "title": "run-owned", "completed": False}})],
        )


def test_runs_generate_distinct_uuid_owned_fixture_names():
    definition = {
        "name": "isolated",
        "start_url": "http://127.0.0.1:8767/",
        "mode": "exploratory",
        "steps": [{"id": "inspect", "goal": "Inspect only"}],
    }
    first, second = Scenario.model_validate(definition), Scenario.model_validate(definition)
    assert first.run_id != second.run_id
    assert UUID(first.run_id).version == 4
    with pytest.raises(ValidationError):
        Scenario.model_validate({**definition, "mode": "contract"})


def test_cleanup_cannot_be_declared_without_explicit_scope():
    definition = {
        "name": "unsafe-cleanup",
        "start_url": "http://127.0.0.1:8767/",
        "mode": "exploratory",
        "steps": [{"id": "inspect", "goal": "Inspect"}],
        "cleanup": [{"id": "cleanup", "goal": "Delete all rows"}],
    }
    with pytest.raises(ValidationError):
        Scenario.model_validate(definition)
