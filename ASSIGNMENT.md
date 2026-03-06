## Cíl
Server vezme texturovanou mesh + orientovaný box (OBB), odstraní geometrii uvnitř boxu a vrátí novou mesh se zachovanou texturou/UV.

### Doporučený formát

- Preferovat glb (jediný soubor, textura uvnitř).
- Zachovat i podporu obj+mtl (volitelné).

### API (MVP)

1. POST /v1/mesh/erase-box
    - Content-Type: multipart/form-data
    - pole:
        - mesh_file (glb/obj zip)
        - request (JSON string)
        
request:
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
Response:

- 200 OK, application/octet-stream (výsledné glb)
- hlavičky:
    - X-Vertices-Before
    - X-Vertices-After
    - X-Triangles-Before
    - X-Triangles-After
    - GET /health
- vrací 200 + {"ok": true}

### Co má server dělat

1. Načíst mesh (včetně UV a textury).
2. Pro každý box:
    - převést box na OBB v mesh_local.
    - označit trojúhelníky k odstranění podle remove_rule:
    - intersects = odstranit trojúhelník, pokud se protíná s boxem (doporučený default).
3. Odstranit označené trojúhelníky.
4. Odstranit nevyužité vrcholy + přeindexovat.
5. Zachovat UV/material/texture u zbytku.
6. Exportovat výsledek do glb.

### Důležité detaily

- space: mesh_local (ať není chaos s Unity world transformací).
- Když je mesh prázdná po řezu, vrátit 422 s chybou.
- Když box nezasáhne mesh, vrátit původní mesh (beze změny).
- Žádné “capování” děr v MVP.

### Doporučené knihovny (Python)

- trimesh (+ případně pygltflib/open3d dle potřeby)
- FastAPI + Uvicorn

### Akceptační testy

1. Box mimo mesh -> výstup stejný počet trojúhelníků.
2. Box přes stůl -> trojúhelníky ubudou, textura zůstane.
3. Více boxů v jednom requestu -> všechny aplikované.
4. GLB vstup -> GLB výstup, načitelný v Unity runtime.



