from __future__ import annotations

import io
import json

import numpy as np
import trimesh
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _export_glb(mesh: trimesh.Trimesh) -> bytes:
    out = mesh.export(file_type="glb")
    if isinstance(out, bytes):
        return out
    return out.encode("utf-8")


def _table_mesh_with_uv(extents: tuple[float, float, float] = (2.0, 0.2, 2.0)) -> trimesh.Trimesh:
    mesh = trimesh.creation.box(extents=extents)
    vertices = mesh.vertices
    uv = np.zeros((len(vertices), 2), dtype=np.float64)

    x_min, x_max = vertices[:, 0].min(), vertices[:, 0].max()
    z_min, z_max = vertices[:, 2].min(), vertices[:, 2].max()
    x_span = x_max - x_min if x_max > x_min else 1.0
    z_span = z_max - z_min if z_max > z_min else 1.0
    uv[:, 0] = (vertices[:, 0] - x_min) / x_span
    uv[:, 1] = (vertices[:, 2] - z_min) / z_span
    mesh.visual = trimesh.visual.texture.TextureVisuals(uv=uv)
    return mesh


def _post_erase(mesh_data: bytes, request_payload: dict, filename: str = "mesh.glb"):
    return client.post(
        "/v1/mesh/erase-box",
        files={
            "mesh_file": (filename, mesh_data, "application/octet-stream"),
            "request": (None, json.dumps(request_payload)),
        },
    )


def _header_int(response, key: str) -> int:
    return int(response.headers[key])


def test_box_outside_mesh_keeps_same_triangle_count():
    mesh = _table_mesh_with_uv()
    mesh_data = _export_glb(mesh)

    response = _post_erase(
        mesh_data,
        {
            "boxes": [
                {
                    "center": [100.0, 100.0, 100.0],
                    "rotation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                    "size": [1.0, 1.0, 1.0],
                }
            ],
            "space": "mesh_local",
            "remove_rule": "intersects",
            "weld_vertices": True,
            "recalc_normals": False,
        },
    )

    assert response.status_code == 200
    assert _header_int(response, "X-Triangles-Before") == _header_int(response, "X-Triangles-After")


def test_box_intersecting_table_reduces_triangles_and_keeps_uv():
    mesh = _table_mesh_with_uv()
    mesh_data = _export_glb(mesh)

    response = _post_erase(
        mesh_data,
        {
            "boxes": [
                {
                    "center": [0.0, 0.0, 0.0],
                    "rotation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                    "size": [0.8, 0.8, 0.8],
                }
            ],
            "space": "mesh_local",
            "remove_rule": "intersects",
            "weld_vertices": True,
            "recalc_normals": False,
        },
    )

    assert response.status_code == 200
    assert _header_int(response, "X-Triangles-After") < _header_int(response, "X-Triangles-Before")

    loaded = trimesh.load(io.BytesIO(response.content), file_type="glb", force="mesh")
    assert isinstance(loaded, trimesh.Trimesh)
    assert hasattr(loaded.visual, "uv")
    assert loaded.visual.uv is not None


def test_multiple_boxes_are_all_applied():
    mesh = _table_mesh_with_uv(extents=(4.0, 0.2, 1.0))
    mesh_data = _export_glb(mesh)

    base_payload = {
        "space": "mesh_local",
        "remove_rule": "intersects",
        "weld_vertices": True,
        "recalc_normals": False,
    }

    single = _post_erase(
        mesh_data,
        {
            **base_payload,
            "boxes": [
                {
                    "center": [-1.2, 0.0, 0.0],
                    "rotation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                    "size": [0.8, 0.8, 0.8],
                }
            ],
        },
    )
    multi = _post_erase(
        mesh_data,
        {
            **base_payload,
            "boxes": [
                {
                    "center": [-1.2, 0.0, 0.0],
                    "rotation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                    "size": [0.8, 0.8, 0.8],
                },
                {
                    "center": [1.2, 0.0, 0.0],
                    "rotation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                    "size": [0.8, 0.8, 0.8],
                },
            ],
        },
    )

    assert single.status_code == 200
    assert multi.status_code == 200

    single_after = _header_int(single, "X-Triangles-After")
    multi_after = _header_int(multi, "X-Triangles-After")
    before = _header_int(multi, "X-Triangles-Before")

    assert single_after < before
    assert multi_after < single_after


def test_glb_input_produces_loadable_glb_output():
    mesh = _table_mesh_with_uv()
    mesh_data = _export_glb(mesh)

    response = _post_erase(
        mesh_data,
        {
            "boxes": [
                {
                    "center": [100.0, 100.0, 100.0],
                    "rotation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                    "size": [1.0, 1.0, 1.0],
                }
            ],
            "space": "mesh_local",
            "remove_rule": "intersects",
            "weld_vertices": True,
            "recalc_normals": False,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"

    loaded = trimesh.load(io.BytesIO(response.content), file_type="glb", force="mesh")
    assert isinstance(loaded, trimesh.Trimesh)
    assert len(loaded.faces) > 0
