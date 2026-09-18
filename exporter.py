# Licensed under the General Public License 3.0

import bpy # pyright: ignore
import mathutils # pyright: ignore
import requests
import base64
import json

from . import utils
from . import compress

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

        if node.bl_idname in utils._GROUP_NODE_TYPES:
            grp = getattr(node, "node_tree", None)
            if grp:
                node_data["node_group_name"] = grp.name

        rna = _serialize_rna_diff(node, skip=utils._NODE_EXPLICIT_PROPS)
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
        if node.bl_idname not in utils._GROUP_NODE_TYPES:
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
        "version": utils.version,
        "tree_type": node_tree.bl_idname,
        "main_tree": _export_single_tree(node_tree),
        "node_groups": groups,
    }

    string = json.dumps(data, separators=(",", ":"))

    if not context.scene.compress or context.scene.exportMode == 'LINK':
        return string.encode('utf-8')

    match context.scene.compressAlg:
        case 'LZMA':
            compressed = compress.compress_lzma(string)

        case 'ZSTD':
            compressed = compress.compress_zstd(string)

        case _:
            raise ValueError(f"Unsupported compression algorithm: {context.scene.compressAlg}")

    match context.scene.encodeAlg:
        case '64':
            encoded = base64.b64encode(compressed)

        case '85':
            encoded = base64.b85encode(compressed)

        case _:
            raise ValueError(f"Unsupported encoding algorithm: {context.scene.encodeAlg}")

    return encoded

class NODECODE_OT_export(bpy.types.Operator):
    bl_idname = "nodecode.export"
    bl_label = "Export"
    bl_description = "Export Nodes"

    def execute(self, context):
        tree, err = utils.get_active_node_tree(context)
        if err:
            self.report({"WARNING"}, err)
            return {"CANCELLED"}

        try: payload = export_node_tree_to_json(tree, context)
        except ValueError as e: 
            print(f"Error while exporting tree: {e}")
            return

        match context.scene.exportMode:
            case 'CLIP':
                context.window_manager.clipboard = payload

            case 'FILE': #wip
                print('Work in progress')

            case 'LINK':
                data = {
                    'api_dev_key': context.scene.pastebinAPI.strip(),
                    'api_option': 'paste',
                    'api_paste_code': payload,
                    'api_paste_expire_date': 'N'
                }

                print(data)

                try:
                    response = requests.post("https://pastebin.com/api/api_post.php", data=data)

                    print(response, response.text)

                    if "Bad API request" in response.text:
                        self.report({"WARNING"}, response.text)
                        return {"CANCELLED"}
                    
                    context.window_manager.clipboard = response.text
                    
                except Exception as e:
                    self.report({"WARNING"}, f"Failed to upload to pastebin: {e}")
                    return {"CANCELLED"}

            case 'WIP': # wip
                print('Work in progress')
            
        self.report({"INFO"}, "Node tree exported")
        return {"FINISHED"}