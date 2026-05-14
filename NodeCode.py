# Copyright 2026 GameStarter2343
# Licensed under the Apache License 2.0

bl_info = {
    "name": "NodeCode Converter",
    "author": "GameStarter2343",
    "version": (1, 5, 1),
    "blender": (2, 93, 0),
    "location": "Node Editor > SideBar > NodeCode",
    "description": "A tool designed to export/import complex node groups with ease",
    "category": "Node",
}

import base64
import json
import lzma

import bpy  # pyright: ignore
import mathutils  # pyright: ignore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GROUP_NODE_TYPES = {
    "ShaderNodeGroup",
    "GeometryNodeGroup",
    "CompositorNodeGroup",
    "TextureNodeGroup",
}

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
        "bl_idname",
        "bl_label",
        "bl_description",
        "bl_icon",
        "bl_width_default",
        "bl_width_min",
        "bl_width_max",
        "bl_height_default",
        "bl_height_min",
        "bl_height_max",
        "location_absolute",
        "select",
        "show_preview",
        "show_texture",
        "show_options",
    }
)

ID_SAFE_PROPS = {"node_tree", "image", "material", "texture", "world", "object"}

# ---------------------------------------------------------------------------
# Generic RNA helpers
# ---------------------------------------------------------------------------


def _decode_json(raw):
    try:
        if not raw.startswith("{"):
            raw = lzma.decompress(base64.a85decode(raw.encode("ascii")))
            raw = raw.decode("utf-8")

        data = json.loads(raw)
        return raw, data

    except Exception:
        raise ValueError("Invalid JSON payload")


def _resolve_id(value):
    """Resolve serialized Blender ID reference back to actual datablock."""
    if not isinstance(value, dict):
        return value

    if "__id__" not in value:
        return value

    name = value.get("__id__")
    id_type = value.get("__type__")

    try:
        # Node trees (Geometry/Shader/Compositor groups)
        if id_type == "NodeTree" or id_type == "NodeGroup":
            return bpy.data.node_groups.get(name)

        if id_type == "Image":
            return bpy.data.images.get(name)

        if id_type == "Material":
            return bpy.data.materials.get(name)

        if id_type == "Texture":
            return bpy.data.textures.get(name)

        if id_type == "World":
            return bpy.data.worlds.get(name)

        if id_type == "Object":
            return bpy.data.objects.get(name)

        # Fallback: try any ID datablock collections generically
        for collection in (
            bpy.data.node_groups,
            bpy.data.images,
            bpy.data.materials,
            bpy.data.textures,
            bpy.data.worlds,
            bpy.data.objects,
        ):
            obj = collection.get(name)
            if obj:
                return obj

    except Exception:
        return None

    return None


def _round_floats(value, decimals=5):
    """Recursively round floats in a JSON-safe structure for compactness."""
    if isinstance(value, float):
        return round(value, decimals)
    if isinstance(value, list):
        return [_round_floats(v, decimals) for v in value]
    return value


def _normalize_compare(v):
    if isinstance(v, (list, tuple)):
        return tuple(_normalize_compare(x) for x in v)
    if isinstance(v, float):
        return round(v, 5)
    return v


