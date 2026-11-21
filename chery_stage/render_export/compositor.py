"""
CheryStage - Render Export: Compositor helpers
Inserts / removes the "glow alpha" node group that rebuilds alpha from glow brightness,
so additive glow (RGB > 0, Alpha = 0) survives straight-alpha formats such as PNG.

Alpha' = max(Alpha, clamp(Strength * MapRange(Source, Threshold..1 -> 0..1) ^ Gamma))
Source = max(R, G, B)  or  luminance   (blended by "Use Luminance")
RGB is left untouched (premultiplied), so saving to PNG un-premultiplies it correctly.
"""

from typing import Optional
import bpy

GROUP_NAME = "CheryTools_GlowAlpha"  # internal name kept from CheryTools: stored in existing .blend files
NODE_NAME = "CheryGlowAlpha"
GROUP_VERSION = 1  # bump when the internal graph changes, forces a rebuild


# ---------------------------------------------------------------------------
# Scene tree access
# ---------------------------------------------------------------------------

def get_scene_tree(scene: bpy.types.Scene) -> Optional[bpy.types.NodeTree]:
    return getattr(scene, "compositing_node_group", None)


def find_output_node(tree: bpy.types.NodeTree) -> Optional[bpy.types.Node]:
    outputs = [n for n in tree.nodes if n.bl_idname == "NodeGroupOutput"]
    if not outputs:
        return None
    for n in outputs:
        if getattr(n, "is_active_output", False):
            return n
    return outputs[0]


def find_glow_node(tree: Optional[bpy.types.NodeTree]) -> Optional[bpy.types.Node]:
    if tree is None:
        return None
    node = tree.nodes.get(NODE_NAME)
    if node and node.bl_idname == "CompositorNodeGroup":
        return node
    return None


# ---------------------------------------------------------------------------
# Node group definition
# ---------------------------------------------------------------------------

def _new_socket(ng, name, in_out, socket_type, default=None, min_value=None, max_value=None):
    item = ng.interface.new_socket(name=name, in_out=in_out, socket_type=socket_type)
    if default is not None and hasattr(item, "default_value"):
        item.default_value = default
    if min_value is not None and hasattr(item, "min_value"):
        item.min_value = min_value
    if max_value is not None and hasattr(item, "max_value"):
        item.max_value = max_value
    return item


def _math(ng, op, loc, clamp=False):
    n = ng.nodes.new("ShaderNodeMath")
    n.operation = op
    n.use_clamp = clamp
    n.location = loc
    return n


