bl_info = {
    "name": "Godot Scene Exporter",
    "author": "Ruminatio Games",
    "version": (2, 6, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Godot Export",
    "description": "Export Blender assets as Godot scenes/GLB with collision suffixes, pivot alignment, preflight health checks, .import generator, and post-import script picker",
    "category": "Import-Export",
}

import bpy
import os
from bpy.props import StringProperty, EnumProperty, BoolProperty
from mathutils import Matrix, Vector
from bpy.types import Operator, Panel, PropertyGroup

NON_UNIFORM_EPS = 1e-4
GODOT_COLLISION_SUFFIXES = ('-col', '-convcol', '-colonly', '-convcolonly', '-navmesh', '-occ', '-vehicle')


def safe_name(name):
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in name)


def collect_hierarchy(root_obj):
    """Return root_obj plus every descendant, recursively."""
    objs = [root_obj]
    for child in root_obj.children:
        objs.extend(collect_hierarchy(child))
    return objs


def rename_mesh_data(hierarchy):
    """Rename each mesh object's data-block to Mesh_<ObjectName> so the
    exported glTF mesh resources are easy to identify in Godot."""
    for obj in hierarchy:
        if obj.type == 'MESH' and obj.data is not None:
            obj.data.name = f"Mesh_{safe_name(obj.name)}"


def dedupe_textures(hierarchy):
    """Materials in the hierarchy that reference the same image file on
    disk under different datablocks get relinked to one canonical datablock."""
    materials = set()
    for obj in hierarchy:
        if obj.type == 'MESH':
            for slot in obj.material_slots:
                if slot.material:
                    materials.add(slot.material)

    path_to_canonical = {}
    replaced = 0
    for mat in materials:
        if not mat.use_nodes or not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image and node.image.filepath:
                img = node.image
                try:
                    abspath = os.path.normpath(bpy.path.abspath(img.filepath, library=img.library))
                except Exception:
                    continue
                canonical = path_to_canonical.get(abspath)
                if canonical is None:
                    path_to_canonical[abspath] = img
                elif canonical is not img:
                    node.image = canonical
                    replaced += 1
    return replaced


def find_nonuniform_scale_warnings(hierarchy):
    """Flag mesh objects with non-uniform scale that also have risky modifiers."""
    risky_modifier_types = {'MIRROR', 'ARRAY', 'BEVEL', 'SOLIDIFY'}
    warnings = []
    for obj in hierarchy:
        if obj.type != 'MESH':
            continue
        sx, sy, sz = obj.scale
        is_nonuniform = (
            abs(sx - sy) > NON_UNIFORM_EPS
            or abs(sy - sz) > NON_UNIFORM_EPS
            or abs(sx - sz) > NON_UNIFORM_EPS
        )
        if not is_nonuniform:
            continue
        risky = [m.type for m in obj.modifiers if m.type in risky_modifier_types]
        if risky:
            warnings.append(f"{obj.name} (non-uniform scale + {', '.join(risky)})")
    return warnings


def get_hierarchy_bottom_center(hierarchy):
    """Compute the world-space bounding box minimum Z and center X, Y for the hierarchy."""
    world_corners = []
    for obj in hierarchy:
        if obj.type == 'MESH' and obj.data:
            for corner in obj.bound_box:
                world_corners.append(obj.matrix_world @ Vector(corner))
    if not world_corners:
        return None, None, None
    min_x = min(c.x for c in world_corners)
    max_x = max(c.x for c in world_corners)
    min_y = min(c.y for c in world_corners)
    max_y = max(c.y for c in world_corners)
    min_z = min(c.z for c in world_corners)
    return min_z, (min_x + max_x) / 2.0, (min_y + max_y) / 2.0


