bl_info = {
    "name": "NodeCode Converter",
    "author": "GameStarter2343",
    "version": (0, 1, 0),
    "blender": (2, 93, 0),
    "location": "Node Editor > SideBar > NodeCode",
    "description": "A tool designed to export/import complex node groups with ease",
    "category": "Node",
}

import json

import bpy
import mathutils

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GROUP_NODE_TYPES = {
    "ShaderNodeGroup",
    "GeometryNodeGroup",
    "CompositorNodeGroup",
    "TextureNodeGroup",
}

# Properties handled explicitly during export/import; excluded from the
# generic RNA round-trip to avoid double-application or ordering conflicts.
_NODE_EXPLICIT_PROPS = frozenset(
    {
        "name",
        "label",
        "location",
        "width",
        "height",
        "hide",
        "mute",
        "color",
        "use_custom_color",
        "parent",
        "node_tree",
    }
)

_SOCKET_EXPLICIT_PROPS = frozenset({"default_value"})


# ---------------------------------------------------------------------------
# Generic RNA helpers
# ---------------------------------------------------------------------------


def _serialize_value(value):
    """Recursively convert a Blender RNA value to a JSON-safe type."""
    if isinstance(value, (int, float, str, bool)):
        return value

    if isinstance(
        value,
        (mathutils.Vector, mathutils.Color, mathutils.Euler, mathutils.Quaternion),
    ):
        return list(value)

    # Catch-all for array-like objects (e.g. bpy_prop_array).
    if hasattr(value, "__len__") and not isinstance(value, str):
        try:
            return [_serialize_value(v) for v in value]
        except Exception:
            pass

    # ID datablocks (Image, NodeTree, …) – store name + type for reference.
    if isinstance(value, bpy.types.ID):
        return {"__id__": value.name, "__type__": value.__class__.__name__}

    return str(value)


def _serialize_rna_properties(obj, skip=frozenset()):
    """Return a dict of all writable RNA properties on *obj*, skipping *skip*."""
    props = {}
    for prop in obj.bl_rna.properties:
        identifier = prop.identifier
        if identifier in {"rna_type"} or identifier in skip:
            continue
        if prop.is_readonly:
            continue
        try:
            props[identifier] = _serialize_value(getattr(obj, identifier))
        except Exception:
            pass
    return props


def _apply_rna_properties(obj, data, skip=frozenset()):
    """Apply a previously serialised RNA dict back to *obj*.

    Lists are tried first as-is; if that fails they are converted to tuples,
    which is required by many Blender colour / vector RNA properties.
    """
    for key, value in data.items():
        if key in skip or not hasattr(obj, key):
            continue
        # Skip ID-datablock placeholders – they cannot be resolved generically.
        if isinstance(value, dict) and "__id__" in value:
            continue
        try:
            setattr(obj, key, value)
        except TypeError:
            if isinstance(value, list):
                try:
                    setattr(obj, key, tuple(value))
                except Exception:
                    pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Colour-ramp helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Socket helpers
# ---------------------------------------------------------------------------


def _export_sockets_full(sockets):
    """Serialise all sockets including their default values.

    ``default_value`` is stored under its own top-level key and excluded from
    the generic RNA dict to prevent double-application on import.
    """
    result = []
    for sock in sockets:
        sock_data = {
            "name": sock.name,
            "type": sock.bl_idname,
            "rna": _serialize_rna_properties(sock, skip=_SOCKET_EXPLICIT_PROPS),
        }
        if hasattr(sock, "default_value"):
            try:
                sock_data["default_value"] = _serialize_value(sock.default_value)
            except Exception:
                pass
        result.append(sock_data)
    return result


def _apply_sockets_full(sockets, data):
    for sock, sock_data in zip(sockets, data):
        _apply_rna_properties(sock, sock_data.get("rna", {}))

        if "default_value" in sock_data:
            value = sock_data["default_value"]
            try:
                sock.default_value = value
            except TypeError:
                if isinstance(value, list):
                    try:
                        sock.default_value = tuple(value)
                    except Exception:
                        pass
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _export_single_tree(node_tree):
    data = {"nodes": [], "links": []}
    node_index = {node: i for i, node in enumerate(node_tree.nodes)}

    for node in node_tree.nodes:
        node_data = {
            "id": node_index[node],
            "name": node.name,
            "label": node.label,
            "type": node.bl_idname,
            "location": [node.location.x, node.location.y],
            "width": node.width,
            "hide": node.hide,
            "mute": node.mute,
            # Colour stored only when a custom colour is active.
            "color": list(node.color) if node.use_custom_color else None,
            "inputs": _export_sockets_full(node.inputs),
            "outputs": _export_sockets_full(node.outputs),
            "parent": node_index[node.parent] if node.parent else None,
            # Generic RNA for node-type-specific properties; explicitly-handled
            # props are excluded so they are not applied twice on import.
            "rna": _serialize_rna_properties(node, skip=_NODE_EXPLICIT_PROPS),
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
                "f": node_index[link.from_node],
                "fs": list(link.from_node.outputs).index(link.from_socket),
                "t": node_index[link.to_node],
                "ts": list(link.to_node.inputs).index(link.to_socket),
            }
        )

    return data


