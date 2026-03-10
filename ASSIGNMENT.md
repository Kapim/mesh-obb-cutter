# Mesh Server Spec For Diminished Reality

## Goal

Implement a mesh server for diminished reality where:

- the space identity is `scene_id` from the AR server, for example `scn_d404ae3849904be0881175d753d29d85`
- for each `scene_id`, the server stores:
  - `original` mesh
  - `current` mesh
  - `current_revision`
- `current` is never edited incrementally
- every new version is always rebuilt from `original` and the full current set of collision boxes
- collision boxes are not stored on this server as the source of truth; they arrive only as a snapshot in the rebuild request
- the client caches mesh assets using `revision` and `ETag`

## Asset Format

The server must treat a mesh as a full asset, not as a single geometry file.

For the current Unity client, the primary supported runtime format is:

- `OBJ + MTL + texture image`

Typical asset contents:

- `mesh.obj`
- `mesh.mtl`
- `texture.jpg` or `texture.png`

Important:

- the current Unity runtime loader in this project reliably supports `OBJ`
- it also loads `MTL` and the diffuse texture referenced by `map_Kd`
- `GLB/GLTF/FBX` are not currently implemented as reliable runtime imports in the headset build
- therefore the MVP server contract should be built around `OBJ + MTL + texture`

## Texture Handling

When the server cuts geometry from an `OBJ`, it usually does not need to modify the texture image itself.

Expected behavior:

- remove triangles from the mesh
- preserve UVs for the remaining triangles
- keep using the same `MTL`
- keep using the same `JPG/PNG`

This means:

- geometry changes
- the texture file usually stays unchanged
- the material mapping remains valid for the remaining mesh

The server should not attempt texture rebaking or UV atlas regeneration in the MVP.

The texture would only need regeneration if the implementation were doing things such as:

- UV remapping
- texture atlas rebuild
- hole capping with new textured surfaces

That is explicitly out of scope for the MVP.

## Important Constraints

- the AR server data model cannot be changed
- collision boxes cannot be deleted
- the user can add, delete, and move boxes during edit mode
- only when edit mode is turned off does the client send all current boxes together and the server rebuilds `current`

This is critical because it automatically restores geometry that was removed earlier if the box layout changes later.

## Required Behavior

1. When a scene is opened, the client knows `scene_id`.
2. The client checks whether a mesh is already bound to this `scene_id` on the mesh server.
3. If not:
   - the client requests the list of available meshes
   - the user selects one
   - the client binds it to the scene
4. If yes:
   - the client downloads `current` or uses the local cache if `ETag` matches
5. When edit mode is turned off:
   - the client sends the full current list of collision boxes
   - the server takes `original`
   - applies all boxes
   - creates a new `current`
   - increments `revision`
- the client downloads the new `current` asset

## Recommended Stack

- Python
- FastAPI
- Uvicorn
- `trimesh`
- `Pillow` if needed for textures
- MVP asset format: `OBJ + MTL + texture`
- if `GLB` support already exists and is stable on the server, it may remain internally, but the contract for the current Unity client must work reliably for `OBJ + MTL + texture`

## Server Data Model

For each `scene_id`, maintain:

```json
{
  "scene_id": "scn_d404ae3849904be0881175d753d29d85",
  "source_mesh_id": "scan-room-a-raw",
  "current_revision": 7,
  "original_asset_path": "...",
  "current_asset_path": "...",
  "etag": "\"scn_d404ae3849904be0881175d753d29d85-r7\"",
  "updated_at": "2026-03-10T12:00:00Z"
}
```

Also maintain a catalog of available meshes:

```json
{
  "mesh_id": "scan-room-a-raw",
  "label": "Room A raw",
  "format": "obj",
  "asset_root_path": "..."
}
```

Implementation can be:

- a simple JSON metadata store plus files on disk
- no database is required unless clearly needed

## Disk Layout

Recommended structure:

```text
data/
  catalog/
    meshes.json
  scenes/
    scn_d404ae3849904be0881175d753d29d85/
      metadata.json
      original/
        mesh.obj
        mesh.mtl
        textures/...
      current/
        mesh.obj
        mesh.mtl
        textures/...
```

