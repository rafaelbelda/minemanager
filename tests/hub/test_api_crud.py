"""Node and instance CRUD, and what the API does when no agent is connected."""

from __future__ import annotations


class TestNodes:
    def test_create_returns_a_token_once(self, client):
        r = client.post("/api/nodes", json={"name": "box-a"})
        assert r.status_code == 201
        body = r.json()
        assert body["enrollment_token"] and body["expires_in_s"] > 0
        # the token is not retrievable afterwards
        assert "enrollment_token" not in client.get("/api/nodes").json()[0]

    def test_listed_node_starts_offline_and_unenrolled(self, client, node_id):
        node = client.get("/api/nodes").json()[0]
        assert (node["online"], node["enrolled"]) == (False, False)

    def test_reenroll_mints_a_new_token(self, client, node_id):
        first = client.post(f"/api/nodes/{node_id}/reenroll").json()["enrollment_token"]
        second = client.post(f"/api/nodes/{node_id}/reenroll").json()["enrollment_token"]
        assert first != second

    def test_reenroll_unknown_node_404s(self, client):
        assert client.post("/api/nodes/nope/reenroll").status_code == 404

    def test_delete_cascades_to_instances(self, client, node_id, instance_id):
        assert client.delete(f"/api/nodes/{node_id}").status_code == 204
        assert client.get(f"/api/nodes/{node_id}/instances").status_code == 404
        assert client.get("/api/nodes").json() == []

    def test_delete_unknown_node_404s(self, client):
        assert client.delete("/api/nodes/nope").status_code == 404


class TestInstances:
    def test_create_and_list(self, client, node_id, instance_id):
        rows = client.get(f"/api/nodes/{node_id}/instances").json()
        assert [r["id"] for r in rows] == [instance_id]
        assert rows[0]["start_with_node"] is False

    def test_create_on_unknown_node_404s(self, client, instance_base):
        assert client.post("/api/nodes/nope/instances", json=instance_base).status_code == 404

    def test_patch_unknown_instance_404s(self, client):
        assert client.patch("/api/instances/nope", json={"name": "x"}).status_code == 404

    def test_delete(self, client, node_id, instance_id):
        assert client.delete(f"/api/instances/{instance_id}").status_code == 204
        assert client.get(f"/api/nodes/{node_id}/instances").json() == []

    def test_delete_unknown_instance_404s(self, client):
        assert client.delete("/api/instances/nope").status_code == 404


class TestWithoutAnAgent:
    """Every proxied route must fail as 409, not hang or 500."""

    def test_power_is_409(self, client, instance_id):
        assert client.post(f"/api/instances/{instance_id}/power/start").status_code == 409

    def test_files_list_is_409(self, client, instance_id):
        assert client.get(f"/api/instances/{instance_id}/files").status_code == 409

    def test_console_is_409(self, client, instance_id):
        r = client.post(f"/api/instances/{instance_id}/console", json={"line": "say hi"})
        assert r.status_code == 409

    def test_unknown_instance_is_404_not_409(self, client):
        assert client.post("/api/instances/nope/power/start").status_code == 404

    def test_instance_states_degrades_to_empty(self, client, node_id, instance_id):
        """Best-effort: an offline node yields no states rather than an error."""
        r = client.get(f"/api/nodes/{node_id}/instance-states")
        assert (r.status_code, r.json()) == (200, {"states": {}})

    def test_unknown_power_op_is_400(self, client, instance_id):
        assert client.post(f"/api/instances/{instance_id}/power/explode").status_code == 400


class TestMeta:
    def test_health(self, client):
        assert client.get("/api/health").json()["status"] == "ok"

    def test_ui_config_exposes_the_editor_limits(self, client):
        body = client.get("/api/config").json()
        assert {"editor_warn_bytes", "editor_max_bytes", "transfer_cap_bytes"} <= set(body)

    def test_docs_are_off_by_default(self, client):
        assert client.get("/openapi.json").status_code == 404