def apply_collision_suffixes(hierarchy, collision_mode):
    """Temporarily rename mesh objects to append Godot collision suffixes."""
    original_names = {}
    if collision_mode == 'NONE':
        return original_names

    suffix_map = {
        'CONVEX': '-convcol',
        'TRIMESH': '-col',
        'CONVEX_ONLY': '-convcolonly',
        'TRIMESH_ONLY': '-colonly',
    }

    for obj in hierarchy:
        if obj.type != 'MESH':
            continue

        if any(obj.name.endswith(sfx) for sfx in GODOT_COLLISION_SUFFIXES):
            continue

        if collision_mode == 'SMART_PREFIX':
            uname = obj.name.upper()
            if uname.startswith("UCX_") or uname.startswith("UBX_"):
                original_names[obj] = obj.name
                base = obj.name[4:]
                obj.name = f"{base}-convcolonly"
            elif uname.startswith("COL_"):
                original_names[obj] = obj.name
                base = obj.name[4:]
                obj.name = f"{base}-colonly"
        else:
            sfx = suffix_map.get(collision_mode, "")
            if sfx:
                original_names[obj] = obj.name
                obj.name = f"{obj.name}{sfx}"

    return original_names


def restore_names(original_names):
    """Restore original object names after export."""
    for obj, orig_name in original_names.items():
        obj.name = orig_name


def format_godot_res_path(file_path, output_dir):
    """Convert an absolute or relative file path to a Godot 'res://...' path relative to the Godot project root."""
    if not file_path:
        return ""

    file_path = file_path.strip()
    if file_path.startswith("res://"):
        return file_path

    abs_file = os.path.normpath(bpy.path.abspath(file_path))
    abs_out = os.path.normpath(bpy.path.abspath(output_dir)) if output_dir else ""

    start_dir = abs_out if abs_out and os.path.exists(abs_out) else os.path.dirname(abs_file)
    curr = start_dir if os.path.isdir(start_dir) else os.path.dirname(start_dir)
    project_root = None
    while curr:
        if os.path.exists(os.path.join(curr, "project.godot")):
            project_root = curr
            break
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        curr = parent

    if project_root and abs_file.startswith(project_root):
        rel = os.path.relpath(abs_file, project_root).replace("\\", "/")
        return f"res://{rel}"
    elif abs_out and abs_file.startswith(abs_out):
        rel = os.path.relpath(abs_file, abs_out).replace("\\", "/")
        return f"res://{rel}"

    return f"res://{os.path.basename(abs_file)}"


def write_godot_import_sidecar(glb_path, settings):
    """Generate a .import sidecar file next to the exported .glb file for Godot auto-import."""
    import_file = f"{glb_path}.import"
    filename = os.path.basename(glb_path)
    root_type = settings.godot_root_type
    script_path = format_godot_res_path(settings.post_import_script_path, settings.output_dir)

    content = f"""[remap]

importer="scene"
type="PackedScene"
uid="uid://auto"

[deps]

source_file="res://{filename}"

[params]

nodes/root_type="{root_type}"
nodes/root_name=""
import_script/path="{script_path}"
glTF/embedded_image_handling=1
"""
    try:
        with open(import_file, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"Failed to write sidecar .import file: {e}")
        return False


