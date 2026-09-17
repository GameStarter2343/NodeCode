# Licensed under the General Public License 3.0

import base64
import json
import lzma
import os
import re

import bpy  # pyright: ignore
import mathutils  # pyright: ignore
import tomllib
import requests

from bpy_extras.io_utils import ExportHelper  # pyright: ignore

from . import compress
# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


current_dir = os.path.dirname(__file__)
manifest_path = os.path.join(current_dir, "blender_manifest.toml")

version = "0.0.0"

try:
    with open(manifest_path, "rb") as f:
        data = tomllib.load(f)
        version = data.get("version", "0.0.0")
except Exception:
    pass


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

    for el, ed in zip(list(cr.elements), elements_data):
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


def _socket_default(sock):
    try:
        prop = sock.bl_rna.properties.get("default_value")
        if prop:
            return _get_prop_default(prop)
    except Exception:
        pass
    return None


def _export_sockets_sparse(sockets, connected_indices=None, include_default_value=True):
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

        if include_default_value and hasattr(sock, "default_value"):
            if connected_indices is None or i not in connected_indices:
                try:
                    current = _serialize_value(sock.default_value)
                    default = _socket_default(sock)
                    if default is None or _normalize_compare(
                        current
                    ) != _normalize_compare(default):
                        entry["dv"] = current
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
    data["tree_type"] = node_tree.bl_idname

    for link in node_tree.links:
        node = link.to_node
        for idx, sock in enumerate(node.inputs):
            if sock is link.to_socket:
                connected_inputs.setdefault(id(node), set()).add(idx)
                break

    for node in node_tree.nodes:
        ci = connected_inputs.get(id(node))

        node_data = {
            "i": node_index[node],
            "t": node.bl_idname,
            "l": [round(node.location.x, 1), round(node.location.y, 1)],
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

        inputs = _export_sockets_sparse(
            node.inputs, connected_indices=ci, include_default_value=True
        )
        outputs = _export_sockets_sparse(node.outputs, include_default_value=False)

        if inputs:
            node_data["inputs"] = inputs
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
        for _key in ("data_type", "operation", "mode", "blend_type"):
            if hasattr(node, _key):
                try:
                    _v = getattr(node, _key)
                    if isinstance(_v, str):
                        rna[_key] = _v
                except Exception:
                    pass

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


def export_node_tree_to_json(node_tree, context):
    groups = {}
    _collect_groups(node_tree, groups, set())

    data = {
        "version": version,
        "tree_type": node_tree.bl_idname,
        "main_tree": _export_single_tree(node_tree),
        "node_groups": groups,
    }

    string = json.dumps(data, separators=(",", ":"))

    compressed = ""

    if not context.scene.compress:
        compressed = string.encode('utf-8')

    match context.scene.compressAlg:
        case 'LZMA':
            compressed = compress.compress_lzma(string)

        case 'ZSTD':
            compressed = compress.compress_zstd(string)

    encoded = ""

    match context.scene.encodeAlg:
        case '64':
            encoded = base64.b64encode(compressed)

        case '85':
            encoded = base64.b85encode(compressed)

    return encoded





# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _import_single_tree(node_tree, tree_data, groups_map, context):
    created = {}

    for nd in tree_data.get("nodes", []):
        try:
            node = node_tree.nodes.new(nd["t"])
        except RuntimeError:
            continue
        node.name = nd.get("n", node.name)
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

        rna_props = nd.get("rna", {})
        if "data_type" in rna_props and hasattr(node, "data_type"):
            try:
                setattr(node, "data_type", rna_props["data_type"])
            except Exception:
                pass

        _apply_rna_properties(
            node,
            rna_props,
            skip=_NODE_EXPLICIT_PROPS,
        )

        _apply_color_ramp(node, nd.get("color_ramp"))

        _apply_sockets_any(node.inputs, nd.get("inputs"))
        _apply_sockets_any(node.outputs, nd.get("outputs"))

        node_id = nd.get("i")
        if node_id is not None:
            created[node_id] = node

    # Assign parenting first
    for nd in tree_data.get("nodes", []):
        parent_id = nd.get("parent")
        if parent_id is None:
            continue

        node = created.get(nd.get("i"))
        parent = created.get(parent_id)

        if node and parent:
            try:
                node.parent = parent
            except Exception:
                pass

    # Assign ALL locations only after parenting is finalized.
    # Blender recalculates child coordinates when parent is set,
    # including nested NodeFrame hierarchies.
    for nd in tree_data.get("nodes", []):
        node = created.get(nd.get("i"))

        if node:
            try:
                node.location = nd.get("l", [0, 0])
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


def import_node_tree_from_json(node_tree, json_data, context):
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
        _import_single_tree(groups_map[grp_name], grp_data, groups_map, context)

    _import_single_tree(node_tree, data["main_tree"], groups_map, context)


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

        context.window_manager.clipboard = export_node_tree_to_json(
            tree, context
        )
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

        context.window_manager.clipboard = export_node_tree_to_json(
            tree, context
        )
        self.report({"INFO"}, "Node tree exported to clipboard")
        return {"FINISHED"}

class NODECODE_OT_export_pastebin(bpy.types.Operator):
    bl_idname = "nodecode.export_pastebin"
    bl_label = "Pastebin"
    bl_description = "Export nodes to pastebin and copy link to clipboard"

    def execute(self, context):
        tree, err = get_active_node_tree(context)
        if err:
            self.report({"WARNING"}, err)
            return {"CANCELLED"}

        wm = context.window_manager
        payload = export_node_tree_to_json(
            tree, context
        )
        
        data = {
            'api_dev_key': context.scene.pastebinAPI.strip(),
            'api_option': 'paste',
            'api_paste_code': payload,
            'api_paste_expire_date': 'N'
        }
        
        try:
            response = requests.post("https://pastebin.com/api/api_post.php", data=data)
            
            if "Bad API request" in response.text:
                self.report({"WARNING"}, response.text)
                return {"CANCELLED"}
            
            wm.clipboard = response.text
            
        except Exception as e:
            self.report({"WARNING"}, f"Failed to upload to pastebin: {e}")
            return {"CANCELLED"}
        
        self.report({"INFO"}, "Node tree exported to pastebin, link copied to clipboard")
        return {"FINISHED"}

class NODECODE_OT_export_file(bpy.types.Operator, ExportHelper):
    bl_idname = "nodecode.export_file"
    bl_label = "Compact"
    bl_description = "Export Nodes to File in compact format"

    filename_ext = ".txt"

    def execute(self, context):
        tree, err = get_active_node_tree(context)
        if err:
            self.report({"WARNING"}, err)
            return {"CANCELLED"}

        data = export_node_tree_to_json(tree, context)
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                f.write(data)

        except Exception as e:
            self.report({"ERROR"}, f"Failed to save file: {e}")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Node tree exported to {self.filepath}")
        return {"FINISHED"}

class NODECODE_OT_export_file_pretty(bpy.types.Operator, ExportHelper):
    bl_idname = "nodecode.export_file_pretty"
    bl_label = "Readable"
    bl_description = "Export Nodes to File in human readable format"

    filename_ext = ".txt"

    def execute(self, context):
        tree, err = get_active_node_tree(context)
        if err:
            self.report({"WARNING"}, err)
            return {"CANCELLED"}

        data = export_node_tree_to_json(tree, context)
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                f.write(data)

        except Exception as e:
            self.report({"ERROR"}, f"Failed to save file: {e}")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Node tree exported to {self.filepath}")
        return {"FINISHED"}

class NODECODE_OT_import_buffer(bpy.types.Operator):
    bl_idname = "nodecode.import_buffer"
    bl_label = "Clipboard"
    bl_description = "Import Nodes from Clipboard"

    bypassVerCheck: bpy.props.BoolProperty(default=False)  # pyright: ignore

    _raw = None
    _data = None

    def invoke(self, context, event):
        self._raw, self._data = _decode_json(context.window_manager.clipboard.strip())

        addon_version = self._data.get("version", (0, 0, 0))

        current_ver = version
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
        if self._raw is None or self._data is None:
            self._raw, self._data = _decode_json(
                context.window_manager.clipboard.strip()
            )

        tree_type_hint = self._data.get("tree_type", "ShaderNodeTree")

        tree, err = get_active_node_tree(context)

        if err:
            tree, err = ensure_import_node_tree(context, tree_type_hint)

            if err:
                self.report({"WARNING"}, err)
                return {"CANCELLED"}

        import_node_tree_from_json(tree, self._raw, context)

        self._raw = None
        self._data = None
        self.bypassVerCheck = False

        bpy.ops.ed.undo_push(message="NodeCode: undo import text")
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

        current_ver = version
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

        import_node_tree_from_json(tree, raw, context)

        self.bypassVerCheck = False

        bpy.ops.ed.undo_push(message="NodeCode: undo import file")
        self.report({"INFO"}, "Node tree imported successfully")
        return {"FINISHED"}

class NODECODE_OT_import_pastebin(bpy.types.Operator):
    bl_idname = "nodecode.import_pastebin"
    bl_label = "pastebin"
    bl_description = "Import Nodes from Pastebin url (https://pastebin.com/<id>)"

    def execute(self, context):
            clipboard_text = context.window_manager.clipboard.strip()

            match = re.search(r"pastebin\.com/(?:raw/)?([a-zA-Z0-9]+)", clipboard_text)
            if not match:
                self.report(
                    {"ERROR"}, "Clipboard does not contain a valid Pastebin URL"
                )
                return {"CANCELLED"}

            paste_id = match.group(1)
            raw_url = f"https://pastebin.com/raw/{paste_id}"

            try:
                response = requests.get(raw_url, timeout=5)
                response.raise_for_status()
                payload = response.text
                raw, data = _decode_json(payload.strip())

                tree_type_hint = data.get("tree_type", "ShaderNodeTree")
                
                tree, err = get_active_node_tree(context)
            
                if err:
                    tree, err = ensure_import_node_tree(context, tree_type_hint)
            
                    if err:
                        self.report({"WARNING"}, err)
                        return {"CANCELLED"}
            
                import_node_tree_from_json(tree, raw, context)
            
                bpy.ops.ed.undo_push(message="NodeCode: undo import file")
                self.report({"INFO"}, "Node tree imported successfully")

                return {"FINISHED"}

            except requests.exceptions.HTTPError as e:
                self.report({"ERROR"}, f"Failed to fetch content. HTTP Status: {e.response.status_code}")
                return {"CANCELLED"}

            except requests.exceptions.RequestException as e:
                self.report({"ERROR"}, f"Network error: {str(e)}")
                return {"CANCELLED"}

            except Exception as e:
                self.report({"ERROR"}, f"Unexpected error: {str(e)}")
                return {"CANCELLED"}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class NODECODE_OT_check_pastebin(bpy.types.Operator):
    bl_idname = "nodecode.check_pastebin"
    bl_label = "Check Pastebin"
    bl_description = "Create test paste on pastebin to check if API key works"
    def execute(self, context):
        data = {
            'api_dev_key': context.scene.pastebinAPI.strip(),
            'api_option': 'paste',
            'api_paste_code': "API Validation Test",
            'api_paste_expire_date': '10M'
        }
        try:
            response = requests.post("https://pastebin.com/api/api_post.php", data=data)
            if "Bad API request" in response.text: 
                context.scene.api_key_valid = False
                print(response.text, "api key:", context.scene.pastebinAPI.strip())
            else: 
                context.scene.api_key_valid = True
        except requests.RequestException:
            context.scene.api_key_valid = False
        return {"FINISHED"}

# ---------------------------------------------------------------------------
# UI panel
# ---------------------------------------------------------------------------
class NODECODE_PT_main(bpy.types.Panel):
    bl_label = "NodeCode Converter"
    bl_idname = "NODECODE_PT_main"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "NodeCode"

    def draw(self, context):
        layout = self.layout
        space = context.space_data
        scene = context.scene
        layout.use_property_split = True
        layout.use_property_decorate = False

        tree = (space.edit_tree or space.node_tree) if space else None
        if not tree:
            layout.label(text="No active node tree", icon="ERROR")
            return

        info_box = layout.box()
        # ---------------------------------------------------------------
        # 1. HEADER: Info Label
        # ---------------------------------------------------------------
        header_row = info_box.row()
        header_row.alignment = "CENTER"
        header_row.label(text="Info", icon="INFO")

        # ---------------------------------------------------------------
        # 2. VERSIONS LINE
        # ---------------------------------------------------------------
        row = info_box.row()
        row.alignment = "CENTER"
        row.label(text=f"Blender: {".".join(map(str, bpy.app.version[:3]))}    |   Addon: {version}  ")


        # ---------------------------------------------------------------
        # 3. NODE TREE INFO HEADER
        # ---------------------------------------------------------------
        TREE_TYPE_LABELS = {
            "ShaderNodeTree": ("Shader Editor", "NODE_MATERIAL"),
            "GeometryNodeTree": ("Geometry Nodes", "GEOMETRY_NODES"),
            "CompositorNodeTree": ("Compositor", "NODE_COMPOSITING"),
            "TextureNodeTree": ("Texture Nodes", "NODE_TEXTURE"),
        }

        tree_info = TREE_TYPE_LABELS.get(
            tree.bl_idname, (tree.bl_idname, "NODETREE")
        )

        type_row = info_box.row()
        type_row.alignment = "CENTER"
        type_row.label(text=tree_info[0], icon=tree_info[1])

        # ---------------------------------------------------------------
        # 4. STATS GRID / COLUMNS
        # ---------------------------------------------------------------
        frames = sum(1 for n in tree.nodes if n.bl_idname == "NodeFrame")
        groups = sum(
            1 for n in tree.nodes if n.bl_idname in getattr(self, "GROUP_NODE_TYPES", ())
        )
        regular = len(tree.nodes) - frames

        items = [
            ("Nodes", regular, "NODE"),
            ("Links", len(tree.links), "LINKED"),
            ("Frames", frames, "OBJECT_DATA"),
            ("Groups", groups, "NODETREE"),
        ]

        grid_row = info_box.row(align=True)
        grid_row.alignment = "CENTER"
        for label_text, value, icon in items:
            col = grid_row.column(align=True)
            row = col.row()
            row.alignment = "CENTER"
            row.label(text=label_text)
            row = col.row()
            row.alignment = "CENTER"
            row.label(text=str(value), icon=icon)

        
        mode = scene.exportMode
        

        export_box = layout.box()

        row = export_box.row()
        row.operator(icon='EXPORT', operator="nodecode.export")
        row = export_box.row()
        row.alignment = "EXPAND"
        row.prop(scene, "exportMode")

        match mode:
            case 'E1':
                export_box.separator(factor=0)
                export_box.prop(scene, "compress")
                col = export_box.column(align=True)
                col.enabled = scene.compress
                col.row().prop(scene, "compressAlg")
                col.row().prop(scene, "encodeAlg")

            case 'E2': #wip
                row=export_box.row()
                row.alignment="CENTER"
                row.label(text="Work in progress")
            
            case 'E3':
                export_box.separator(factor=0)
                export_box.prop(scene, "pastebinAPI")
                export_box.prop(scene, "compress")
                col = export_box.column(align=True)
                col.enabled = scene.compress
                col.row().prop(scene, "compressAlg")
                col.row().prop(scene, "encodeAlg")

            case 'E4': #wip
                row=export_box.row()
                row.alignment="CENTER"
                row.label(text="Work in progress")


        import_box = layout.box()

        import_box.operator(icon='IMPORT', operator="nodecode.import_buffer")
        col = import_box.column()
        col.prop(scene, 'importMode')
        col.prop(scene, 'importReuseGroups')

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    NODECODE_OT_export,
    NODECODE_OT_export_pretty,
    NODECODE_OT_export_pastebin,
    NODECODE_OT_export_file,
    NODECODE_OT_export_file_pretty,
    NODECODE_OT_import_buffer,
    NODECODE_OT_import_file,
    NODECODE_OT_import_pastebin,
    NODECODE_OT_check_pastebin,
    NODECODE_PT_main,
)


