from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import trimesh
from fastapi.testclient import TestClient

from app.main import create_app


def _write_texture(path: Path) -> None:
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde\x00\x00\x00\x0cIDAT\x08\x99c\xf8\xff\xff?\x00\x05\xfe\x02\xfeA\x89\x18\x8f"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    path.write_bytes(png)


def _write_obj_asset(asset_root: Path, extents: tuple[float, float, float] = (2.0, 0.2, 2.0)) -> None:
    asset_root.mkdir(parents=True, exist_ok=True)
    mesh = trimesh.creation.box(extents=extents)
    vertices = mesh.vertices
    uv = np.zeros((len(vertices), 2), dtype=np.float64)

    x_min, x_max = vertices[:, 0].min(), vertices[:, 0].max()
    z_min, z_max = vertices[:, 2].min(), vertices[:, 2].max()
    uv[:, 0] = (vertices[:, 0] - x_min) / max(x_max - x_min, 1.0)
    uv[:, 1] = (vertices[:, 2] - z_min) / max(z_max - z_min, 1.0)
    mesh.visual = trimesh.visual.texture.TextureVisuals(uv=uv)

    lines = ["mtllib mesh.mtl", "usemtl material_0"]
    for vertex in np.asarray(mesh.vertices):
        lines.append(f"v {vertex[0]:.6f} {vertex[1]:.6f} {vertex[2]:.6f}")
    for tex in uv:
        lines.append(f"vt {tex[0]:.6f} {tex[1]:.6f}")
    for normal in np.asarray(mesh.vertex_normals):
        lines.append(f"vn {normal[0]:.6f} {normal[1]:.6f} {normal[2]:.6f}")
    for face in np.asarray(mesh.faces) + 1:
        a, b, c = face
        lines.append(f"f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}")

    (asset_root / "mesh.obj").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (asset_root / "mesh.mtl").write_text(
        "newmtl material_0\nKd 1.0 1.0 1.0\nmap_Kd textures/texture.png\n",
        encoding="utf-8",
    )
    texture_dir = asset_root / "textures"
    texture_dir.mkdir(exist_ok=True)
    _write_texture(texture_dir / "texture.png")