def export_target_as_glb(target, out_dir, context, export_format, texture_dir):
    """Export an object hierarchy or collection as a glTF/GLB file."""
    if isinstance(target, bpy.types.Collection):
        base_name = safe_name(target.name)
        hierarchy = [o for o in target.all_objects if o.type in {'MESH', 'ARMATURE', 'EMPTY', 'LIGHT', 'CAMERA'}]
        coll_objs = set(hierarchy)
        roots = [o for o in hierarchy if o.parent not in coll_objs]
    else:
        base_name = safe_name(target.name)
        hierarchy = collect_hierarchy(target)
        roots = [target]

    if not hierarchy:
        raise ValueError("Hierarchy contains no exportable objects")

    extension = ".glb" if export_format == 'GLB' else ".gltf"
    glb_path = os.path.join(out_dir, f"{base_name}{extension}")

    rename_mesh_data(hierarchy)
    textures_merged = dedupe_textures(hierarchy)
    scale_warnings = find_nonuniform_scale_warnings(hierarchy)
    settings = context.scene.godot_export_settings

    # Collision renaming
    original_names = apply_collision_suffixes(hierarchy, settings.collision_mode)

    # Pivot / Origin adjustments
    original_matrices = {}
    pivot_mode = settings.pivot_mode

    if pivot_mode == 'WORLD' and roots:
        inv = roots[0].matrix_world.inverted()
        for obj in hierarchy:
            original_matrices[obj] = obj.matrix_world.copy()
            obj.matrix_world = inv @ obj.matrix_world
        context.view_layer.update()
    elif pivot_mode == 'BOTTOM_CENTER':
        min_z, center_x, center_y = get_hierarchy_bottom_center(hierarchy)
        if min_z is not None:
            offset_matrix = Matrix.Translation(Vector((-center_x, -center_y, -min_z)))
            for obj in hierarchy:
                original_matrices[obj] = obj.matrix_world.copy()
                obj.matrix_world = offset_matrix @ obj.matrix_world
            context.view_layer.update()

    bpy.ops.object.select_all(action='DESELECT')
    for obj in hierarchy:
        obj.select_set(True)
    if roots and roots[0] in context.view_layer.objects.values():
        context.view_layer.objects.active = roots[0]

    try:
        bpy.ops.export_scene.gltf(
            filepath=glb_path,
            export_format=export_format,
            use_selection=True,
            export_apply=True,
            export_yup=True,
            export_extras=settings.export_extras,
            export_texture_dir=texture_dir if export_format == 'GLTF_SEPARATE' else ""
        )

        if settings.generate_import_files:
            write_godot_import_sidecar(glb_path, settings)
    finally:
        if original_matrices:
            for obj, m in original_matrices.items():
                obj.matrix_world = m
            context.view_layer.update()
        if original_names:
            restore_names(original_names)

    return glb_path, len(hierarchy), textures_merged, scale_warnings


def gather_targets(context, mode):
    """Return top-level objects or collections to export."""
    if mode == 'SELECTED':
        selected = set(context.selected_objects)
        return [o for o in selected if o.parent not in selected]
    elif mode == 'ACTIVE_COLLECTION':
        coll = context.view_layer.active_layer_collection.collection
        members = set(coll.objects)
        return [o for o in members if o.parent not in members]
    elif mode == 'COLLECTIONS':
        return [c for c in context.scene.collection.children_recursive if len(c.all_objects) > 0]
    else:  # ALL_TOP_LEVEL
        return [o for o in context.scene.objects if o.parent is None]


def unique_export_name(base_name, used_names):
    name = base_name
    counter = 2
    while name in used_names:
        name = f"{base_name}_{counter}"
        counter += 1
    used_names.add(name)
    return name


def run_preflight_health_check(context):
    """Scan scene/export targets for common issues before exporting."""
    settings = context.scene.godot_export_settings
    targets = gather_targets(context, settings.export_mode)
    issues = []

    if not targets:
        issues.append({"level": "ERROR", "msg": "No export targets found for the selected mode."})
        return issues

    seen_names = set()
    total_objs = 0

    for target in targets:
        s_name = safe_name(target.name)
        if s_name in seen_names:
            issues.append({"level": "WARNING", "msg": f"Name Collision: Multiple targets will export as '{s_name}'"})
        seen_names.add(s_name)

        if isinstance(target, bpy.types.Collection):
            hierarchy = list(target.all_objects)
        else:
            hierarchy = collect_hierarchy(target)

        total_objs += len(hierarchy)

        for obj in hierarchy:
            # Unapplied scale / rotation check
            if obj.type == 'MESH':
                sx, sy, sz = obj.scale
                rx, ry, rz = obj.rotation_euler
                if abs(sx - 1.0) > 1e-3 or abs(sy - 1.0) > 1e-3 or abs(sz - 1.0) > 1e-3:
                    issues.append({"level": "WARNING", "msg": f"Unapplied Scale on '{obj.name}': scale is ({sx:.2f}, {sy:.2f}, {sz:.2f})"})
                if abs(rx) > 1e-3 or abs(ry) > 1e-3 or abs(rz) > 1e-3:
                    issues.append({"level": "WARNING", "msg": f"Unapplied Rotation on '{obj.name}'"})

                # Missing materials check
                if not obj.material_slots or all(s.material is None for s in obj.material_slots):
                    issues.append({"level": "WARNING", "msg": f"Missing Material on mesh '{obj.name}'"})

                # Missing texture image check
                for slot in obj.material_slots:
                    if slot.material and slot.material.use_nodes and slot.material.node_tree:
                        for node in slot.material.node_tree.nodes:
                            if node.type == 'TEX_IMAGE' and node.image:
                                if node.image.filepath:
                                    abs_p = bpy.path.abspath(node.image.filepath)
                                    if not os.path.exists(abs_p):
                                        issues.append({"level": "ERROR", "msg": f"Broken Texture: '{node.image.name}' file not found on disk"})

                # High polygon count check (> 50k tris)
                try:
                    tris = sum(len(p.vertices) - 2 for p in obj.data.polygons)
                    if tris > 50000:
                        issues.append({"level": "WARNING", "msg": f"High Poly Count: '{obj.name}' has {tris:,} triangles"})
                except Exception:
                    pass

    return issues


