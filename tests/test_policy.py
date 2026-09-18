import json

import pytest
from pydantic import ValidationError

from jev_frontend_qa.core.models import Policy
from jev_frontend_qa.core.policy import PolicyEnforcer


def test_permissions_do_not_cross_product_origins_methods_and_paths():
    gate = PolicyEnforcer(
        Policy.model_validate(
            {
                "network": {
                    "allowed_origins": [
                        {"origin": "https://read.example", "methods": ["GET"], "path_prefix": "/"},
                        {"origin": "https://write.example", "methods": ["POST"], "path_prefix": "/api/todos"},
                    ]
                }
            }
        )
    )
    assert gate.check_request("GET", "https://read.example/view").allowed
    assert gate.check_request("POST", "https://write.example/api/todos/owned").allowed
    assert not gate.check_request("POST", "https://read.example/api/todos").allowed
    assert not gate.check_request("POST", "https://write.example/admin").allowed
    assert not gate.check_request("POST", "https://write.example/api/todos-archive").allowed


def test_ambiguous_paths_and_url_credentials_cannot_expand_permission():
    gate = PolicyEnforcer(
        Policy.model_validate(
            {
                "network": {
                    "allowed_origins": [
                        {"origin": "https://example.com", "methods": ["POST"], "path_prefix": "/api"},
                    ]
                }
            }
        )
    )
    assert gate.check_request("POST", "https://EXAMPLE.com:443/api/todos").allowed
    for url in (
        "https://example.com/api/../admin",
        "https://example.com/api/%2e%2e/admin",
        "https://example.com/api%2ftodos",
        "https://example.com/api/%252e%252e/admin",
        "https://secret@example.com/api/todos",
        "file:///api/todos",
    ):
        assert not gate.check_request("POST", url).allowed


def test_omitting_methods_cannot_grant_mutation_permission():
    gate = PolicyEnforcer(Policy.model_validate({"network": {"allowed_origins": [{"origin": "https://example.com"}]}}))
    assert gate.check_request("GET", "https://example.com/").allowed
    assert not gate.check_request("DELETE", "https://example.com/owned").allowed


def test_invalid_policy_is_not_silently_accepted():
    with pytest.raises(ValidationError):
        Policy.model_validate({"allow_everything": True})
    with pytest.raises(ValidationError):
        Policy.model_validate({"network": {"allowed_origins": [{"origin": "https://example.com/not-an-origin"}]}})


def test_denied_disclosure_removes_labels_values_titles_and_history():
    gate = PolicyEnforcer(Policy())
    value = gate.filter_disclosable_state(
        {
            "page": {"url": "https://private.example", "title": "PRIVATE", "text": "PRIVATE"},
            "elements": [{"label": "PRIVATE", "value": "PRIVATE"}],
            "recent_actions": [{"text": "PRIVATE"}],
        }
    )
    assert "PRIVATE" not in json.dumps(value)
    assert "private.example" not in json.dumps(value)


def test_page_disclosure_does_not_implicitly_grant_history_disclosure():
    gate = PolicyEnforcer(Policy.model_validate({"model_disclosure": {"allow_page_text": True}}))
    value = gate.filter_disclosable_state(
        {
            "page": {"text": "approved"},
            "elements": [{"label": "approved"}],
            "recent_actions": [{"text": "PRIVATE_HISTORY"}],
        }
    )
    assert "approved" in json.dumps(value)
    assert "PRIVATE_HISTORY" not in json.dumps(value)
