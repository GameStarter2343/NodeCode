bl_info = {
    "name": "Node Code Converter",
    "blender": (4, 3, 0),
    "category": "Node",
}

import json

import bpy

# Constants
GROUP_NODE_TYPES = {
    "ShaderNodeGroup",
    "GeometryNodeGroup",
    "CompositorNodeGroup",
    "TextureNodeGroup",
}


# ------------------------
# Helpers
# ------------------------

NODE_MODE_PROPERTIES = (
    "operation",
    "blend_type",
    "blend_mode",
    "mode",
    "data_type",
    "interpolation_type",
    "vector_type",
    "color_space",
    "space",
    "mapping",
    "noise_dimensions",
    "noise_type",
    "distance_metric",
    "voronoi_feature",
    "feature",
    "musgrave_type",
    "wave_type",
    "wave_profile",
    "bands_direction",
    "rings_direction",
    "gradient_type",
    "distribution",
    "falloff_type",
    "clamp",
    "clamp_factor",
    "use_clamp",
    "component",
    "direction_type",
    "pivot_axis",
)


def _get_node_mode_properties(node):
    props = {}
    for attr in NODE_MODE_PROPERTIES:
        if not hasattr(node, attr):
            continue
        try:
            props[attr] = getattr(node, attr)
        except Exception:
            pass
    return props


def _apply_node_mode_properties(node, props):
    for attr, value in props.items():
        if hasattr(node, attr):
            try:
                setattr(node, attr, value)
            except Exception:
                pass


# ------------------------
# Color ramp helpers
# ------------------------


def _export_color_ramp(node):
    cr = getattr(node, "color_ramp", None)
    if cr is None:
        return None
    return {
        "color_mode": cr.color_mode,
        "interpolation": cr.interpolation,
        "hue_interpolation": cr.hue_interpolation,
        "elements": [
            {"position": el.position, "color": list(el.color)} for el in cr.elements
        ],
    }


def _apply_color_ramp(node, cr_data):
    if not cr_data:
        return
    cr = getattr(node, "color_ramp", None)
    if cr is None:
        return

    try:
        cr.color_mode = cr_data.get("color_mode", cr.color_mode)
        cr.interpolation = cr_data.get("interpolation", cr.interpolation)
        cr.hue_interpolation = cr_data.get("hue_interpolation", cr.hue_interpolation)
    except Exception:
        pass

    elements_data = cr_data.get("elements", [])
    if not elements_data:
        return

    target_count = len(elements_data)
    while len(cr.elements) < target_count:
        cr.elements.new(0.0)

    for i in range(target_count - 1, -1, -1):
        ed = elements_data[i]
        el = cr.elements[i]
        try:
            el.position = ed["position"]
        except Exception:
            pass
        try:
            el.color = ed["color"]
        except Exception:
            pass

    while len(cr.elements) > target_count:
        cr.elements.remove(cr.elements[-1])


# ------------------------
# Socket value helpers (now with list → tuple conversion)
# ------------------------


def _export_socket_values(sockets):
    result = {}
    for i, sock in enumerate(sockets):
        if not hasattr(sock, "default_value"):
            continue
        try:
            val = sock.default_value
            key = f"{i}:{sock.name}"  # index prevents duplicate-name collisions
            if hasattr(val, "__len__"):
                result[key] = list(val)
            else:
                result[key] = val
        except Exception:
            pass
    return result


def _apply_socket_values(sockets, values):
    for i, sock in enumerate(sockets):
        # Prefer the new indexed key; fall back to bare name for old JSON
        key = f"{i}:{sock.name}"
        value = values.get(key, values.get(sock.name))
        if value is None:
            continue
        try:
            if isinstance(value, list):
                value = tuple(value)
            sock.default_value = value
        except Exception:
            pass


# ------------------------
# Context helpers (unchanged)
# ------------------------


def get_active_node_tree(context):
    space = context.space_data
    if not space or space.type != "NODE_EDITOR":
        return None, "Open a Node Editor first"

    tree = space.edit_tree or space.node_tree
    if tree:
        return tree, None

    tree_type = space.tree_type

    if tree_type == "ShaderNodeTree":
        if space.shader_type == "WORLD":
            world = context.scene.world
            if not world:
                return None, "No world in scene"
            world.use_nodes = True
            return world.node_tree, None

        obj = context.object
        if not obj:
            return None, "No active object"

        mat = obj.active_material
        if not mat:
            return None, "No active material on object"

        mat.use_nodes = True
        return mat.node_tree, None

    if tree_type == "GeometryNodeTree":
        obj = context.object
        if not obj:
            return None, "No active object"
        for mod in obj.modifiers:
            if mod.type == "NODES" and mod.node_group:
                return mod.node_group, None
        return None, "No Geometry Nodes modifier with a node group"

    if tree_type == "CompositorNodeTree":
        context.scene.use_nodes = True
        return context.scene.node_tree, None

    if tree_type == "TextureNodeTree":
        tex = getattr(context, "texture", None)
        if not tex:
            return None, "No active texture"
        return tex.node_tree, None

    return None, f"Cannot resolve node tree for type '{tree_type}'"