class GODOT_EXPORT_settings(PropertyGroup):
    output_dir: StringProperty(
        name="Output Folder",
        description="Folder to write export files into (usually your Godot project folder)",
        subtype='DIR_PATH',
    )
    export_mode: EnumProperty(
        name="Export",
        items=[
            ('SELECTED', "Selected Objects", "Export each selected top-level object hierarchy as a separate file"),
            ('ALL_TOP_LEVEL', "All Top-Level Objects", "Export every unparented object hierarchy in the scene as separate files"),
            ('ACTIVE_COLLECTION', "Active Collection Objects", "Export every top-level object hierarchy in the active collection"),
            ('COLLECTIONS', "All Collections", "Export each Collection in the scene as its own glTF/GLB asset file"),
        ],
        default='SELECTED',
    )
    export_format: EnumProperty(
        name="Format",
        items=[
            ('GLB', "Single File (.glb)", "One self-contained binary file per asset; textures are embedded"),
            ('GLTF_SEPARATE', "Separate Textures (.gltf)", "Textures and buffers written as external files next to .gltf"),
        ],
        default='GLB',
    )
    collision_mode: EnumProperty(
        name="Collision Mode",
        description="Automatically append Godot collision suffixes to exported mesh objects",
        items=[
            ('NONE', "None", "Do not modify object names"),
            ('CONVEX', "Convex (-convcol)", "Add -convcol suffix to meshes (fast convex collision in Godot)"),
            ('TRIMESH', "Trimesh (-col)", "Add -col suffix to meshes (exact static trimesh collision in Godot)"),
            ('CONVEX_ONLY', "Convex Only (-convcolonly)", "Add -convcolonly suffix (invisible convex collider shape)"),
            ('TRIMESH_ONLY', "Trimesh Only (-colonly)", "Add -colonly suffix (invisible trimesh collider shape)"),
            ('SMART_PREFIX', "Smart Prefix (UCX_, UBX_, COL_)", "Convert UCX_/UBX_ prefixes to -convcolonly and COL_ to -colonly"),
        ],
        default='NONE',
    )
    pivot_mode: EnumProperty(
        name="Pivot / Origin",
        description="Control origin placement for exported asset files",
        items=[
            ('OBJECT', "Object Origin", "Keep original object pivot/origin points"),
            ('WORLD', "World Origin", "Shift hierarchy root to world origin (0, 0, 0)"),
            ('BOTTOM_CENTER', "Bottom Center", "Shift bounding box bottom center to origin (ideal for ground snapping in Godot)"),
        ],
        default='OBJECT',
    )
    export_extras: BoolProperty(
        name="Export Custom Properties",
        description="Include Blender custom properties as glTF extras (accessible in Godot via node.get_meta())",
        default=True,
    )
    generate_import_files: BoolProperty(
        name="Generate .import Sidecars",
        description="Write matching .import sidecar files to auto-configure Godot scene import settings",
        default=True,
    )
    godot_root_type: EnumProperty(
        name="Godot Root Node",
        description="Default root node type for imported Godot scene",
        items=[
            ('Node3D', "Node3D", "Standard 3D container node"),
            ('RigidBody3D', "RigidBody3D", "Dynamic rigid physics body node"),
            ('StaticBody3D', "StaticBody3D", "Static physics body node"),
            ('CharacterBody3D', "CharacterBody3D", "Kinematic character physics body node"),
            ('Area3D', "Area3D", "3D trigger / detector region node"),
        ],
        default='Node3D',
    )
    post_import_script_path: StringProperty(
        name="Import Script",
        description="Select an EditorScenePostImport GDScript file (.gd) from your computer or Godot project",
        subtype='FILE_PATH',
        default="",
    )
    texture_dir: StringProperty(
        name="Texture Subfolder",
        description="Subfolder relative to output folder for external textures (Only for 'Separate Textures')",
        default="Textures",
    )