def _collect_groups(node_tree, groups_out, visited):
    """Recursively gather all nested node groups."""
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
        "version": bl_info["version"],
        "tree_type": node_tree.bl_idname,
        "main_tree": _export_single_tree(node_tree),
        "node_groups": groups,
    }
    return json.dumps(result, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _import_single_tree(node_tree, tree_data, groups_map):
    node_tree.nodes.clear()
    created = {}

    # --- Pass 1: create nodes and apply all non-spatial properties ----------
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
            try:
                node.color = colour
            except TypeError:
                node.color = tuple(colour)

        if node.bl_idname == "NodeFrame":
            frame_data = nd.get("frame", {})
            if frame_data:
                try:
                    if frame_data.get("label_size") is not None:
                        node.label_size = frame_data["label_size"]
                    node.shrink = frame_data.get("shrink", False)
                except Exception:
                    pass

        # Assign the group node's inner tree before touching sockets so that
        # the socket list matches what the group exposes.
        grp_name = nd.get("node_group_name")
        if grp_name and grp_name in groups_map:
            try:
                node.node_tree = groups_map[grp_name]
            except Exception:
                pass

        # Generic RNA (node-type-specific props; explicit props already done).
        _apply_rna_properties(node, nd.get("rna", {}), skip=_NODE_EXPLICIT_PROPS)
        _apply_color_ramp(node, nd.get("color_ramp"))
        _apply_sockets_full(node.inputs, nd.get("inputs", []))
        _apply_sockets_full(node.outputs, nd.get("outputs", []))

        node_id = nd.get("id")
        if node_id is not None:
            created[node_id] = node

    # --- Pass 2: locate frames first (children need parents to exist) -------
    for nd in tree_data.get("nodes", []):
        if nd.get("type") != "NodeFrame":
            continue
        node = created.get(nd.get("id"))
        if node:
            try:
                node.location = nd.get("location", [0, 0])
            except Exception:
                pass

    # --- Pass 3: assign parent frames ---------------------------------------
    for nd in tree_data.get("nodes", []):
        parent_id = nd.get("parent")
        if not parent_id:
            continue
        node = created.get(nd.get("id"))
        parent = created.get(parent_id)
        if node and parent:
            try:
                node.parent = parent
            except Exception:
                pass

    # --- Pass 4: locate non-frame nodes (after parenting so offsets work) --
    for nd in tree_data.get("nodes", []):
        if nd.get("type") == "NodeFrame":
            continue
        node = created.get(nd.get("id"))
        if node:
            try:
                node.location = nd.get("location", [0, 0])
            except Exception:
                pass

    # --- Pass 5: recreate links --------------------------------------------
    for lnk in tree_data.get("links", []):
        try:
            from_node = created.get(lnk["f"])
            to_node = created.get(lnk["t"])
            if not from_node or not to_node:
                continue

            from_idx = lnk.get("fs")
            to_idx = lnk.get("ts")
            if (
                from_idx is not None
                and to_idx is not None
                and from_idx < len(from_node.outputs)
                and to_idx < len(to_node.inputs)
            ):
                node_tree.links.new(
                    from_node.outputs[from_idx],
                    to_node.inputs[to_idx],
                )
                continue

            # Fallback: match by socket name (supports older exports).
            from_socket = from_node.outputs.get(lnk.get("from_socket_name"))
            to_socket = to_node.inputs.get(lnk.get("to_socket_name"))
            if from_socket and to_socket:
                node_tree.links.new(from_socket, to_socket)

        except Exception:
            pass


def import_node_tree_from_json(node_tree, json_data):
    data = json.loads(json_data)

    # Support bare single-tree exports (no wrapper object).
    if "main_tree" not in data:
        data = {
            "tree_type": node_tree.bl_idname,
            "main_tree": data,
            "node_groups": {},
        }

    groups_map = {}

    # Build / reuse existing node groups. Each group carries its own
    # bl_idname in the export so we use that rather than the main tree type.
    for grp_name, grp_data in data.get("node_groups", {}).items():
        grp_type = grp_data.get("tree_type", data.get("tree_type", "ShaderNodeTree"))
        existing = bpy.data.node_groups.get(grp_name)
        if existing and existing.bl_idname == grp_type:
            groups_map[grp_name] = existing
        else:
            groups_map[grp_name] = bpy.data.node_groups.new(grp_name, grp_type)

    # Populate groups before the main tree so group nodes can reference them.
    for grp_name, grp_data in data.get("node_groups", {}).items():
        _import_single_tree(groups_map[grp_name], grp_data, groups_map)

    _import_single_tree(node_tree, data["main_tree"], groups_map)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


class NODECODE_OT_export(bpy.types.Operator):
    bl_idname = "nodecode.export"
    bl_label = "Export Nodes to Clipboard"

    def execute(self, context):
        tree, err = get_active_node_tree(context)
        if err:
            self.report({"WARNING"}, err)
            return {"CANCELLED"}

        context.window_manager.clipboard = export_node_tree_to_json(tree)
        self.report({"INFO"}, "Node tree exported to clipboard")
        return {"FINISHED"}


class NODECODE_OT_import_buffer(bpy.types.Operator):
    bl_idname = "nodecode.import_buffer"
    bl_label = "Import Nodes from Clipboard"

    def execute(self, context):
        raw = context.window_manager.clipboard
        try:
            data = json.loads(raw)
        except Exception:
            self.report({"ERROR"}, "Clipboard does not contain valid JSON")
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


# ---------------------------------------------------------------------------
# UI panel
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

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