def register():
    scene = bpy.types.Scene
    props = bpy.props

    scene.EraseNodes = props.BoolProperty(
        name="Erase Old Nodes", default=True
    )
    scene.exportMode = props.EnumProperty(
        name="Export Option", description="Select an option for exporting node tree", items=[
            ('E1', 'Clipboard', f"Export nodes as a JSON and save result to clipboard. \nLeast compact of all options"),
            ('E2', 'File', f"Export nodes as a JSON and save result to file. \nNot sure why you may want to use it since sharing pure text is easier than a file"),
            ('E3', 'Link (Pastebin)', f"Export nodes as a JSON, create paste at Pastebin and save link to clipboard. \nProbably the best variant since it's fast, simple and pastebin allows paste to be available indefinitely long. \n\nPLEASE NOTE: You need to provide your developer api key in order to create pastes, on free accounts pastes are limited to 20 per day"),
            ('E4', 'Link (x0.at)', f"Export nodes as a JSON, create paste at x0.at and save link to clipboard. \nMore like a fallback from Pastebin, paste's lifetime is limited to a year. Use only if you have reached daily limit or don't want to show your api key."),
        ], 
    )

    scene.compress = props.BoolProperty(
        name="Compress", default=True
    )

    scene.compressAlg = props.EnumProperty(
        name="Compressor", description="What compression algorithm to use for compressing JSON", items=[
            ('LZMA', 'LZMA', f"Use LZMA (Lempel-Ziv-Markov chain algorithm) for compressing JSON."),
            ('ZSTD', 'Zstd', f"Use Zstandard for compressing JSON.\nSupports custom pre-trained dictionary"),
        ]
    )
    scene.encodeAlg = props.EnumProperty( 
        name="Encoder", description="What encoding algorithm to use for turning data into text", items=[
            ('64', 'Base64', f"Use Base64 for encoding JSON. \nAdds about 33% but entirely safe for formatting"),
            ('85', 'Base85', f"Use Base85 for encoding JSON. \nAdds about 25% but may conflict with formatting"),
        ]
    )

    scene.importMode = props.EnumProperty(
        name="Import Option", description="Select an option for importing a node tree", items=[
            ('REP', 'Replace', "Replace old node tree with imported one"),
            ('ADD', 'Add', "Place nodes alongside old tree"),
            ('NEW', 'New tree', "Create new material/geo/compositor node tree")
        ]
    )
    scene.importReuseGroups = props.EnumProperty(
        name="Name Conflict", description="What should addon do to resolve name conflicts between imported and existing nodes", items=[
            ('NEW', 'Rename New', "Rename imported nodes that conflict with imported ones"),
            ('OLD', 'Rename Old', "Rename existing nodes that conflict with imported ones"),
            ('DEL', 'Delete', "Delete existing nodes that conflict with imported ones"),
            ('USE', 'Use Groups', "Use existing node groups if their name matches imported group, rename standard nodes.\nThis mode can create conflicts or errors if you have a different node named just like imported one.\nIf material/geometry/compositor looks weird, try selecting other mode first"),
        ], default='USE',
    )

    bpy.types.Scene.pastebinAPI = bpy.props.StringProperty(
        name="Pastebin API key",
        description="Developer api key from your pastebin account.\nAPI key is required to create pastes",
        subtype='PASSWORD'
    )
    bpy.types.Scene.api_key_valid = bpy.props.BoolProperty(
        name="Api key status"
    )

    for cls in classes:
        bpy.utils.register_class(cls)  


def unregister():
    scene = bpy.types.Scene

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    del scene.EraseNodes
    del scene.exportMode
    del scene.compress
    del scene.compressAlg
    del scene.encodeAlg
    del scene.importMode
    del scene.importReuseGroups


    del bpy.types.Scene.pastebinAPI
    del bpy.types.Scene.api_key_valid

if __name__ == "__main__":
    with open("blender_manifest.toml", "rb") as f:
        data = tomllib.load(f)

    version = data.get("version")
    register()
