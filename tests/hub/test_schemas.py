"""Instance create/update validation and persistence.

Written after a PATCH carrying ``type`` returned 500 in production: ``type`` was
covered by a validator but never exercised with a real value, so the enum being
flattened to a plain ``str`` went unnoticed. Every field is therefore walked
with a valid value, an explicit null, and a blank — asserting what *persisted*,
not just the status code.
"""

from __future__ import annotations

import pytest

# field -> (a valid new value, backed by a NOT NULL column?)
FIELDS = {
    "name": ("renamed", True),
    "type": ("velocity", True),
    "root_dir": ("/srv/other", True),
    "start_command": ("java -Xmx2G -jar velocity.jar", True),
    "auto_restart": (False, True),
    "jar_path": ("velocity.jar", False),
    "java_home": ("/opt/jdk-21", False),
}

REQUIRED_TEXT = ("name", "root_dir", "start_command")


@pytest.mark.parametrize("software", ["paper", "vanilla", "velocity"])
def test_create_accepts_every_supported_type(client, node_id, instance_base, software):
    r = client.post(f"/api/nodes/{node_id}/instances", json={**instance_base, "type": software})
    assert r.status_code == 201
    assert r.json()["type"] == software


def test_create_rejects_unsupported_type(client, node_id, instance_base):
    r = client.post(f"/api/nodes/{node_id}/instances", json={**instance_base, "type": "forge"})
    assert r.status_code == 422


@pytest.mark.parametrize("field", list(FIELDS))
def test_patch_persists_each_field(client, instance_id, field):
    value = FIELDS[field][0]
    r = client.patch(f"/api/instances/{instance_id}", json={field: value})
    assert r.status_code == 200, r.text
    assert r.json()[field] == value


@pytest.mark.parametrize("field", list(FIELDS))
def test_patch_leaves_other_fields_alone(client, instance_id, instance_base, field):
    client.patch(f"/api/instances/{instance_id}", json={field: FIELDS[field][0]})
    body = client.patch(f"/api/instances/{instance_id}", json={}).json()
    for other, expected in instance_base.items():
        if other != field:
            assert body[other] == expected


@pytest.mark.parametrize("field", list(FIELDS))
def test_patch_null_is_422_only_for_not_null_columns(client, instance_id, field):
    not_null = FIELDS[field][1]
    r = client.patch(f"/api/instances/{instance_id}", json={field: None})
    assert r.status_code == (422 if not_null else 200), r.text


@pytest.mark.parametrize("field", REQUIRED_TEXT)
def test_patch_blank_string_rejected(client, instance_id, field):
    assert client.patch(f"/api/instances/{instance_id}", json={field: "   "}).status_code == 422


def test_patch_all_fields_at_once(client, instance_id):
    """What the Settings tab actually sends."""
    payload = {f: v for f, (v, _) in FIELDS.items()}
    r = client.patch(f"/api/instances/{instance_id}", json=payload)
    assert r.status_code == 200, r.text
    for field, value in payload.items():
        assert r.json()[field] == value


def test_patch_survives_a_reread(client, node_id, instance_id):
    client.patch(f"/api/instances/{instance_id}", json={"type": "vanilla", "auto_restart": False})
    rows = client.get(f"/api/nodes/{node_id}/instances").json()
    row = next(i for i in rows if i["id"] == instance_id)
    assert row["type"] == "vanilla"
    assert row["auto_restart"] is False


def test_patch_trims_whitespace(client, instance_id):
    r = client.patch(f"/api/instances/{instance_id}", json={"name": "  padded  "})
    assert (r.status_code, r.json()["name"]) == (200, "padded")


def test_create_rejects_blank_required_field(client, node_id, instance_base):
    r = client.post(f"/api/nodes/{node_id}/instances", json={**instance_base, "name": ""})
    assert r.status_code == 422


class TestRequireValue:
    """The helper directly — an enum member must survive it intact."""

    def test_enum_passes_through_unflattened(self):
        from minemanager_hub.api.schemas import _require_value
        from minemanager_shared.protocol import InstanceType

        out = _require_value(InstanceType.paper)
        assert out is InstanceType.paper
        assert hasattr(out, "value")      # the regression: .strip() lost this

    def test_null_rejected(self):
        from minemanager_hub.api.schemas import _require_value

        with pytest.raises(ValueError):
            _require_value(None)

    def test_blank_rejected_and_text_trimmed(self):
        from minemanager_hub.api.schemas import _require_value

        with pytest.raises(ValueError):
            _require_value("  ")
        assert _require_value("  x  ") == "x"

    def test_non_text_untouched(self):
        from minemanager_hub.api.schemas import _require_value

        assert _require_value(False) is False


class TestJavaHome:
    @pytest.mark.parametrize("bad", ["relative/path", "/opt/jdk;rm -rf /", "/opt/jdk-21/bin/java"])
    def test_rejected(self, client, instance_id, bad):
        assert client.patch(f"/api/instances/{instance_id}", json={"java_home": bad}).status_code == 422

    def test_trailing_slash_normalised(self, client, instance_id):
        r = client.patch(f"/api/instances/{instance_id}", json={"java_home": "/opt/jdk-21/"})
        assert (r.status_code, r.json()["java_home"]) == (200, "/opt/jdk-21")

    def test_empty_means_node_default(self, client, instance_id):
        r = client.patch(f"/api/instances/{instance_id}", json={"java_home": ""})
        assert (r.status_code, r.json()["java_home"]) == (200, None)
