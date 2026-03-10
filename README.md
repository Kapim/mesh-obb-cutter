# Mesh OBB Cutter Server

FastAPI server implementing the scene-based mesh workflow from `ASSIGNMENT.md`.

The server binds a catalog mesh asset to a `scene_id`, stores immutable `original` and mutable `current` copies on disk, and rebuilds `current` from `original` plus the full snapshot of collision boxes sent by the client.

The current MVP contract is built around `OBJ + MTL + texture` so the Unity client can load:

- `mesh.obj`
- `mesh.mtl`
- referenced diffuse texture via `map_Kd`

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Data Layout

Default data root is `./data` and contains:

```text
data/
  catalog/
    meshes.json
  scenes/
    <scene_id>/
      metadata.json
      original/
      current/
```

Catalog entries in `data/catalog/meshes.json` are expected to look like:

```json
{
  "items": [
    {
      "mesh_id": "scan-room-a-raw",
      "label": "Room A raw",
      "format": "obj",
      "entry_file": "mesh.obj",
      "asset_root_path": "C:/path/to/catalog_assets/scan-room-a-raw"
    }
  ]
}
```

You can override the data root with `MESH_SERVER_DATA_ROOT`.

## Endpoints

- `GET /health`
- `GET /v1/meshes`
- `GET /v1/scenes/{scene_id}/binding`
- `POST /v1/scenes/{scene_id}/bind-mesh`
- `GET /v1/scenes/{scene_id}/assets/current/{asset_path}`
- `POST /v1/scenes/{scene_id}/rebuild-from-boxes`
- `POST /v1/scenes/{scene_id}/reset-to-original`

## Rebuild Semantics

- Rebuild always loads `original`, never edits `current` incrementally.
- `boxes` is the full current snapshot from the client.
- Empty `boxes` restores `current` to the same shape as `original`.
- `base_revision` is checked for optimistic concurrency.
- `ETag` is updated whenever `current` changes.

Example rebuild request:

```json
{
  "base_revision": 7,
  "boxes": [
    {
      "id": "box_1",
      "center": [0.0, 0.75, 1.2],
      "rotation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
      "size": [1.2, 1.0, 0.8]
    }
  ],
  "space": "mesh_local",
  "remove_rule": "intersects",
  "weld_vertices": true,
  "recalc_normals": false
}
```

## Notes

- The server preserves `mtllib` in `mesh.obj`.
- The server preserves `map_Kd` references in `mesh.mtl`.
- Texture files are copied through unchanged in the MVP.
- Acceptance coverage lives in [tests/test_acceptance.py](/d:/projects/mesh-obb-cutter/tests/test_acceptance.py).
