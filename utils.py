# Licensed under the General Public License 3.0

import os

current_dir = os.path.dirname(__file__)
manifest_path = os.path.join(current_dir, "blender_manifest.toml")

version = "0.0.0"

_GROUP_NODE_TYPES = {
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

_ID_SAFE_PROPS = {"node_tree", "image", "material", "texture", "world", "object"}

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
