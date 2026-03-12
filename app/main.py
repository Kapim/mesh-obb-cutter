from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.mesh_ops import BoxSpec, erase_boxes, export_obj_asset, load_mesh_from_obj

logger = logging.getLogger("mesh_obb_cutter")
_LOG_LEVEL = os.getenv("MESH_CUTTER_LOG_LEVEL", "INFO").upper()
logger.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _copy_tree(src: Path, dst: Path) -> None:
    _safe_rmtree(dst)
    shutil.copytree(src, dst)


def _etag(scene_id: str, revision: int) -> str:
    return f"\"{scene_id}-r{revision}\""


def _scene_download_url(scene_id: str, entry_file: str) -> str:
    return f"/v1/scenes/{scene_id}/assets/current/{entry_file}"


@dataclass
class SceneMetadata:
    scene_id: str
    source_mesh_id: str
    current_revision: int
    original_asset_path: str
    current_asset_path: str
    entry_file: str
    etag: str
    updated_at: str
    relative_position: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    relative_rotation_quat_xyzw: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 1.0])


class BoxRequest(BaseModel):
    id: str | None = None
    center: list[float] = Field(min_length=3, max_length=3)
    rotation_quat_xyzw: list[float] = Field(min_length=4, max_length=4)
    size: list[float] = Field(min_length=3, max_length=3)


class BindMeshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mesh_id: str


class RebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int
    boxes: list[BoxRequest] = Field(default_factory=list)
    space: str = "mesh_local"
    remove_rule: str = "intersects"
    weld_vertices: bool = True
    recalc_normals: bool = False


class MeshTransformRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_position: list[float] = Field(min_length=3, max_length=3)
    relative_rotation_quat_xyzw: list[float] = Field(min_length=4, max_length=4)


def _binding_payload(meta: SceneMetadata) -> dict:
    return {
        "scene_id": meta.scene_id,
        "bound": True,
        "source_mesh_id": meta.source_mesh_id,
        "current_revision": meta.current_revision,
        "etag": meta.etag,
        "download_url": _scene_download_url(meta.scene_id, meta.entry_file),
        "relative_position": meta.relative_position,
        "relative_rotation_quat_xyzw": meta.relative_rotation_quat_xyzw,
    }


def _mesh_transform_payload(meta: SceneMetadata) -> dict:
    return {
        "scene_id": meta.scene_id,
        "relative_position": meta.relative_position,
        "relative_rotation_quat_xyzw": meta.relative_rotation_quat_xyzw,
        "updated_at": meta.updated_at,
    }


class DataStore:
    def __init__(self, data_root: Path):
        self.data_root = data_root
        self.catalog_dir = data_root / "catalog"
        self.scenes_dir = data_root / "scenes"
        self.catalog_file = self.catalog_dir / "meshes.json"
        self.catalog_dir.mkdir(parents=True, exist_ok=True)
        self.scenes_dir.mkdir(parents=True, exist_ok=True)

    def load_catalog(self) -> dict:
        if not self.catalog_file.exists():
            return {"items": []}
        return json.loads(self.catalog_file.read_text(encoding="utf-8"))

    def get_mesh(self, mesh_id: str) -> dict:
        catalog = self.load_catalog()
        for item in catalog.get("items", []):
            if item["mesh_id"] == mesh_id:
                return item
        raise HTTPException(status_code=404, detail="Mesh not found")

    def scene_dir(self, scene_id: str) -> Path:
        return self.scenes_dir / scene_id

    def metadata_path(self, scene_id: str) -> Path:
        return self.scene_dir(scene_id) / "metadata.json"

    def load_scene(self, scene_id: str) -> SceneMetadata | None:
        path = self.metadata_path(scene_id)
        if not path.exists():
            return None
        return SceneMetadata(**json.loads(path.read_text(encoding="utf-8")))

    def save_scene(self, meta: SceneMetadata) -> None:
        scene_dir = self.scene_dir(meta.scene_id)
        scene_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_path(meta.scene_id).write_text(json.dumps(asdict(meta), indent=2), encoding="utf-8")