Requirements:

- `original` must remain immutable after binding
- `current` is replaced on every rebuild
- `revision` increases monotonically
- the server must preserve relative links between `mesh.obj`, `mesh.mtl`, and texture files

## Endpoints

### 1. `GET /v1/scenes/{scene_id}/binding`

Returns whether a mesh is bound to the scene.

Response when bound:

```json
{
  "scene_id": "scn_d404ae3849904be0881175d753d29d85",
  "bound": true,
  "source_mesh_id": "scan-room-a-raw",
  "current_revision": 7,
  "etag": "\"scn_d404ae3849904be0881175d753d29d85-r7\"",
  "download_url": "/v1/scenes/scn_d404ae3849904be0881175d753d29d85/assets/current/mesh.obj"
}
```

Response when not bound:

```json
{
  "scene_id": "scn_d404ae3849904be0881175d753d29d85",
  "bound": false
}
```

### 2. `GET /v1/meshes`

Returns the list of available meshes for first-time binding.

Response:

```json
{
  "items": [
    {
      "mesh_id": "scan-room-a-raw",
      "label": "Room A raw",
      "format": "obj",
      "entry_file": "mesh.obj"
    },
    {
      "mesh_id": "scan-room-a-clean",
      "label": "Room A clean",
      "format": "obj",
      "entry_file": "mesh.obj"
    }
  ]
}
```

### 3. `POST /v1/scenes/{scene_id}/bind-mesh`

Request:

```json
{
  "mesh_id": "scan-room-a-raw"
}
```

Server must:

- verify that `mesh_id` exists in the catalog
- create the scene record
- copy the selected mesh asset as `original`
- copy the same mesh asset as the initial `current`
- set `revision = 1`
- set `etag`

Response:

```json
{
  "scene_id": "scn_d404ae3849904be0881175d753d29d85",
  "bound": true,
  "source_mesh_id": "scan-room-a-raw",
  "current_revision": 1,
  "etag": "\"scn_d404ae3849904be0881175d753d29d85-r1\"",
  "download_url": "/v1/scenes/scn_d404ae3849904be0881175d753d29d85/assets/current/mesh.obj"
}
```

### 4. `GET /v1/scenes/{scene_id}/assets/current/{asset_path}`

Returns a file from the current mesh asset for the scene.

Examples:

- `/v1/scenes/{scene_id}/assets/current/mesh.obj`
- `/v1/scenes/{scene_id}/assets/current/mesh.mtl`
- `/v1/scenes/{scene_id}/assets/current/texture.jpg`

Requirements:

- return the correct content type for the format
- return `ETag`
- support `If-None-Match`
- if `ETag` matches, return `304 Not Modified`
- resolve files relative to the current asset root only
- do not allow path traversal outside the current asset directory

Rationale:

- the Unity client loads `mesh.obj`
- the OBJ references `mesh.mtl`
- the MTL references `texture.jpg` or `texture.png`
- therefore the server must expose the whole current asset set with stable relative paths

### 5. `POST /v1/scenes/{scene_id}/rebuild-from-boxes`

This is the key endpoint.

Request:

```json
{
  "base_revision": 7,
  "boxes": [
    {
      "id": "box_1",
      "center": [0.0, 0.75, 1.2],
      "rotation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
      "size": [1.2, 1.0, 0.8]
    },
    {
      "id": "box_2",
      "center": [2.0, 0.4, -0.6],
      "rotation_quat_xyzw": [0.0, 0.707, 0.0, 0.707],
      "size": [0.8, 0.8, 0.8]
    }
  ],
  "space": "mesh_local",
  "remove_rule": "intersects",
  "weld_vertices": true,
  "recalc_normals": false
}
```

Rules:

- `boxes` is always the complete current snapshot of all collision boxes relevant for diminished reality
- computation is always:
  - load `original`
  - apply all boxes
  - store the result as new `current`
- if `boxes` is an empty array, resulting `current` must become identical to `original`
- `space` will be `mesh_local` in the MVP
- default `remove_rule` is `intersects`

