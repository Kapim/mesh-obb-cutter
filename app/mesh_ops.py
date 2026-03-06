from __future__ import annotations

import io
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import trimesh


@dataclass(frozen=True)
class BoxSpec:
    center: np.ndarray
    rotation_quat_xyzw: np.ndarray
    size: np.ndarray


@dataclass(frozen=True)
class MeshStats:
    vertices_before: int
    vertices_after: int
    triangles_before: int
    triangles_after: int


def _xyzw_to_wxyz(quat_xyzw: np.ndarray) -> np.ndarray:
    return np.array([quat_xyzw[3], quat_xyzw[0], quat_xyzw[1], quat_xyzw[2]], dtype=np.float64)


def _box_transform(box: BoxSpec) -> np.ndarray:
    rotation = trimesh.transformations.quaternion_matrix(_xyzw_to_wxyz(box.rotation_quat_xyzw))
    transform = rotation.copy()
    transform[:3, 3] = box.center
    return transform


def _axis_overlaps(points: np.ndarray, axis: np.ndarray, half_size: np.ndarray) -> bool:
    if np.allclose(axis, 0.0):
        return True
    p = points @ axis
    r = np.dot(np.abs(axis), half_size)
    return not (p.max() < -r or p.min() > r)


def _triangle_box_intersect_local(tri: np.ndarray, half_size: np.ndarray) -> bool:
    # SAT implementation derived from Akenine-Moller tri-box overlap test.
    v0 = tri[0]
    v1 = tri[1]
    v2 = tri[2]

    e0 = v1 - v0
    e1 = v2 - v1
    e2 = v0 - v2

    # 9 cross-product axes (triangle edges x box axes)
    basis = np.eye(3)
    for edge in (e0, e1, e2):
        for b in basis:
            axis = np.cross(edge, b)
            if not _axis_overlaps(tri, axis, half_size):
                return False

    # 3 box face axes
    tri_min = np.min(tri, axis=0)
    tri_max = np.max(tri, axis=0)
    if np.any(tri_min > half_size) or np.any(tri_max < -half_size):
        return False

    # Triangle face normal
    normal = np.cross(e0, e1)
    if not _axis_overlaps(tri, normal, half_size):
        return False

    return True


def _boxes_triangle_mask(mesh: trimesh.Trimesh, boxes: Iterable[BoxSpec]) -> np.ndarray:
    triangles = mesh.triangles
    remove_mask = np.zeros(len(triangles), dtype=bool)

    for box in boxes:
        transform = _box_transform(box)
        inv_transform = np.linalg.inv(transform)
        half_size = box.size / 2.0

        tri_h = np.concatenate(
            [triangles.reshape(-1, 3), np.ones((len(triangles) * 3, 1), dtype=np.float64)],
            axis=1,
        )
        local = (inv_transform @ tri_h.T).T[:, :3].reshape(-1, 3, 3)

        for idx in np.where(~remove_mask)[0]:
            if _triangle_box_intersect_local(local[idx], half_size):
                remove_mask[idx] = True

    return remove_mask


def erase_boxes(
    mesh: trimesh.Trimesh, boxes: list[BoxSpec], remove_rule: str = "intersects"
) -> tuple[trimesh.Trimesh, MeshStats, bool]:
    if remove_rule != "intersects":
        raise ValueError(f"Unsupported remove_rule: {remove_rule}")

    vertices_before = int(len(mesh.vertices))
    triangles_before = int(len(mesh.faces))

    if triangles_before == 0:
        stats = MeshStats(vertices_before, vertices_before, triangles_before, triangles_before)
        return mesh.copy(), stats, False

    remove_mask = _boxes_triangle_mask(mesh, boxes)
    removed_count = int(np.count_nonzero(remove_mask))

    if removed_count == 0:
        stats = MeshStats(vertices_before, vertices_before, triangles_before, triangles_before)
        return mesh.copy(), stats, False

    kept_faces = mesh.faces[~remove_mask]
    if len(kept_faces) == 0:
        stats = MeshStats(vertices_before, 0, triangles_before, 0)
        empty = trimesh.Trimesh(vertices=np.empty((0, 3)), faces=np.empty((0, 3), dtype=np.int64))
        return empty, stats, True

    new_mesh = mesh.submesh([~remove_mask], append=True, repair=False)
    if not isinstance(new_mesh, trimesh.Trimesh):
        raise RuntimeError("Submesh operation did not produce a valid mesh")

    new_mesh.remove_unreferenced_vertices()

    stats = MeshStats(
        vertices_before=vertices_before,
        vertices_after=int(len(new_mesh.vertices)),
        triangles_before=triangles_before,
        triangles_after=int(len(new_mesh.faces)),
    )
    return new_mesh, stats, True


def _resolve_scene_to_mesh(scene: trimesh.Scene) -> trimesh.Trimesh:
    if len(scene.geometry) == 0:
        return trimesh.Trimesh(vertices=np.empty((0, 3)), faces=np.empty((0, 3), dtype=np.int64))

    if len(scene.geometry) == 1:
        return next(iter(scene.geometry.values())).copy()

    merged = scene.dump(concatenate=True)
    if isinstance(merged, trimesh.Trimesh):
        return merged
    raise ValueError("Unable to resolve scene geometry")


def load_mesh_from_bytes(data: bytes, filename: str) -> trimesh.Trimesh:
    suffix = Path(filename).suffix.lower()

    if suffix in {".glb", ".gltf", ".obj"}:
        loaded = trimesh.load(io.BytesIO(data), file_type=suffix.lstrip("."), force="scene")
    elif suffix == ".zip":
        with tempfile.TemporaryDirectory() as temp_dir:
            with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
                zf.extractall(temp_dir)
            obj_candidates = list(Path(temp_dir).rglob("*.obj"))
            if not obj_candidates:
                raise ValueError("ZIP must contain at least one .obj file")
            loaded = trimesh.load(str(obj_candidates[0]), file_type="obj", force="scene")
    else:
        raise ValueError("Unsupported mesh format. Use .glb, .gltf, .obj or .zip")

    if isinstance(loaded, trimesh.Scene):
        mesh = _resolve_scene_to_mesh(loaded)
    elif isinstance(loaded, trimesh.Trimesh):
        mesh = loaded
    else:
        raise ValueError("Uploaded file did not contain a valid mesh")

    mesh.process(validate=False)
    return mesh


def export_glb(mesh: trimesh.Trimesh) -> bytes:
    exported = mesh.export(file_type="glb")
    if isinstance(exported, bytes):
        return exported
    if isinstance(exported, str):
        return exported.encode("utf-8")
    raise RuntimeError("GLB export failed")
