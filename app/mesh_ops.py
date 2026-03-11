from __future__ import annotations

import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import trimesh

logger = logging.getLogger("mesh_obb_cutter.mesh_ops")


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


@dataclass(frozen=True)
class ObjAssetInfo:
    mtllib: str | None
    material_name: str | None
    texture_refs: tuple[str, ...]


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
    v0 = tri[0]
    v1 = tri[1]
    v2 = tri[2]

    e0 = v1 - v0
    e1 = v2 - v1
    e2 = v0 - v2

    basis = np.eye(3)
    for edge in (e0, e1, e2):
        for b in basis:
            axis = np.cross(edge, b)
            if not _axis_overlaps(tri, axis, half_size):
                return False

    tri_min = np.min(tri, axis=0)
    tri_max = np.max(tri, axis=0)
    if np.any(tri_min > half_size) or np.any(tri_max < -half_size):
        return False

    normal = np.cross(e0, e1)
    if not _axis_overlaps(tri, normal, half_size):
        return False

    return True


def _boxes_triangle_mask(
    mesh: trimesh.Trimesh,
    boxes: Iterable[BoxSpec],
    progress_callback: Callable[[str], None] | None = None,
    progress_interval_seconds: float = 5.0,
) -> np.ndarray:
    triangles = mesh.triangles
    boxes = list(boxes)
    remove_mask = np.zeros(len(triangles), dtype=bool)
    total_boxes = len(boxes)
    total_triangles = len(triangles)
    total_checks = max(total_boxes * total_triangles, 1)
    checks_done = 0
    last_progress_at = time.monotonic()

    def emit_progress(message: str) -> None:
        if progress_callback is not None:
            progress_callback(message)
            return
        logger.info(message)

    emit_progress(
        "Cutting started: %d boxes across %d triangles (%d planned box-triangle checks)"
        % (total_boxes, total_triangles, total_checks)
    )

    for box_index, box in enumerate(boxes, start=1):
        transform = _box_transform(box)
        inv_transform = np.linalg.inv(transform)
        half_size = box.size / 2.0

        tri_h = np.concatenate(
            [triangles.reshape(-1, 3), np.ones((len(triangles) * 3, 1), dtype=np.float64)],
            axis=1,
        )
        local = (inv_transform @ tri_h.T).T[:, :3].reshape(-1, 3, 3)

        pending_indices = np.where(~remove_mask)[0]
        if len(pending_indices) == 0:
            emit_progress(
                "Cutting finished early after box %d/%d: all triangles already marked for removal"
                % (box_index, total_boxes)
            )
            break

        for pending_index, idx in enumerate(pending_indices, start=1):
            if _triangle_box_intersect_local(local[idx], half_size):
                remove_mask[idx] = True
            checks_done += 1

            now = time.monotonic()
            if now - last_progress_at >= progress_interval_seconds:
                percent = min(99.0, 100.0 * checks_done / total_checks)
                emit_progress(
                    "Cutting progress: %.1f%% (%d/%d checks), box %d/%d, box-local triangle %d/%d, removed %d triangles"
                    % (
                        percent,
                        checks_done,
                        total_checks,
                        box_index,
                        total_boxes,
                        pending_index,
                        len(pending_indices),
                        int(np.count_nonzero(remove_mask)),
                    )
                )
                last_progress_at = now

        emit_progress(
            "Completed box %d/%d: removed %d/%d triangles so far"
            % (box_index, total_boxes, int(np.count_nonzero(remove_mask)), total_triangles)
        )

    emit_progress(
        "Cutting finished: removed %d/%d triangles"
        % (int(np.count_nonzero(remove_mask)), total_triangles)
    )

    return remove_mask


def erase_boxes(
    mesh: trimesh.Trimesh,
    boxes: list[BoxSpec],
    remove_rule: str = "intersects",
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[trimesh.Trimesh, MeshStats, bool]:
    if remove_rule != "intersects":
        raise ValueError(f"Unsupported remove_rule: {remove_rule}")

    vertices_before = int(len(mesh.vertices))
    triangles_before = int(len(mesh.faces))

    if triangles_before == 0:
        stats = MeshStats(vertices_before, vertices_before, triangles_before, triangles_before)
        return mesh.copy(), stats, False

    remove_mask = _boxes_triangle_mask(mesh, boxes, progress_callback=progress_callback)
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


def _parse_obj_asset_info(obj_path: Path) -> ObjAssetInfo:
    mtllib = None
    material_name = None
    for line in obj_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("mtllib ") and mtllib is None:
            mtllib = stripped.split(None, 1)[1]
        elif stripped.startswith("usemtl ") and material_name is None:
            material_name = stripped.split(None, 1)[1]
    texture_refs: list[str] = []
    if mtllib:
        mtl_path = (obj_path.parent / mtllib).resolve()
        if mtl_path.exists():
            for line in mtl_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("map_Kd "):
                    texture_refs.append(stripped.split(None, 1)[1])
    return ObjAssetInfo(mtllib=mtllib, material_name=material_name, texture_refs=tuple(texture_refs))


def load_mesh_from_obj(obj_path: Path) -> tuple[trimesh.Trimesh, ObjAssetInfo]:
    loaded = trimesh.load(str(obj_path), file_type="obj", force="mesh")
    if not isinstance(loaded, trimesh.Trimesh):
        raise ValueError("OBJ did not contain a valid mesh")
    loaded.process(validate=False)
    return loaded, _parse_obj_asset_info(obj_path)


def _copy_asset_dependencies(original_root: Path, current_root: Path, entry_file: str) -> None:
    _safe_delete(current_root)
    shutil.copytree(original_root, current_root)
    target_obj = current_root / entry_file
    if not target_obj.exists():
        raise ValueError("Entry OBJ is missing from copied asset")


def _safe_delete(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _format_vertex(values: np.ndarray) -> str:
    return " ".join(f"{float(v):.8f}".rstrip("0").rstrip(".") if abs(float(v)) >= 1e-8 else "0" for v in values)


def export_obj_asset(
    mesh: trimesh.Trimesh,
    obj_info: ObjAssetInfo,
    original_root: Path,
    current_root: Path,
    entry_file: str,
) -> None:
    _copy_asset_dependencies(original_root, current_root, entry_file)
    obj_out = current_root / entry_file

    uv = getattr(mesh.visual, "uv", None)
    normals = None
    try:
        normals = mesh.vertex_normals
    except Exception:
        normals = None

    lines: list[str] = []
    if obj_info.mtllib:
        lines.append(f"mtllib {obj_info.mtllib}")
    if obj_info.material_name:
        lines.append(f"usemtl {obj_info.material_name}")

    for vertex in np.asarray(mesh.vertices):
        lines.append(f"v {_format_vertex(vertex)}")

    if uv is not None:
        for tex in np.asarray(uv):
            lines.append(f"vt {_format_vertex(np.asarray(tex)[:2])}")

    if normals is not None and len(normals) == len(mesh.vertices):
        for normal in np.asarray(normals):
            lines.append(f"vn {_format_vertex(normal)}")

    has_uv = uv is not None and len(uv) == len(mesh.vertices)
    has_normals = normals is not None and len(normals) == len(mesh.vertices)
    for face in np.asarray(mesh.faces):
        indices = face + 1
        parts: list[str] = []
        for index in indices:
            if has_uv and has_normals:
                parts.append(f"{index}/{index}/{index}")
            elif has_uv:
                parts.append(f"{index}/{index}")
            elif has_normals:
                parts.append(f"{index}//{index}")
            else:
                parts.append(str(index))
        lines.append(f"f {' '.join(parts)}")

    obj_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
