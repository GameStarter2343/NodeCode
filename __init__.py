# Licensed under the General Public License 3.0

import os

import bpy  # pyright: ignore
import tomllib
import requests

from bpy_extras.io_utils import ExportHelper  # pyright: ignore

from . import compress
from . import exporter
from . import importer

current_dir = os.path.dirname(__file__)
manifest_path = os.path.join(current_dir, "blender_manifest.toml")

version = "0.0.0"

try:
    with open(manifest_path, "rb") as f:
        data = tomllib.load(f)
        version = data.get("version", "0.0.0")
except Exception:
    pass

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

        # 1. HEADER: Info Label
        header_row = info_box.row()
        header_row.alignment = "CENTER"
        header_row.label(text="Info", icon="INFO")

        # 2. VERSIONS LINE
        row = info_box.row()
        row.alignment = "CENTER"
        row.label(text=f"Blender: {".".join(map(str, bpy.app.version[:3]))}    |   Addon: {version}  ")

        # 3. NODE TREE INFO HEADER
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

        # 4. STATS GRID / COLUMNS
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

        export_box = layout.box()

        row = export_box.row()
        row.operator(icon='EXPORT', operator="nodecode.export")
        row = export_box.row()
        row.alignment = "EXPAND"
        row.prop(scene, "exportMode")

        match scene.exportMode:
            case 'CLIP':
                export_box.separator(factor=0)
                export_box.prop(scene, "compress")
                col = export_box.column(align=True)
                col.enabled = scene.compress
                col.row().prop(scene, "compressAlg")
                col.row().prop(scene, "encodeAlg")

            case 'FILE': #wip
                row=export_box.row()
                row.alignment="CENTER"
                row.label(text="Work in progress")
            
            case 'LINK':
                export_box.separator(factor=0)
                export_box.prop(scene, "pastebinAPI")

            case 'WIP': #wip
                row=export_box.row()
                row.alignment="CENTER"
                row.label(text="Work in progress")


        import_box = layout.box()

        import_box.operator(icon='IMPORT', operator="nodecode.import")
        col = import_box.column()
        col.prop(scene, 'importMode')
        col.prop(scene, 'importReuseGroups')

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    exporter.NODECODE_OT_export,
    importer.NODECODE_OT_import,
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
            ('CLIP', 'Clipboard', f"Export nodes as a JSON and save result to clipboard. \nLeast compact of all options"),
            ('FILE', 'File [WIP]', "Work in progress"),
            ('LINK', 'Link (Pastebin)', f"Export nodes as a JSON, create paste at Pastebin and save link to clipboard. \nProbably the best variant since it's fast, simple and pastebin allows paste to be available indefinitely long. \n\nPLEASE NOTE: You need to provide your developer api key in order to create pastes, on free accounts pastes are limited to 20 per day"),
            ('WIP', 'Link (x0.at) [WIP]', "Work in progress"),
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

    exporter.utils.version = data.get("version")
    register()