class GODOT_OT_preflight_check(Operator):
    bl_idname = "godot_export.preflight_check"
    bl_label = "Run Preflight Health Check"
    bl_description = "Check targets for unapplied scale, missing textures, missing materials, and polycounts"

    def execute(self, context):
        issues = run_preflight_health_check(context)
        errors = [i for i in issues if i["level"] == "ERROR"]
        warnings = [i for i in issues if i["level"] == "WARNING"]

        if not issues:
            self.report({'INFO'}, "Preflight Check Passed! 0 issues found.")
        else:
            self.report({'WARNING'}, f"Preflight Check: {len(errors)} error(s), {len(warnings)} warning(s). See console/panel.")
            print("\n=== Godot Exporter Preflight Report ===")
            for issue in issues:
                print(f"  [{issue['level']}] {issue['msg']}")
            print("=======================================\n")

        return {'FINISHED'}


class GODOT_OT_export_scenes(Operator):
    bl_idname = "godot_export.export_scenes"
    bl_label = "Export Asset Pack for Godot"
    bl_description = "Export asset hierarchies/collections as glTF/GLB files for Godot"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.godot_export_settings
        out_dir = bpy.path.abspath(settings.output_dir)

        if not out_dir:
            self.report({'ERROR'}, "Set an output folder first")
            return {'CANCELLED'}

        os.makedirs(out_dir, exist_ok=True)

        targets = gather_targets(context, settings.export_mode)
        if not targets:
            self.report({'WARNING'}, "No valid targets found for chosen export mode")
            return {'CANCELLED'}

        active_before = context.view_layer.objects.active
        selected_before = list(context.selected_objects)

        used_names = set()
        results = []
        total_objects = 0
        total_textures_merged = 0
        all_scale_warnings = []

        for target in targets:
            base_name = safe_name(target.name)
            export_name = unique_export_name(base_name, used_names)
            if export_name != base_name:
                self.report({'WARNING'}, f"Name collision: '{target.name}' exported as '{export_name}'")
            try:
                renamed = export_name != base_name
                original_name = target.name
                if renamed:
                    target.name = export_name

                _, obj_count, textures_merged, scale_warnings = export_target_as_glb(
                    target, out_dir, context, settings.export_format, settings.texture_dir
                )

                if renamed:
                    target.name = original_name

                results.append((export_name, obj_count, None))
                total_objects += obj_count
                total_textures_merged += textures_merged
                all_scale_warnings.extend(scale_warnings)
            except Exception as e:
                results.append((export_name, None, str(e)))
                self.report({'ERROR'}, f"Failed to export {target.name}: {e}")

        bpy.ops.object.select_all(action='DESELECT')
        for obj in selected_before:
            if obj in context.view_layer.objects.values():
                obj.select_set(True)
        if active_before and active_before in context.view_layer.objects.values():
            context.view_layer.objects.active = active_before

        succeeded = [r for r in results if r[2] is None]
        failed = [r for r in results if r[2] is not None]

        print("\n=== Godot Export Report ===")
        for name, count, error in results:
            if error is None:
                print(f"  OK   {name}: {count} object(s)")
            else:
                print(f"  FAIL {name}: {error}")
        if total_textures_merged:
            print(f"  Merged {total_textures_merged} duplicate texture reference(s)")
        if all_scale_warnings:
            print("  Non-uniform scale warnings:")
            for w in all_scale_warnings:
                print(f"    - {w}")
        print("============================\n")

        if all_scale_warnings:
            self.report({'WARNING'}, f"{len(all_scale_warnings)} object(s) have non-uniform scale warnings (see console)")
        if total_textures_merged:
            self.report({'INFO'}, f"Merged {total_textures_merged} texture(s)")

        summary = f"Exported {len(succeeded)}/{len(results)} file(s), {total_objects} object(s) total, to {out_dir}"
        if failed:
            self.report({'WARNING'}, summary + f" — {len(failed)} failed, see console")
        else:
            self.report({'INFO'}, summary)

        return {'FINISHED'}