def ensure_import_node_tree(context, tree_type_hint):
    if tree_type_hint == "CompositorNodeTree":
        context.scene.use_nodes = True
        return context.scene.node_tree, None

    if tree_type_hint == "ShaderNodeTree":
        obj = context.object
        if not obj:
            return None, "No active object to import material onto"

        mat = obj.active_material
        if not mat:
            mat = bpy.data.materials.new(name="Imported Material")
            obj.data.materials.append(mat)

        mat.use_nodes = True
        return mat.node_tree, None

    if tree_type_hint == "GeometryNodeTree":
        obj = context.object
        if not obj:
            return None, "No active object to add Geometry Nodes to"

        for mod in obj.modifiers:
            if mod.type == "NODES":
                if not mod.node_group:
                    mod.node_group = bpy.data.node_groups.new(
                        "Geometry Nodes", "GeometryNodeTree"
                    )
                return mod.node_group, None

        mod = obj.modifiers.new("GeometryNodes", "NODES")
        mod.node_group = bpy.data.node_groups.new("Geometry Nodes", "GeometryNodeTree")
        return mod.node_group, None

    return None, f"Cannot auto-create node tree for type '{tree_type_hint}'"


# ------------------------
# Export / Import (core logic)
# ------------------------


def _export_single_tree(node_tree):
    data = {"nodes": [], "links": []}

    for node in node_tree.nodes:
        node_data = {
            "name": node.name,
            "label": node.label,
            "type": node.bl_idname,
            "location": [node.location.x, node.location.y],
            "width": node.width,
            "hide": node.hide,
            "mute": node.mute,
            "color": list(node.color) if node.use_custom_color else None,
            "inputs": _export_socket_values(node.inputs),
            "outputs": _export_socket_values(node.outputs),
            "parent": node.parent.name if node.parent else None,
            "node_props": _get_node_mode_properties(node),
        }

        if node.bl_idname == "NodeFrame":
            node_data["frame"] = {
                "label_size": getattr(node, "label_size", None),
                "shrink": getattr(node, "shrink", False),
            }

        cr_data = _export_color_ramp(node)
        if cr_data is not None:
            node_data["color_ramp"] = cr_data

        if node.bl_idname in GROUP_NODE_TYPES:
            grp = getattr(node, "node_tree", None)
            if grp:
                node_data["node_group_name"] = grp.name

        data["nodes"].append(node_data)

    for link in node_tree.links:
        data["links"].append(
            {
                "from_node": link.from_node.name,
                "from_socket_index": list(link.from_node.outputs).index(
                    link.from_socket
                ),
                "to_node": link.to_node.name,
                "to_socket_index": list(link.to_node.inputs).index(link.to_socket),
                "from_socket_name": link.from_socket.name,
                "to_socket_name": link.to_socket.name,
            }
        )

    return data


def _collect_groups(node_tree, groups_out, visited):
    for node in node_tree.nodes:
        if node.bl_idname not in GROUP_NODE_TYPES:
            continue
        grp = getattr(node, "node_tree", None)
        if grp and grp.name not in visited:
            visited.add(grp.name)
            groups_out[grp.name] = _export_single_tree(grp)
            _collect_groups(grp, groups_out, visited)


def export_node_tree_to_json(node_tree):
    groups = {}
    _collect_groups(node_tree, groups, set())
    result = {
        "tree_type": node_tree.bl_idname,
        "main_tree": _export_single_tree(node_tree),
        "node_groups": groups,
    }
    return json.dumps(result, indent=2)