def build_group() -> bpy.types.NodeTree:
    """Create (or rebuild) the shared GlowAlpha node group."""
    ng = bpy.data.node_groups.get(GROUP_NAME)
    if ng is not None and ng.get("chery_version") == GROUP_VERSION:
        return ng
    if ng is None:
        ng = bpy.data.node_groups.new(GROUP_NAME, "CompositorNodeTree")
    else:
        ng.nodes.clear()
        ng.interface.clear()

    _new_socket(ng, "Image", "INPUT", "NodeSocketColor", (0.0, 0.0, 0.0, 1.0))
    _new_socket(ng, "Strength", "INPUT", "NodeSocketFloat", 1.0, 0.0, 10.0)
    _new_socket(ng, "Threshold", "INPUT", "NodeSocketFloat", 0.0, 0.0, 0.95)
    _new_socket(ng, "Gamma", "INPUT", "NodeSocketFloat", 1.0, 0.1, 5.0)
    _new_socket(ng, "Use Luminance", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
    _new_socket(ng, "Image", "OUTPUT", "NodeSocketColor")

    nodes, links = ng.nodes, ng.links
    gi = nodes.new("NodeGroupInput"); gi.location = (-900, 0)
    go = nodes.new("NodeGroupOutput"); go.location = (900, 0)

    sep = nodes.new("CompositorNodeSeparateColor"); sep.location = (-650, 150)
    links.new(gi.outputs["Image"], sep.inputs["Image"])

    max_rg = _math(ng, "MAXIMUM", (-400, 250))
    links.new(sep.outputs["Red"], max_rg.inputs[0])
    links.new(sep.outputs["Green"], max_rg.inputs[1])
    max_rgb = _math(ng, "MAXIMUM", (-200, 250))
    links.new(max_rg.outputs[0], max_rgb.inputs[0])
    links.new(sep.outputs["Blue"], max_rgb.inputs[1])

    lum = nodes.new("CompositorNodeRGBToBW"); lum.location = (-400, 50)
    links.new(gi.outputs["Image"], lum.inputs[0])

    mix = nodes.new("ShaderNodeMix"); mix.location = (0, 150)
    mix.data_type = "FLOAT"
    links.new(gi.outputs["Use Luminance"], mix.inputs[0])
    links.new(max_rgb.outputs[0], mix.inputs[2])
    links.new(lum.outputs[0], mix.inputs[3])

    rng = nodes.new("ShaderNodeMapRange"); rng.location = (200, 150)
    rng.clamp = True
    links.new(mix.outputs[0], rng.inputs[0])
    links.new(gi.outputs["Threshold"], rng.inputs[1])
    rng.inputs[2].default_value = 1.0
    rng.inputs[3].default_value = 0.0
    rng.inputs[4].default_value = 1.0

    pw = _math(ng, "POWER", (400, 150))
    links.new(rng.outputs[0], pw.inputs[0])
    links.new(gi.outputs["Gamma"], pw.inputs[1])

    mul = _math(ng, "MULTIPLY", (550, 150), clamp=True)
    links.new(pw.outputs[0], mul.inputs[0])
    links.new(gi.outputs["Strength"], mul.inputs[1])

    new_alpha = _math(ng, "MAXIMUM", (700, 0), clamp=True)
    links.new(sep.outputs["Alpha"], new_alpha.inputs[0])
    links.new(mul.outputs[0], new_alpha.inputs[1])

    comb = nodes.new("CompositorNodeCombineColor"); comb.location = (800, -150)
    links.new(sep.outputs["Red"], comb.inputs["Red"])
    links.new(sep.outputs["Green"], comb.inputs["Green"])
    links.new(sep.outputs["Blue"], comb.inputs["Blue"])
    links.new(new_alpha.outputs[0], comb.inputs["Alpha"])
    links.new(comb.outputs["Image"], go.inputs["Image"])
    go.location = (1000, -150)

    ng["chery_version"] = GROUP_VERSION
    return ng


# ---------------------------------------------------------------------------
# Insert / remove / sync
# ---------------------------------------------------------------------------

def sync_glow_values(scene: bpy.types.Scene) -> None:
    node = find_glow_node(get_scene_tree(scene))
    if node is None:
        return
    p = scene.chery_render
    values = {
        "Strength": p.glow_strength,
        "Threshold": p.glow_threshold,
        "Gamma": p.glow_gamma,
        "Use Luminance": 1.0 if p.glow_source == "LUMA" else 0.0,
    }
    for name, val in values.items():
        sock = node.inputs.get(name)
        if sock is not None and abs(sock.default_value - val) > 1e-6:
            sock.default_value = val


def insert_glow_node(scene: bpy.types.Scene) -> str:
    """Returns '' on success, otherwise a Chinese error message."""
    tree = get_scene_tree(scene)
    if tree is None:
        return "当前场景没有合成节点树，请先在合成器中新建，或点击「新建合成节点」"
    node = find_glow_node(tree)
    group = build_group()
    if node is not None:
        node.node_tree = group
        sync_glow_values(scene)
        return ""

    out = find_output_node(tree)
    if out is None or not out.inputs:
        return "合成节点树中没有「组输出」节点"
    target = out.inputs[0]
    if not target.is_linked:
        return "「组输出」节点的 Image 输入未连接任何节点"

    link = target.links[0]
    src = link.from_socket
    tree.links.remove(link)

    node = tree.nodes.new("CompositorNodeGroup")
    node.node_tree = group
    node.name = NODE_NAME
    node.label = "辉光透明 (CheryStage)"
    node.location = (out.location.x - 220, out.location.y)
    out.location.x += 80
    tree.links.new(src, node.inputs["Image"])
    tree.links.new(node.outputs["Image"], target)

    scene.render.use_compositing = True
    sync_glow_values(scene)
    return ""


def remove_glow_node(scene: bpy.types.Scene) -> None:
    tree = get_scene_tree(scene)
    node = find_glow_node(tree)
    if node is None:
        return
    in_sock = node.inputs.get("Image")
    src = in_sock.links[0].from_socket if in_sock is not None and in_sock.is_linked else None
    targets = [l.to_socket for l in node.outputs["Image"].links]
    tree.nodes.remove(node)
    if src is not None:
        for t in targets:
            tree.links.new(src, t)


def create_compositor_with_glare(scene: bpy.types.Scene) -> bpy.types.NodeTree:
    """Render Layers -> Glare (Bloom) -> Group Output (+ Viewer)."""
    tree = bpy.data.node_groups.new(f"{scene.name}_合成", "CompositorNodeTree")
    tree.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    nodes, links = tree.nodes, tree.links

    rl = nodes.new("CompositorNodeRLayers"); rl.location = (-500, 0)
    glare = nodes.new("CompositorNodeGlare"); glare.location = (-200, 0)
    for val in ("Bloom", "BLOOM"):
        try:
            glare.inputs["Type"].default_value = val
            break
        except Exception:
            pass
    out = nodes.new("NodeGroupOutput"); out.location = (200, 0)
    viewer = nodes.new("CompositorNodeViewer"); viewer.location = (200, -180)

    links.new(rl.outputs["Image"], glare.inputs["Image"])
    links.new(glare.outputs["Image"], out.inputs[0])
    links.new(glare.outputs["Image"], viewer.inputs[0])

    scene.compositing_node_group = tree
    scene.render.use_compositing = True
    return tree
