bl_info = {
    "name": "Node Code Converter",
    "blender": (2, 93, 0),
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

TREE_TYPE_LABELS = {
    "ShaderNodeTree": "Shader / World",
    "GeometryNodeTree": "Geometry Nodes",
    "CompositorNodeTree": "Compositor",
    "TextureNodeTree": "Texture",
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
    """Return a serialisable dict for node.color_ramp, or None."""
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
    """Rebuild node.color_ramp from a previously exported dict."""
    if not cr_data:
        return
    cr = getattr(node, "color_ramp", None)
    if cr is None:
        return

    # Ramp-level settings
    try:
        cr.color_mode = cr_data.get("color_mode", cr.color_mode)
        cr.interpolation = cr_data.get("interpolation", cr.interpolation)
        cr.hue_interpolation = cr_data.get("hue_interpolation", cr.hue_interpolation)
    except Exception:
        pass

    elements_data = cr_data.get("elements", [])
    if not elements_data:
        return

    # A fresh ColorRamp always contains exactly 2 elements and the API
    # won't let you go below 2.  Strategy:
    #   1. Add all extra stops we need beyond the initial 2.
    #   2. Set every stop's position + color back-to-front (so repositioning
    #      a stop never pushes a neighbour past it and triggers a reorder).
    #   3. Remove any leftover stops from the tail.

    target_count = len(elements_data)
    while len(cr.elements) < target_count:
        cr.elements.new(0.0)  # position will be overwritten below

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

    # Remove surplus elements (only needed if saved ramp somehow had <2 stops).
    while len(cr.elements) > target_count:
        cr.elements.remove(cr.elements[-1])


# ------------------------
# Context helpers
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
# Export
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
            "inputs": {},
            "parent": node.parent.name if node.parent else None,
            "node_props": _get_node_mode_properties(node),
        }

        # Frame-specific data
        if node.bl_idname == "NodeFrame":
            node_data["frame"] = {
                "label_size": getattr(node, "label_size", None),
                "shrink": getattr(node, "shrink", False),
            }

        # Color ramp (ShaderNodeValToRGB, ColorRamp in GN, etc.)
        cr_data = _export_color_ramp(node)
        if cr_data is not None:
            node_data["color_ramp"] = cr_data

        if node.bl_idname in GROUP_NODE_TYPES:
            grp = getattr(node, "node_tree", None)
            if grp:
                node_data["node_group_name"] = grp.name

        for sock in node.inputs:
            if not hasattr(sock, "default_value"):
                continue
            try:
                node_data["inputs"][sock.name] = list(sock.default_value)
            except TypeError:
                node_data["inputs"][sock.name] = sock.default_value

        data["nodes"].append(node_data)

    for link in node_tree.links:
        data["links"].append(
            {
                "from_node": link.from_node.name,
                "from_socket": link.from_socket.name,
                "to_node": link.to_node.name,
                "to_socket": link.to_socket.name,
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


# ------------------------
# Import
# ------------------------


def _import_single_tree(node_tree, tree_data, groups_map):
    node_tree.nodes.clear()
    created = {}

    # Pass 1 – create every node and apply the properties that govern its
    # socket layout (operation, blend_type, data_type, …) BEFORE touching
    # inputs, so the correct sockets are present when we assign values.
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

        # Frame-specific settings (label_size, shrink).
        if node.bl_idname == "NodeFrame":
            frame_data = nd.get("frame", {})
            if frame_data:
                try:
                    if frame_data.get("label_size") is not None:
                        node.label_size = frame_data["label_size"]
                    node.shrink = frame_data.get("shrink", False)
                except Exception:
                    pass

        # Resolve node group reference.
        grp_name = nd.get("node_group_name")
        if grp_name and grp_name in groups_map:
            try:
                node.node_tree = groups_map[grp_name]
            except Exception:
                pass

        # *** Apply mode / operation props FIRST so sockets are correct. ***
        _apply_node_mode_properties(node, nd.get("node_props", {}))

        # Rebuild color ramp stops before assigning regular inputs.
        _apply_color_ramp(node, nd.get("color_ramp"))

        # Now assign input default values (sockets are in their final state).
        for inp_name, value in nd.get("inputs", {}).items():
            if inp_name not in node.inputs:
                continue
            try:
                node.inputs[inp_name].default_value = value
            except Exception:
                pass

        created[node.name] = node

    # Pass 2 – position frames BEFORE parenting children to them.
    for nd in tree_data.get("nodes", []):
        if nd.get("type") != "NodeFrame":
            continue
        node = created.get(nd.get("name"))
        if not node:
            continue
        try:
            node.location = nd.get("location", [0, 0])
        except Exception:
            pass

    # Pass 3 – parent child nodes to their frames.
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

    # Pass 4 – position every non-frame node.
    for nd in tree_data.get("nodes", []):
        if nd.get("type") == "NodeFrame":
            continue
        node = created.get(nd.get("name"))
        if not node:
            continue
        try:
            node.location = nd.get("location", [0, 0])
        except Exception:
            pass

    # Links
    for lnk in tree_data.get("links", []):
        try:
            node_tree.links.new(
                created[lnk["from_node"]].outputs[lnk["from_socket"]],
                created[lnk["to_node"]].inputs[lnk["to_socket"]],
            )
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
# Operators
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

        self.report({"INFO"}, "Exported node tree")
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

        self.report({"INFO"}, "Imported node tree")
        return {"FINISHED"}


# ------------------------
# UI Panel
# ------------------------


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


# ------------------------
# Register
# ------------------------

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