DEFAULT_POST_IMPORT_SCRIPT = """# Post-import script for Godot 4.x
#
# What it does, every time a matching 3D scene (e.g. a .glb from the
# Blender exporter) is imported or re-imported:
#   1. Renames every MeshInstance3D node to "Mesh_<NodeName>" so the
#      prefix is actually visible in the scene tree.
#   2. Extracts each surface material to a shared "Materials" folder
#      next to the source file, creating the folder if needed.
#   3. For any physics body in the scene (RigidBody3D, StaticBody3D, CharacterBody3D, Area3D),
#      reparents all collision shapes (CollisionShape3D / CollisionPolygon3D) to be DIRECT children
#      of the physics body (not buried under MeshInstance3D or dummy child bodies), and cleans up
#      redundant empty intermediate body nodes.
#
# Setup:
#   1. Save this file somewhere in your project (e.g. res://scripts/godot_post_import.gd).
#   2. Select any imported .glb in the FileSystem dock.
#   3. Go to the Import dock, set "Script" to this file.
#   4. Click "Set as Default for 'Scene'" so it applies to every scene
#      import in the project (existing imports need a re-import: right
#      click the file(s) > Reimport).

@tool
extends EditorScenePostImport


func _post_import(scene: Node) -> Object:
	var source_dir: String = get_source_file().get_base_dir()
	var materials_dir: String = source_dir.path_join("Materials")

	if not DirAccess.dir_exists_absolute(materials_dir):
		DirAccess.make_dir_recursive_absolute(materials_dir)

	_process_node(scene, materials_dir)
	_fix_physics_body_hierarchy(scene)
	return scene


func _process_node(node: Node, materials_dir: String) -> void:
	if node is MeshInstance3D:
		if not node.name.begins_with("Mesh_"):
			node.name = "Mesh_%s" % node.name
		_extract_materials(node, materials_dir)

	for child in node.get_children():
		_process_node(child, materials_dir)


func _extract_materials(mesh_instance: MeshInstance3D, materials_dir: String) -> void:
	var mesh: Mesh = mesh_instance.mesh
	if mesh == null:
		return

	for surface_idx in mesh.get_surface_count():
		var mat: Material = mesh_instance.get_active_material(surface_idx)
		if mat == null:
			continue

		var mat_name: String = mat.resource_name if mat.resource_name != "" else "Material_%d" % surface_idx
		var mat_path: String = materials_dir.path_join("%s.tres" % mat_name)

		if FileAccess.file_exists(mat_path):
			var existing: Material = load(mat_path)
			mesh_instance.set_surface_override_material(surface_idx, existing)
		else:
			var err: int = ResourceSaver.save(mat, mat_path)
			if err == OK:
				var saved: Material = load(mat_path)
				mesh_instance.set_surface_override_material(surface_idx, saved)


func _fix_physics_body_hierarchy(scene_root: Node) -> void:
	var physics_bodies: Array[Node] = []
	_collect_all_physics_bodies(scene_root, physics_bodies)

	for body in physics_bodies:
		_flatten_collision_shapes_for_body(body, scene_root)


func _collect_all_physics_bodies(node: Node, bodies: Array[Node]) -> void:
	if node is StaticBody3D or node is RigidBody3D or node is CharacterBody3D or node is Area3D:
		bodies.append(node)
	for child in node.get_children():
		_collect_all_physics_bodies(child, bodies)


func _flatten_collision_shapes_for_body(body: Node, scene_root: Node) -> void:
	var shapes: Array[Node3D] = []
	var dummy_bodies: Array[Node] = []

	_collect_shapes_under(body, body, shapes, dummy_bodies)

	for shape in shapes:
		if shape.get_parent() != body:
			var rel_xform: Transform3D = _get_transform_relative_to(shape, body as Node3D)
			var old_parent: Node = shape.get_parent()
			if old_parent != null:
				old_parent.remove_child(shape)
			body.add_child(shape)
			shape.transform = rel_xform
			shape.owner = scene_root

	for dummy in dummy_bodies:
		if dummy != body and dummy is Node and dummy.get_child_count() == 0:
			dummy.queue_free()


func _collect_shapes_under(curr_node: Node, target_body: Node, shapes: Array[Node3D], dummy_bodies: Array[Node]) -> void:
	for child in curr_node.get_children():
		if child is CollisionShape3D or child is CollisionPolygon3D:
			shapes.append(child as Node3D)
		else:
			if (child is StaticBody3D or child is RigidBody3D or child is CharacterBody3D or child is Area3D) and child != target_body:
				dummy_bodies.append(child)
			_collect_shapes_under(child, target_body, shapes, dummy_bodies)


func _get_transform_relative_to(node: Node3D, ancestor: Node3D) -> Transform3D:
	var xform: Transform3D = Transform3D.IDENTITY
	var curr: Node = node
	while curr != null and curr != ancestor:
		if curr is Node3D:
			xform = (curr as Node3D).transform * xform
		curr = curr.get_parent()
	return xform
"""