def _serialize_value(value):
    """Recursively convert a Blender RNA value to a JSON-safe type."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 5)
    if isinstance(value, str):
        return value

    if isinstance(
        value,
        (mathutils.Vector, mathutils.Color, mathutils.Euler, mathutils.Quaternion),
    ):
        return [round(v, 5) for v in value]

    # Catch-all for array-like objects (e.g. bpy_prop_array).
    if hasattr(value, "__len__") and not isinstance(value, str):
        try:
            return [_serialize_value(v) for v in value]
        except Exception:
            pass

    if isinstance(value, bpy.types.ID):
        return {"__id__": value.name, "__type__": value.__class__.__name__}  # pyright: ignore

    return str(value)


def _get_prop_default(prop):
    """Return the RNA default value for a property, or None if unavailable."""
    try:
        if hasattr(prop, "default_array") and len(prop.default_array) > 0:
            return list(prop.default_array)

        if hasattr(prop, "default"):
            return prop.default
    except Exception:
        pass
    return None


def _serialize_rna_diff(obj, skip=frozenset()):
    """Serialize only RNA properties that differ from their defaults."""
    props = {}
    rna = obj.bl_rna

    for prop in rna.properties:
        key = prop.identifier

        if key in {"rna_type"} or key in skip:
            continue
        if prop.is_readonly:
            continue

        try:
            value = getattr(obj, key)
        except Exception:
            continue

        default = _get_prop_default(prop)
        if default is not None:
            serialized = _serialize_value(value)
            if _normalize_compare(serialized) == _normalize_compare(default):
                continue

        props[key] = _serialize_value(value)

    return props


def _apply_rna_properties(obj, data, skip=frozenset()):
    """Apply a previously serialised RNA dict back to *obj*."""
    for key, value in data.items():
        if key in skip or not hasattr(obj, key):
            continue

        if key in ID_SAFE_PROPS and isinstance(value, dict):
            value = _resolve_id(value)
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
            {
                "position": round(el.position, 5),
                "color": [round(c, 5) for c in el.color],
            }
            for el in cr.elements
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


def _export_sockets_sparse(sockets, connected_indices=None):
    """Export only sockets with non-default state, keyed by index string.

    ``connected_indices`` is a set of input socket indices whose default_value
    can be skipped because a link will overwrite them at render time.

    Returns None (omitted from JSON) when every socket is at its default.
    """
    result = {}
    for i, sock in enumerate(sockets):
        entry = {}

        for key, default in {
            "hide": False,
            "enabled": True,
            "hide_value": False,
        }.items():
            val = getattr(sock, key, None)
            if val != default:
                entry[key] = val

        if hasattr(sock, "default_value") and (
            connected_indices is None or i not in connected_indices
        ):
            try:
                entry["dv"] = _serialize_value(sock.default_value)
            except Exception:
                pass

        if entry:
            result[str(i)] = entry

    return result or None


def _apply_sockets_sparse(sockets, data):
    """Apply sparse socket data (index-keyed dict) back to a socket list."""
    for idx_str, sock_data in data.items():
        try:
            sock = sockets[int(idx_str)]
        except (IndexError, ValueError):
            continue

        for key in ("hide", "enabled", "hide_value"):
            if key in sock_data:
                try:
                    setattr(sock, key, sock_data[key])
                except Exception:
                    pass

        if "dv" in sock_data:
            value = sock_data["dv"]
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


def _apply_sockets_full(sockets, data):
    """Apply legacy dense socket list (list of dicts with 'default_value')."""
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


def _apply_sockets_any(sockets, data):
    """Dispatch to sparse or legacy dense handler based on data shape."""
    if data is None:
        return
    if isinstance(data, dict):
        _apply_sockets_sparse(sockets, data)
    elif isinstance(data, list):
        _apply_sockets_full(sockets, data)


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

    connected_inputs = {}
    for link in node_tree.links:
        node = link.to_node
        for idx, sock in enumerate(node.inputs):
            if sock is link.to_socket:
                connected_inputs.setdefault(id(node), set()).add(idx)
                break

    for node in node_tree.nodes:
        ci = connected_inputs.get(id(node))

        node_data = {
            "id": node_index[node],
            "name": node.name,
            "type": node.bl_idname,
            "location": [round(node.location.x, 2), round(node.location.y, 2)],
        }

        if node.label:
            node_data["label"] = node.label
        if node.width != 140.0:
            node_data["width"] = node.width
        if node.hide:
            node_data["hide"] = True
        if node.mute:
            node_data["mute"] = True
        if node.use_custom_color:
            node_data["color"] = [round(c, 5) for c in node.color]
        if node.parent:
            node_data["parent"] = node_index[node.parent]

        inputs = _export_sockets_sparse(node.inputs, connected_indices=ci)
        if inputs:
            node_data["inputs"] = inputs

        outputs = _export_sockets_sparse(node.outputs)
        if outputs:
            node_data["outputs"] = outputs

        if node.bl_idname == "NodeFrame":
            frame_data = {}
            ls = getattr(node, "label_size", None)
            if ls is not None:
                frame_data["label_size"] = ls
            shrink = getattr(node, "shrink", False)
            if shrink:
                frame_data["shrink"] = True
            if frame_data:
                node_data["frame"] = frame_data

        cr_data = _export_color_ramp(node)
        if cr_data is not None:
            node_data["color_ramp"] = cr_data

        if node.bl_idname in GROUP_NODE_TYPES:
            grp = getattr(node, "node_tree", None)
            if grp:
                node_data["node_group_name"] = grp.name

        rna = _serialize_rna_diff(node, skip=_NODE_EXPLICIT_PROPS)
        if rna:
            node_data["rna"] = rna

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


def export_node_tree_to_json(node_tree, compact=False):
    groups = {}
    _collect_groups(node_tree, groups, set())

    result = {
        "version": bl_info["version"],
        "tree_type": node_tree.bl_idname,
        "main_tree": _export_single_tree(node_tree),
        "node_groups": groups,
    }

    if not compact:
        return json.dumps(result, separators=(",", ":"))

    payload = json.dumps(result, separators=(",", ":")).encode("utf-8")

    compressed = lzma.compress(payload, preset=9)

    return base64.a85encode(compressed).decode("ascii")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _import_single_tree(node_tree, tree_data, groups_map):
    node_tree.nodes.clear()
    created = {}

    for nd in tree_data.get("nodes", []):
        try:
            node = node_tree.nodes.new(nd["type"])
        except RuntimeError:
            continue

        node.name = nd.get("name", node.name)
        node.width = nd.get("width", 140.0)
        node.hide = nd.get("hide", False)
        node.mute = nd.get("mute", False)
        node.label = nd.get("label", "")

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

        grp_name = nd.get("node_group_name")
        if grp_name and grp_name in groups_map:
            try:
                node.node_tree = groups_map[grp_name]
            except Exception:
                pass

        _apply_rna_properties(node, nd.get("rna", {}), skip=_NODE_EXPLICIT_PROPS)
        _apply_color_ramp(node, nd.get("color_ramp"))

        _apply_sockets_any(node.inputs, nd.get("inputs"))
        _apply_sockets_any(node.outputs, nd.get("outputs"))

        node_id = nd.get("id")
        if node_id is not None:
            created[node_id] = node

    for nd in tree_data.get("nodes", []):
        if nd.get("type") != "NodeFrame":
            continue
        node = created.get(nd.get("id"))
        if node:
            try:
                node.location = nd.get("location", [0, 0])
            except Exception:
                pass

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

    for nd in tree_data.get("nodes", []):
        if nd.get("type") == "NodeFrame":
            continue
        node = created.get(nd.get("id"))
        if node:
            try:
                node.location = nd.get("location", [0, 0])
            except Exception:
                pass

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

    groups_map = {}

    for grp_name, grp_data in data.get("node_groups", {}).items():
        grp_type = grp_data.get("tree_type", data.get("tree_type", "ShaderNodeTree"))
        existing = bpy.data.node_groups.get(grp_name)
        if existing and existing.bl_idname == grp_type:
            groups_map[grp_name] = existing
        else:
            groups_map[grp_name] = bpy.data.node_groups.new(grp_name, grp_type)

    for grp_name, grp_data in data.get("node_groups", {}).items():
        _import_single_tree(groups_map[grp_name], grp_data, groups_map)

    _import_single_tree(node_tree, data["main_tree"], groups_map)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


class NODECODE_OT_export(bpy.types.Operator):
    bl_idname = "nodecode.export"
    bl_label = "Compact"
    bl_description = "Export Nodes to Clipboard in compact format"

    def execute(self, context):
        tree, err = get_active_node_tree(context)
        if err:
            self.report({"WARNING"}, err)
            return {"CANCELLED"}

        context.window_manager.clipboard = export_node_tree_to_json(tree, True)
        self.report({"INFO"}, "Node tree exported to clipboard")
        return {"FINISHED"}


class NODECODE_OT_export_pretty(bpy.types.Operator):
    bl_idname = "nodecode.export_pretty"
    bl_label = "Readable"
    bl_description = "Export Nodes to Clipboard in human readable format"

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
    bl_label = "Clipboard"

    bypassVerCheck: bpy.props.BoolProperty(default=False)  # pyright: ignore

    def invoke(self, context, event):
        raw, data = _decode_json(context.window_manager.clipboard.strip())

        addon_version = data.get("version", (0, 0, 0))

        current_ver = ".".join(map(str, bl_info["version"]))
        imported_ver = ".".join(map(str, addon_version))

        if not self.bypassVerCheck and imported_ver != current_ver:
            self.bypassVerCheck = True

            return context.window_manager.invoke_confirm(
                self,
                event,
                title="Version Mismatch",
                message=f"{imported_ver} != {current_ver}\n Do you want to continue?",
                icon="WARNING",
            )

        return self.execute(context)

    def execute(self, context):
        raw, data = _decode_json(context.window_manager.clipboard.strip())

        tree_type_hint = data.get("tree_type", "ShaderNodeTree")

        tree, err = get_active_node_tree(context)

        if err:
            tree, err = ensure_import_node_tree(context, tree_type_hint)

            if err:
                self.report({"WARNING"}, err)
                return {"CANCELLED"}

        import_node_tree_from_json(tree, raw)

        # reset for next operator run
        self.bypassVerCheck = False

        self.report({"INFO"}, "Node tree imported successfully")
        return {"FINISHED"}


class NODECODE_OT_import_file(bpy.types.Operator):
    bl_idname = "nodecode.import_file"
    bl_label = "File"
    bl_description = "Import Nodes from File (.txt .json)"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")  # pyright: ignore
    filter_glob: bpy.props.StringProperty(  # pyright: ignore
        default="*.json;*.txt",
        options={"HIDDEN"},
    )

    bypassVerCheck: bpy.props.BoolProperty(default=False)  # pyright: ignore

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                raw = f.read()
        except Exception as e:
            self.report({"ERROR"}, f"Could not read file: {e}")
            return {"CANCELLED"}

        try:
            raw, data = _decode_json(raw.strip())
        except Exception:
            self.report({"ERROR"}, "File does not contain valid JSON")
            return {"CANCELLED"}

        addon_version = data.get("version", (0, 0, 0))

        current_ver = ".".join(map(str, bl_info["version"]))
        imported_ver = ".".join(map(str, addon_version))

        if not self.bypassVerCheck and imported_ver != current_ver:
            self.bypassVerCheck = True

            return context.window_manager.invoke_confirm(
                self,
                None,
                title="Version Mismatch",
                message=f"{imported_ver} != {current_ver}\nDo you want to continue?",
                icon="WARNING",
            )

        tree_type_hint = data.get("tree_type", "ShaderNodeTree")

        tree, err = get_active_node_tree(context)

        if err:
            tree, err = ensure_import_node_tree(context, tree_type_hint)

            if err:
                self.report({"WARNING"}, err)
                return {"CANCELLED"}

        import_node_tree_from_json(tree, raw)

        self.bypassVerCheck = False

        self.report({"INFO"}, "Node tree imported successfully")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# UI panel
# ---------------------------------------------------------------------------


class NODECODE_PT_panel(bpy.types.Panel):
    bl_label = "NodeCode Converter"
    bl_idname = "NODECODE_PT_panel"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "NodeCode"

    def draw(self, context):
        layout = self.layout
        space = context.space_data

        tree = (space.edit_tree or space.node_tree) if space else None
        if tree:
            TREE_TYPE_LABELS = {
                "ShaderNodeTree": ("Shader", "NODE_MATERIAL"),
                "GeometryNodeTree": ("Geometry", "GEOMETRY_NODES"),
                "CompositorNodeTree": ("Compositor", "NODE_COMPOSITING"),
                "TextureNodeTree": ("Texture", "NODE_TEXTURE"),
            }
            label = TREE_TYPE_LABELS.get(tree.bl_idname, tree.bl_idname)
            layout.label(text=label[0], icon=label[1])  # pyright: ignore

            frames = sum(1 for n in tree.nodes if n.bl_idname == "NodeFrame")
            groups = sum(1 for n in tree.nodes if n.bl_idname in GROUP_NODE_TYPES)
            regular = len(tree.nodes) - frames - groups

            row = layout.row(align=True)
            row.alignment = "EXPAND"

            items = [
                ("Nodes", regular, "NODE"),
                ("Links", len(tree.links), "LINKED"),
                ("Frames", frames, "OBJECT_DATA"),
                ("Groups", groups, "NODETREE"),
            ]

            for label_text, value, ICON in items:
                col = row.column(align=True)
                col.alignment = "CENTER"

                col.label(text=label_text, icon="NONE")
                col.label(text=f"{value}", icon=ICON)

            layout.separator()

            row = layout.row()
            row.alignment = "CENTER"
            row.label(text="Export Nodes To Code", icon="COPYDOWN")

            row = layout.row(align=True)
            row.operator("nodecode.export")
            row.operator("nodecode.export_pretty")

            row = layout.row()
            row.alignment = "CENTER"
            row.label(text="Import Nodes To Code", icon="PASTEDOWN")

            row = layout.row(align=True)
            row.operator("nodecode.import_buffer")
            row.operator("nodecode.import_file")

        else:
            layout.label(text="No active node tree", icon="ERROR")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    NODECODE_OT_export,
    NODECODE_OT_export_pretty,
    NODECODE_OT_import_buffer,
    NODECODE_OT_import_file,
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
