# Post-import script for Godot 4.x
#
# What it does, every time a matching 3D scene (e.g. a .glb from the
# Blender exporter) is imported or re-imported:
#   1. Renames every MeshInstance3D node to "Mesh_<NodeName>" so the
#      prefix is actually visible in the scene tree.
#   2. Extracts each surface material to a shared "Materials" folder
#      next to the source file, creating the folder if needed, and
#      reuses an existing .tres of the same name instead of writing a
#      duplicate — so materials shared across multiple exported assets
#      collapse to one file on disk.
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