def _make_client(tmp_path: Path) -> TestClient:
    data_root = tmp_path / "data"
    asset_root = tmp_path / "catalog_assets" / "scan-room-a-raw"
    _write_obj_asset(asset_root)
    (data_root / "catalog").mkdir(parents=True, exist_ok=True)
    (data_root / "catalog" / "meshes.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "mesh_id": "scan-room-a-raw",
                        "label": "Room A raw",
                        "format": "obj",
                        "entry_file": "mesh.obj",
                        "asset_root_path": str(asset_root),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return TestClient(create_app(data_root))


def _load_obj(content: bytes) -> trimesh.Trimesh:
    loaded = trimesh.load(io.BytesIO(content), file_type="obj", force="mesh")
    assert isinstance(loaded, trimesh.Trimesh)
    return loaded


def test_scene_without_binding_lists_meshes_and_reports_unbound(tmp_path: Path):
    client = _make_client(tmp_path)
    scene_id = "scn_d404ae3849904be0881175d753d29d85"

    binding = client.get(f"/v1/scenes/{scene_id}/binding")
    meshes = client.get("/v1/meshes")

    assert binding.status_code == 200
    assert binding.json() == {"scene_id": scene_id, "bound": False}
    assert meshes.status_code == 200
    assert meshes.json()["items"][0]["mesh_id"] == "scan-room-a-raw"


def test_bind_creates_original_and_current_with_revision_one(tmp_path: Path):
    client = _make_client(tmp_path)
    scene_id = "scn_scene_bind"

    response = client.post(f"/v1/scenes/{scene_id}/bind-mesh", json={"mesh_id": "scan-room-a-raw"})

    assert response.status_code == 200
    body = response.json()
    assert body["bound"] is True
    assert body["current_revision"] == 1
    assert body["etag"] == f"\"{scene_id}-r1\""
    assert body["download_url"].endswith("/mesh.obj")
    assert body["relative_position"] == [0.0, 0.0, 0.0]
    assert body["relative_rotation_quat_xyzw"] == [0.0, 0.0, 0.0, 1.0]

    obj = client.get(body["download_url"])
    mtl = client.get(f"/v1/scenes/{scene_id}/assets/current/mesh.mtl")
    texture = client.get(f"/v1/scenes/{scene_id}/assets/current/textures/texture.png")

    assert obj.status_code == 200
    assert mtl.status_code == 200
    assert texture.status_code == 200
    assert obj.headers["etag"] == f"\"{scene_id}-r1\""
    assert "mtllib mesh.mtl" in obj.text
    assert "map_Kd textures/texture.png" in mtl.text


def test_mesh_transform_can_be_persisted_per_scene(tmp_path: Path):
    client = _make_client(tmp_path)
    scene_id = "scn_position"
    client.post(f"/v1/scenes/{scene_id}/bind-mesh", json={"mesh_id": "scan-room-a-raw"})

    update = client.put(
        f"/v1/scenes/{scene_id}/mesh-transform",
        json={
            "relative_position": [1.25, -0.5, 3.0],
            "relative_rotation_quat_xyzw": [0.0, 0.707, 0.0, 0.707],
        },
    )
    binding = client.get(f"/v1/scenes/{scene_id}/binding")
    transform = client.get(f"/v1/scenes/{scene_id}/mesh-transform")
    legacy = client.get(f"/v1/scenes/{scene_id}/mesh-position")

    assert update.status_code == 200
    assert update.json()["relative_position"] == [1.25, -0.5, 3.0]
    assert update.json()["relative_rotation_quat_xyzw"] == [0.0, 0.707, 0.0, 0.707]
    assert binding.status_code == 200
    assert binding.json()["relative_position"] == [1.25, -0.5, 3.0]
    assert binding.json()["relative_rotation_quat_xyzw"] == [0.0, 0.707, 0.0, 0.707]
    assert transform.status_code == 200
    assert transform.json()["relative_position"] == [1.25, -0.5, 3.0]
    assert transform.json()["relative_rotation_quat_xyzw"] == [0.0, 0.707, 0.0, 0.707]
    assert legacy.status_code == 200
    assert legacy.json()["relative_rotation_quat_xyzw"] == [0.0, 0.707, 0.0, 0.707]


def test_rebuild_from_original_changes_revision_and_geometry(tmp_path: Path):
    client = _make_client(tmp_path)
    scene_id = "scn_rebuild"
    bind = client.post(f"/v1/scenes/{scene_id}/bind-mesh", json={"mesh_id": "scan-room-a-raw"}).json()

    original_obj = client.get(bind["download_url"]).content
    rebuild = client.post(
        f"/v1/scenes/{scene_id}/rebuild-from-boxes",
        json={
            "base_revision": 1,
            "boxes": [
                {
                    "id": "box_1",
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

    assert rebuild.status_code == 200
    assert rebuild.json()["new_revision"] == 2
    current_obj = client.get(rebuild.json()["download_url"]).content
    original_mesh = _load_obj(original_obj)
    current_mesh = _load_obj(current_obj)
    assert len(current_mesh.faces) < len(original_mesh.faces)


def test_empty_box_list_restores_original_shape(tmp_path: Path):
    client = _make_client(tmp_path)
    scene_id = "scn_reset_by_empty"
    bind = client.post(f"/v1/scenes/{scene_id}/bind-mesh", json={"mesh_id": "scan-room-a-raw"}).json()
    original_obj = client.get(bind["download_url"]).text

    rebuild = {
        "base_revision": 1,
        "boxes": [
            {
                "id": "box_1",
                "center": [0.0, 0.0, 0.0],
                "rotation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                "size": [0.8, 0.8, 0.8],
            }
        ],
    }
    first = client.post(f"/v1/scenes/{scene_id}/rebuild-from-boxes", json=rebuild)
    second = client.post(
        f"/v1/scenes/{scene_id}/rebuild-from-boxes",
        json={"base_revision": 2, "boxes": []},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    restored_obj = client.get(second.json()["download_url"]).text
    assert restored_obj == original_obj


def test_conflict_and_if_none_match_behavior(tmp_path: Path):
    client = _make_client(tmp_path)
    scene_id = "scn_cache_conflict"
    bind = client.post(f"/v1/scenes/{scene_id}/bind-mesh", json={"mesh_id": "scan-room-a-raw"}).json()

    cached = client.get(bind["download_url"], headers={"If-None-Match": bind["etag"]})
    conflict = client.post(
        f"/v1/scenes/{scene_id}/rebuild-from-boxes",
        json={"base_revision": 99, "boxes": []},
    )

    assert cached.status_code == 304
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Revision conflict"
    assert conflict.json()["current_revision"] == 1


def test_reset_to_original_increments_revision(tmp_path: Path):
    client = _make_client(tmp_path)
    scene_id = "scn_reset_endpoint"
    client.post(f"/v1/scenes/{scene_id}/bind-mesh", json={"mesh_id": "scan-room-a-raw"})

    response = client.post(f"/v1/scenes/{scene_id}/reset-to-original")

    assert response.status_code == 200
    assert response.json()["new_revision"] == 2