def create_app(data_root: Path | None = None) -> FastAPI:
    root = data_root or Path(os.getenv("MESH_SERVER_DATA_ROOT", "data"))
    store = DataStore(root)
    app = FastAPI(title="Mesh OBB Cutter", version="2.0.0")
    app.state.store = store

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        started = time.monotonic()
        logger.info("HTTP %s %s started", request.method, request.url.path)
        try:
            response = await call_next(request)
        except Exception:
            duration = time.monotonic() - started
            logger.exception(
                "HTTP %s %s failed after %.2fs", request.method, request.url.path, duration
            )
            raise

        duration = time.monotonic() - started
        logger.info(
            "HTTP %s %s completed with %d in %.2fs",
            request.method,
            request.url.path,
            response.status_code,
            duration,
        )
        return response

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/v1/meshes")
    def list_meshes() -> JSONResponse:
        return JSONResponse(store.load_catalog())

    @app.get("/v1/scenes/{scene_id}/binding")
    def get_binding(scene_id: str) -> JSONResponse:
        meta = store.load_scene(scene_id)
        if meta is None:
            return JSONResponse({"scene_id": scene_id, "bound": False})
        return JSONResponse(_binding_payload(meta))

    @app.post("/v1/scenes/{scene_id}/bind-mesh")
    def bind_mesh(scene_id: str, request: BindMeshRequest) -> JSONResponse:
        mesh = store.get_mesh(request.mesh_id)
        asset_root = Path(mesh["asset_root_path"])
        if not asset_root.exists():
            raise HTTPException(status_code=500, detail="Catalog asset root is missing")

        scene_dir = store.scene_dir(scene_id)
        original_dir = scene_dir / "original"
        current_dir = scene_dir / "current"
        scene_dir.mkdir(parents=True, exist_ok=True)
        _copy_tree(asset_root, original_dir)
        _copy_tree(asset_root, current_dir)

        revision = 1
        meta = SceneMetadata(
            scene_id=scene_id,
            source_mesh_id=request.mesh_id,
            current_revision=revision,
            original_asset_path=str(original_dir),
            current_asset_path=str(current_dir),
            entry_file=mesh["entry_file"],
            relative_position=[0.0, 0.0, 0.0],
            relative_rotation_quat_xyzw=[0.0, 0.0, 0.0, 1.0],
            etag=_etag(scene_id, revision),
            updated_at=_utc_now(),
        )
        store.save_scene(meta)
        return JSONResponse(_binding_payload(meta))

    @app.get("/v1/scenes/{scene_id}/mesh-transform")
    def get_mesh_transform(scene_id: str) -> JSONResponse:
        meta = store.load_scene(scene_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Scene is not bound")
        return JSONResponse(_mesh_transform_payload(meta))

    @app.put("/v1/scenes/{scene_id}/mesh-transform")
    def update_mesh_transform(scene_id: str, request: MeshTransformRequest) -> JSONResponse:
        meta = store.load_scene(scene_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Scene is not bound")

        meta.relative_position = [float(value) for value in request.relative_position]
        meta.relative_rotation_quat_xyzw = [
            float(value) for value in request.relative_rotation_quat_xyzw
        ]
        meta.updated_at = _utc_now()
        store.save_scene(meta)
        return JSONResponse(_mesh_transform_payload(meta))

    @app.get("/v1/scenes/{scene_id}/mesh-position")
    def get_mesh_position(scene_id: str) -> JSONResponse:
        return get_mesh_transform(scene_id)

    @app.put("/v1/scenes/{scene_id}/mesh-position")
    def update_mesh_position(scene_id: str, request: MeshTransformRequest) -> JSONResponse:
        return update_mesh_transform(scene_id, request)

    @app.get("/v1/scenes/{scene_id}/assets/current/{asset_path:path}")
    def get_current_asset(scene_id: str, asset_path: str, request: Request) -> Response:
        meta = store.load_scene(scene_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Scene is not bound")

        current_root = Path(meta.current_asset_path).resolve()
        resolved = (current_root / asset_path).resolve()
        if current_root not in resolved.parents and resolved != current_root:
            raise HTTPException(status_code=404, detail="Asset not found")
        if not resolved.exists() or not resolved.is_file():
            raise HTTPException(status_code=404, detail="Asset not found")

        if request.headers.get("if-none-match") == meta.etag:
            return Response(status_code=304, headers={"ETag": meta.etag})

        media_type, _ = mimetypes.guess_type(str(resolved))
        return FileResponse(
            resolved,
            media_type=media_type or "application/octet-stream",
            headers={"ETag": meta.etag},
        )

    @app.post("/v1/scenes/{scene_id}/rebuild-from-boxes")
    def rebuild_from_boxes(scene_id: str, request: RebuildRequest) -> JSONResponse:
        started = time.monotonic()
        logger.info(
            "Rebuild requested for scene=%s base_revision=%d boxes=%d weld_vertices=%s recalc_normals=%s remove_rule=%s",
            scene_id,
            request.base_revision,
            len(request.boxes),
            request.weld_vertices,
            request.recalc_normals,
            request.remove_rule,
        )
        meta = store.load_scene(scene_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Scene is not bound")
        if request.space != "mesh_local":
            raise HTTPException(status_code=422, detail="Only space='mesh_local' is supported")
        if request.base_revision != meta.current_revision:
            return JSONResponse(
                status_code=409,
                content={"detail": "Revision conflict", "current_revision": meta.current_revision},
            )

        original_root = Path(meta.original_asset_path)
        original_obj = original_root / meta.entry_file
        if not original_obj.exists():
            raise HTTPException(status_code=500, detail="Original entry file is missing")

        next_revision = meta.current_revision + 1
        current_root = Path(meta.current_asset_path)

        if not request.boxes:
            logger.info("Rebuild for scene=%s has no boxes, restoring original asset", scene_id)
            _copy_tree(original_root, current_root)
        else:
            logger.info("Loading source OBJ for scene=%s from %s", scene_id, original_obj)
            mesh, obj_info = load_mesh_from_obj(original_obj)
            logger.info(
                "Loaded mesh for scene=%s: vertices=%d triangles=%d",
                scene_id,
                len(mesh.vertices),
                len(mesh.faces),
            )
            boxes = [
                BoxSpec(
                    center=np.array(box.center, dtype=np.float64),
                    rotation_quat_xyzw=np.array(box.rotation_quat_xyzw, dtype=np.float64),
                    size=np.array(box.size, dtype=np.float64),
                )
                for box in request.boxes
            ]

            try:
                result_mesh, stats, _ = erase_boxes(
                    mesh,
                    boxes,
                    remove_rule=request.remove_rule,
                    progress_callback=lambda message: logger.info("scene=%s %s", scene_id, message),
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            logger.info(
                "Cut completed for scene=%s: triangles %d -> %d, vertices %d -> %d",
                scene_id,
                stats.triangles_before,
                stats.triangles_after,
                stats.vertices_before,
                stats.vertices_after,
            )
            if stats.triangles_after == 0:
                raise HTTPException(status_code=422, detail="Mesh processing produced an empty result")

            if request.weld_vertices:
                logger.info("Welding vertices for scene=%s", scene_id)
                result_mesh.merge_vertices()
                result_mesh.remove_unreferenced_vertices()
            if request.recalc_normals:
                logger.info("Recalculating normals for scene=%s", scene_id)
                _ = result_mesh.vertex_normals

            logger.info("Exporting rebuilt OBJ asset for scene=%s", scene_id)
            export_obj_asset(result_mesh, obj_info, original_root, current_root, meta.entry_file)

        meta.current_revision = next_revision
        meta.etag = _etag(scene_id, next_revision)
        meta.updated_at = _utc_now()
        store.save_scene(meta)
        logger.info(
            "Rebuild finished for scene=%s new_revision=%d in %.2fs",
            scene_id,
            meta.current_revision,
            time.monotonic() - started,
        )

        return JSONResponse(
            {
                "scene_id": scene_id,
                "new_revision": meta.current_revision,
                "etag": meta.etag,
                "download_url": _scene_download_url(scene_id, meta.entry_file),
            }
        )

    @app.post("/v1/scenes/{scene_id}/reset-to-original")
    def reset_to_original(scene_id: str) -> JSONResponse:
        meta = store.load_scene(scene_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Scene is not bound")

        original_root = Path(meta.original_asset_path)
        current_root = Path(meta.current_asset_path)
        _copy_tree(original_root, current_root)

        meta.current_revision += 1
        meta.etag = _etag(scene_id, meta.current_revision)
        meta.updated_at = _utc_now()
        store.save_scene(meta)
        return JSONResponse(
            {
                "scene_id": scene_id,
                "new_revision": meta.current_revision,
                "etag": meta.etag,
                "download_url": _scene_download_url(scene_id, meta.entry_file),
            }
        )

    return app


app = create_app()
