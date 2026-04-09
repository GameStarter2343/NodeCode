# Node Code Converter

A Blender add-on that lets you **export and import node trees as JSON**.

The project was designed to simplify node exchange, versioning, and migration between projects, serving as a tool for procedural pipelines. The key goal is to simplify the process of sharing node setups as much as possible.

---

## Features

-  Export node trees to clipboard as JSON
-  Import node trees from clipboard
-  Supports all major node systems:
   - Shader Nodes
   - Geometry Nodes
   - Compositor
   - Texture Nodes
-  Preserves links between nodes
-  Keeps layout and visual structure intact

---

## Installation

1. Download or clone this repository
2. Zip the `node_code_converter` folder (if needed)
3. In Blender:
   - Go to **Edit → Preferences → Add-ons**
   - Click **Install**
   - Select the `.zip` file or folder
4. Enable **Node Code Converter** extention

---

## Usage

### Export nodes

1. Open any Node Editor
2. Open the sidebar (`N` key)
3. Go to the **NodeCode** tab
4. Click **Export Nodes**

JSON is copied to your clipboard

---

### Import nodes

1. Copy JSON data (previously exported)
2. Open a Node Editor
3. Click **Import Nodes**

Nodes are reconstructed automatically

---

## How it works

The add-on serializes:

- Node types and properties
- Input values
- Links between nodes
- Node group dependencies (recursively)
- Frame hierarchy
- Node-specific modes (e.g. operations, blend types)
