# Licensed under the General Public License 3.0

import bpy # pyright: ignore
import mathutils # pyright: ignore
import requests
import base64
import json
import time

from . import utils
from . import compress

def _decode_json(payload):
    payload_bytes = payload.encode('utf-8') if isinstance(payload, str) else payload
    decoded = None
    
    for decoder in (base64.b64decode, base64.b85decode):
        try:
            decoded = decoder(payload_bytes)
            break
        except Exception:
            continue

    if decoded is None:
        print("Error while decoding payload: All decoders failed.")
        return None

    decompressed = None
    for decompressor in (compress.decompress_zstd, compress.decompress_lzma):
        try:
            decompressed = decompressor(decoded)
            break
        except Exception:
            continue

    if decompressed is None:
        print("Error while decompressing payload: All decompressors failed.")
        return None

    try:
        return json.loads(decompressed)
    except Exception as e:
        print("Error while parsing JSON:", e)
        return None
    
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

def _apply_rna_properties(obj, data, skip=frozenset()):
    """Apply a previously serialised RNA dict back to *obj*."""
    for key, value in data.items():
        if key in skip or not hasattr(obj, key):
            continue

        if key in utils._ID_SAFE_PROPS and isinstance(value, dict):
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
            skip=utils._NODE_EXPLICIT_PROPS,
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
    data = json_data

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

class NODECODE_OT_import(bpy.types.Operator):
    bl_idname = "nodecode.import"
    bl_label = "Import"
    bl_description = "Import Nodes"

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == 'NODE_EDITOR'
    
    def execute(self, context):
        raw = context.window_manager.clipboard.strip()
        data = _decode_json(raw)
        tree_type_hint = data.get("tree_type", "ShaderNodeTree")

        tree, err = get_active_node_tree(context)

        if err and context.scene.importMode != 'NEW':
            tree, err = ensure_import_node_tree(context, tree_type_hint)
            if err:
                self.report({"WARNING"}, err)
                return {"CANCELLED"}
        
        space = context.space_data

        match context.scene.importMode: 
            case 'REP':
                if space.node_tree:
                    space.node_tree.nodes.clear()
            
            case 'NEW':
                ui_type = space.tree_type
                print(f"Creating new tree for Editor Type: {ui_type}")

                match ui_type:
                    case 'ShaderNodeTree':
                        mat = bpy.data.materials.new(name="NodeCode import")
                        mat.use_nodes = True
                        mat.node_tree.nodes.clear()

                        tree = mat.node_tree

                        obj = context.active_object
                        if obj and obj.type in {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'GREASEPENCIL'}:
                            if obj.data.materials:
                                obj.data.materials[0] = mat
                            else:
                                obj.data.materials.append(mat)
                        
                        space.shader_type = 'OBJECT'
                        space.node_tree = tree

                    case 'GeometryNodeTree':
                        tree = bpy.data.node_groups.new(name="NodeCode import", type='GeometryNodeTree')
                        space.node_tree = tree

                    case 'CompositorNodeTree':
                        tree = bpy.data.node_groups.new(name="NodeCode import", type='CompositorNodeTree')
                        if hasattr(context.scene, "compositing_node_group"):
                            context.scene.compositing_node_group = tree
                        else:
                            context.scene.use_nodes = True
                        space.node_tree = tree

                    case _:
                        print(f"Unsupported tree type: {ui_type}")

        if tree:
            import_node_tree_from_json(tree, data, context)

        bpy.ops.ed.undo_push(message="NodeCode: undo import")
        self.report({"INFO"}, "Node tree imported successfully")
        
        return {"FINISHED"}
