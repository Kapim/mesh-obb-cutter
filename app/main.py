from __future__ import annotations

import json

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.mesh_ops import BoxSpec, erase_boxes, export_glb, load_mesh_from_bytes

app = FastAPI(title="Mesh OBB Erase Server", version="1.0.0")


class BoxRequest(BaseModel):
    center: list[float] = Field(min_length=3, max_length=3)
    rotation_quat_xyzw: list[float] = Field(min_length=4, max_length=4)
    size: list[float] = Field(min_length=3, max_length=3)


class EraseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boxes: list[BoxRequest] = Field(min_length=1)
    space: str = "mesh_local"
    remove_rule: str = "intersects"
    weld_vertices: bool = True
    recalc_normals: bool = False


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.post("/v1/mesh/erase-box")
async def erase_box_endpoint(mesh_file: UploadFile = File(...), request: str = Form(...)) -> Response:
    try:
        request_obj = EraseRequest.model_validate_json(request)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc

    if request_obj.space != "mesh_local":
        raise HTTPException(status_code=422, detail="Only space='mesh_local' is supported")

    payload = await mesh_file.read()
    if not payload:
        raise HTTPException(status_code=422, detail="mesh_file is empty")

    try:
        mesh = load_mesh_from_bytes(payload, mesh_file.filename or "upload.glb")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Failed to load mesh: {exc}") from exc

    boxes = [
        BoxSpec(
            center=np.array(box.center, dtype=np.float64),
            rotation_quat_xyzw=np.array(box.rotation_quat_xyzw, dtype=np.float64),
            size=np.array(box.size, dtype=np.float64),
        )
        for box in request_obj.boxes
    ]

    try:
        result_mesh, stats, changed = erase_boxes(mesh, boxes, remove_rule=request_obj.remove_rule)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if stats.triangles_after == 0:
        raise HTTPException(status_code=422, detail="Mesh is empty after erase")

    if request_obj.weld_vertices:
        result_mesh.merge_vertices()
        result_mesh.remove_unreferenced_vertices()

    if request_obj.recalc_normals:
        _ = result_mesh.vertex_normals

    if not changed and (mesh_file.filename or "").lower().endswith(".glb"):
        output = payload
    else:
        output = export_glb(result_mesh)

    headers = {
        "X-Vertices-Before": str(stats.vertices_before),
        "X-Vertices-After": str(len(result_mesh.vertices)),
        "X-Triangles-Before": str(stats.triangles_before),
        "X-Triangles-After": str(len(result_mesh.faces)),
    }

    return Response(content=output, media_type="application/octet-stream", headers=headers)