def _import_single_tree(node_tree, tree_data, groups_map):
    node_tree.nodes.clear()
    created = {}

    # Pass 1 – create + configure nodes
    for nd in tree_data.get("nodes", []):
        try:
            node = node_tree.nodes.new(nd["type"])
        except RuntimeError:
            continue

        node.name = nd.get("name", node.name)
        node.label = nd.get("label", "")
        node.width = nd.get("width", node.width)
        node.hide = nd.get("hide", False)
        node.mute = nd.get("mute", False)

        colour = nd.get("color")
        if colour is not None:
            node.use_custom_color = True
            node.color = colour

        if node.bl_idname == "NodeFrame":
            frame_data = nd.get("frame", {})
            if frame_data:
                try:
                    if frame_data.get("label_size") is not None:
                        node.label_size = frame_data["label_size"]
                    node.shrink = frame_data.get("shrink", False)
                except Exception:
                    pass

        grp_name = nd.get("node_group_name")
        if grp_name and grp_name in groups_map:
            try:
                node.node_tree = groups_map[grp_name]
            except Exception:
                pass

        _apply_node_mode_properties(node, nd.get("node_props", {}))
        _apply_color_ramp(node, nd.get("color_ramp"))
        _apply_socket_values(node.inputs, nd.get("inputs", {}))
        _apply_socket_values(node.outputs, nd.get("outputs", {}))

        created[node.name] = node

    # Pass 2-4 – frames & positioning (unchanged)
    for nd in tree_data.get("nodes", []):
        if nd.get("type") != "NodeFrame":
            continue
        node = created.get(nd.get("name"))
        if node:
            try:
                node.location = nd.get("location", [0, 0])
            except Exception:
                pass

    for nd in tree_data.get("nodes", []):
        parent_name = nd.get("parent")
        if not parent_name:
            continue
        node = created.get(nd.get("name"))
        parent = created.get(parent_name)
        if node and parent:
            try:
                node.parent = parent
            except Exception:
                pass

    for nd in tree_data.get("nodes", []):
        if nd.get("type") == "NodeFrame":
            continue
        node = created.get(nd.get("name"))
        if node:
            try:
                node.location = nd.get("location", [0, 0])
            except Exception:
                pass

    # Links – supports ALL formats you have used so far
    for lnk in tree_data.get("links", []):
        try:
            from_node = created.get(lnk["from_node"])
            to_node = created.get(lnk["to_node"])
            if not from_node or not to_node:
                continue

            # 1. Index (fastest, from normal export)
            from_idx = lnk.get("from_socket_index")
            to_idx = lnk.get("to_socket_index")
            if from_idx is not None and to_idx is not None:
                if from_idx < len(from_node.outputs) and to_idx < len(to_node.inputs):
                    node_tree.links.new(
                        from_node.outputs[from_idx], to_node.inputs[to_idx]
                    )
                    continue

            # 2. Your current JSON format (from_socket / to_socket)
            if "from_socket" in lnk and "to_socket" in lnk:
                from_socket = from_node.outputs.get(lnk["from_socket"])
                to_socket = to_node.inputs.get(lnk["to_socket"])
                if from_socket and to_socket:
                    node_tree.links.new(from_socket, to_socket)
                    continue

            # 3. Old fallback
            from_socket = from_node.outputs.get(lnk.get("from_socket_name"))
            to_socket = to_node.inputs.get(lnk.get("to_socket_name"))
            if from_socket and to_socket:
                node_tree.links.new(from_socket, to_socket)

        except Exception:
            pass


def import_node_tree_from_json(node_tree, json_data):
    data = json.loads(json_data)

    if "main_tree" not in data:
        data = {
            "tree_type": node_tree.bl_idname,
            "main_tree": data,
            "node_groups": {},
        }

    tree_type = data.get("tree_type", "ShaderNodeTree")
    groups_map = {}

    for grp_name in data.get("node_groups", {}):
        existing = bpy.data.node_groups.get(grp_name)
        if existing and existing.bl_idname == tree_type:
            groups_map[grp_name] = existing
        else:
            groups_map[grp_name] = bpy.data.node_groups.new(grp_name, tree_type)

    for grp_name, grp_data in data.get("node_groups", {}).items():
        _import_single_tree(groups_map[grp_name], grp_data, groups_map)

    _import_single_tree(node_tree, data["main_tree"], groups_map)


# ------------------------
# Operators / UI / Register
# ------------------------


class NODECODE_OT_export(bpy.types.Operator):
    bl_idname = "nodecode.export"
    bl_label = "Export Nodes to Clipboard"

    def execute(self, context):
        tree, err = get_active_node_tree(context)
        if err:
            self.report({"WARNING"}, err)
            return {"CANCELLED"}

        json_data = export_node_tree_to_json(tree)
        context.window_manager.clipboard = json_data
        self.report(
            {"INFO"}, "Node tree exported to clipboard (all preset values saved)"
        )
        return {"FINISHED"}


class NODECODE_OT_import_buffer(bpy.types.Operator):
    bl_idname = "nodecode.import_buffer"
    bl_label = "Import Nodes from Clipboard"

    def execute(self, context):
        raw = context.window_manager.clipboard
        try:
            data = json.loads(raw)
        except Exception:
            self.report({"ERROR"}, "Invalid JSON")
            return {"CANCELLED"}

        tree_type_hint = data.get("tree_type", "ShaderNodeTree")

        tree, err = get_active_node_tree(context)
        if err:
            tree, err = ensure_import_node_tree(context, tree_type_hint)
            if err:
                self.report({"WARNING"}, err)
                return {"CANCELLED"}

        import_node_tree_from_json(tree, raw)
        self.report({"INFO"}, "Node tree imported successfully")
        return {"FINISHED"}


class NODECODE_PT_panel(bpy.types.Panel):
    bl_label = "Node Code"
    bl_idname = "NODECODE_PT_panel"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "NodeCode"

    def draw(self, context):
        layout = self.layout
        layout.operator("nodecode.export", icon="COPYDOWN")
        layout.operator("nodecode.import_buffer", icon="PASTEDOWN")


classes = (
    NODECODE_OT_export,
    NODECODE_OT_import_buffer,
    NODECODE_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