class GODOT_OT_generate_post_import_script(Operator):
    bl_idname = "godot_export.generate_post_import_script"
    bl_label = "Generate Post-Import Script"
    bl_description = "Generate a default Godot EditorScenePostImport script (.gd) that handles mesh renaming, material deduplication, and StaticBody3D root collision shape reparenting"

    def execute(self, context):
        settings = context.scene.godot_export_settings
        out_dir = bpy.path.abspath(settings.output_dir) if settings.output_dir else ""
        if not out_dir:
            self.report({'ERROR'}, "Set an output folder first")
            return {'CANCELLED'}

        os.makedirs(out_dir, exist_ok=True)
        script_file = os.path.join(out_dir, "godot_post_import.gd")
        try:
            with open(script_file, "w", encoding="utf-8") as f:
                f.write(DEFAULT_POST_IMPORT_SCRIPT)
            settings.post_import_script_path = script_file
            self.report({'INFO'}, f"Generated post-import script: {script_file}")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to write post-import script: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}


class GODOT_PT_export_panel(Panel):
    bl_label = "Godot Export"
    bl_idname = "GODOT_PT_export_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Godot Export"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.godot_export_settings

        layout.prop(settings, "output_dir")
        layout.prop(settings, "export_mode")
        layout.prop(settings, "export_format")

        box = layout.box()
        box.label(text="Asset Processing:", icon='SETTINGS')
        box.prop(settings, "collision_mode")
        box.prop(settings, "pivot_mode")
        box.prop(settings, "export_extras")

        imp_box = layout.box()
        imp_box.label(text="Godot Import Sidecars:", icon='FILE_TEXT')
        imp_box.prop(settings, "generate_import_files")
        if settings.generate_import_files:
            imp_box.prop(settings, "godot_root_type")
            row = imp_box.row(align=True)
            row.prop(settings, "post_import_script_path", text="Import Script")
            row.operator(GODOT_OT_generate_post_import_script.bl_idname, text="", icon='ADD')

        if settings.export_format == 'GLTF_SEPARATE':
            layout.prop(settings, "texture_dir")

        layout.separator()
        layout.operator(GODOT_OT_preflight_check.bl_idname, icon='CHECKMARK')
        layout.operator(GODOT_OT_export_scenes.bl_idname, icon='EXPORT')


classes = (
    GODOT_EXPORT_settings,
    GODOT_OT_preflight_check,
    GODOT_OT_generate_post_import_script,
    GODOT_OT_export_scenes,
    GODOT_PT_export_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.godot_export_settings = bpy.props.PointerProperty(type=GODOT_EXPORT_settings)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.godot_export_settings


if __name__ == "__main__":
    register()
