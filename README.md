# Mesh OBB Cutter Server

FastAPI server implementing `POST /v1/mesh/erase-box` and `GET /health` based on `ASSIGNMENT.md`.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Endpoint

- `GET /health` returns `{"ok": true}`.
- `POST /v1/mesh/erase-box` accepts `multipart/form-data` fields:
- `mesh_file`: `.glb`, `.gltf`, `.obj`, or `.zip` containing `.obj`
- `request`: JSON string with boxes and options

The server removes triangles intersecting requested OBBs (`remove_rule="intersects"`) and returns a GLB payload (`application/octet-stream`) with stats headers:

- `X-Vertices-Before`
- `X-Vertices-After`
- `X-Triangles-Before`
- `X-Triangles-After`

## Example Request Payload

```json
{
  "boxes": [
    {
      "center": [0.0, 0.75, 1.2],
      "rotation_quat_xyzw": [0, 0, 0, 1],
      "size": [1.2, 1.0, 0.8]
    }
  ],
  "space": "mesh_local",
  "remove_rule": "intersects",
  "weld_vertices": true,
  "recalc_normals": false
}
```