Response:

```json
{
  "scene_id": "scn_d404ae3849904be0881175d753d29d85",
  "new_revision": 8,
  "etag": "\"scn_d404ae3849904be0881175d753d29d85-r8\"",
  "download_url": "/v1/scenes/scn_d404ae3849904be0881175d753d29d85/assets/current/mesh.obj"
}
```

### 6. `POST /v1/scenes/{scene_id}/reset-to-original`

Optional, but should be implemented.

Behavior:

- `current = original`
- increment `revision`
- return the new `etag`

## Mesh Processing Rules

During `rebuild-from-boxes`, the server must:

1. Load `original` mesh.
2. For each box, construct an OBB in `mesh_local`.
3. Mark triangles for removal using `remove_rule = intersects`.
4. Apply all boxes against `original`.
5. Remove unused vertices and reindex the mesh.
6. Preserve UVs for the remaining mesh.
7. Keep `MTL` and texture files unless there is a strong technical reason to rewrite them.
8. Export the new `current` asset.

Important:

- no incremental cutting from `current`
- no server-side box persistence as authoritative state
- no hole capping in MVP
- if no box hits the mesh, `current` may end up identical to `original`, but `revision` may still increase if the rebuild request succeeded
- if the result would be empty or invalid, return `422`
- the server does not need to regenerate `JPG/PNG` texture files in the MVP
- the server should preserve the relative `mtllib` reference in `mesh.obj`
- the server should preserve the relative `map_Kd` reference in `mesh.mtl`

## Conflicts

Implement optimistic concurrency using `base_revision`.

Rule:

- if `base_revision` does not match the current `current_revision`, return `409 Conflict`

Response:

```json
{
  "detail": "Revision conflict",
  "current_revision": 8
}
```

The client is then expected to:

- reload binding/current state
- resend rebuild if appropriate

## Cache Contract

The client will use:

- `current_revision`
- `ETag`

The server must:

- update `revision` whenever `current` changes
- update `ETag` whenever `current` changes

Recommended format:

```text
"scn_<scene_id>-r<revision>"
```

## Error Handling

Use clear HTTP status codes:

- `404` if `scene_id` is not bound and `current` is requested
- `404` if a requested asset file under `current` does not exist
- `404` if `mesh_id` does not exist during bind
- `409` for `base_revision` conflict
- `422` for invalid mesh processing result
- `500` only for real internal server errors

Keep error responses simple:

```json
{
  "detail": "Human readable message"
}
```

## Acceptance Scenarios

1. Scene without binding:
   - `GET binding` returns `bound: false`
   - `GET /v1/meshes` returns available meshes
   - `POST bind-mesh` creates `original/current/revision=1`

2. Scene with binding:
   - `GET binding` returns `bound: true`
   - `GET /assets/current/mesh.obj` returns OBJ plus `ETag`
   - the OBJ can successfully resolve its `MTL`
   - the MTL can successfully resolve its texture

3. Rebuild with one box:
   - `POST rebuild-from-boxes` creates a new `current`
   - `revision` increments
   - downloaded mesh differs from the original

4. Rebuild with empty box list:
   - `current` returns to the same shape as `original`

5. Box move:
   - two consecutive rebuilds with different box poses produce different `current` meshes

6. Box removal:
   - rebuild with fewer boxes restores geometry that had been cut before, because the result is always recomputed from `original`

7. Conflict:
   - rebuild with stale `base_revision` returns `409`

8. Cache:
   - `GET /assets/current/mesh.obj` with matching `If-None-Match` returns `304`

## Implementation Note

The goal is not a general-purpose version control system. The goal is:

- stable binding `scene_id -> original/current`
- rebuild from `original`
- simple cache invalidation
- clean client flow on scene open and on edit mode exit
- support for runtime loading in the existing Unity client, which currently expects `OBJ + MTL + texture`

If needed, a separate client-side Unity spec can be written next:

1. how to call `binding`
2. how to show the mesh picker when `bound = false`
3. how to collect all collision boxes on edit mode exit and call `rebuild-from-boxes`
