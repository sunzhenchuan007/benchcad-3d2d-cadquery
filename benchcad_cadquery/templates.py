"""Load a blank, millimetre drawing sheet into model space without scaling.

This helper deliberately rejects model-space content.  It is a template
loader, not a sanitiser for expert drawings.  Callers must still curate the
template's text and metadata before distributing it to an agent.
"""

from pathlib import Path


def load_blank_template(path: str | Path, layout_name: str | None = None):
    """Return a new ezdxf document with one paper-space sheet in model space.

    Coordinates are copied verbatim in mm.  Multiple paper layouts require an
    explicit name.  Viewports and entities on non-plot/BC_GUIDE_NOPLOT layers
    are removed, including inside referenced blocks.  Layer, line-type, text,
    and dimension-style resources are retained.  No source file is modified.
    """
    import ezdxf
    from ezdxf.addons import Importer
    from ezdxf.addons.importer import IMPORT_ENTITIES

    source = ezdxf.readfile(Path(path))
    if len(source.modelspace()):
        raise ValueError("Blank template required: model space is not empty")
    if source.units != 4:
        raise ValueError("Template units must explicitly be millimetres; no scaling is applied")

    paper_layouts = [layout for layout in source.layouts if not layout.is_modelspace]
    if layout_name is None:
        if len(paper_layouts) != 1:
            raise ValueError("Template has ambiguous paper layouts; specify layout_name")
        selected = paper_layouts[0]
    else:
        matches = [layout for layout in paper_layouts if layout.name == layout_name]
        if not matches:
            raise ValueError("Requested paper layout does not exist")
        selected = matches[0]

    excluded = {
        layer.dxf.name.casefold()
        for layer in source.layers
        if not layer.dxf.get("plot", 1)
        or layer.dxf.name.casefold() == "bc_guide_noplot"
    }

    def excluded_entity(entity):
        return entity.dxftype() == "VIEWPORT" or entity.dxf.layer.casefold() in excluded

    # Mutate only the in-memory source.  Cleaning block contents before import
    # prevents non-plot guide geometry hidden inside an INSERT from reappearing.
    for block in source.blocks:
        for entity in list(block):
            if excluded_entity(entity):
                block.delete_entity(entity)

    visited = set()

    def validate_entities(entities):
        for entity in entities:
            if entity.dxftype() not in IMPORT_ENTITIES:
                raise ValueError(f"Unsupported template entity: {entity.dxftype()}")
            if entity.dxftype() == "INSERT":
                name = entity.dxf.name
                if name.casefold() in visited:
                    continue
                visited.add(name.casefold())
                if name not in source.blocks:
                    raise ValueError("Template references a missing block")
                block = source.blocks[name]
                if block.block.dxf.flags & 12:
                    raise ValueError("External-reference blocks are not supported")
                validate_entities(block)

    entities = list(selected)
    if not entities:
        raise ValueError("Selected template layout has no printable entities")
    validate_entities(entities)
    target = ezdxf.new(source.dxfversion)
    target.units = 4
    target.header["$MEASUREMENT"] = 1
    importer = Importer(source, target)
    importer.import_tables(["layers", "linetypes", "styles", "dimstyles"], replace=True)
    # Replacing the Standard text-style table entry invalidates the fresh
    # document's unused default MLeader style handle.  Rebind that resource;
    # this does not alter imported text or dimension-style definitions.
    for _, style in target.mleader_styles:
        style.dxf.text_style_handle = target.styles.get("Standard").dxf.handle
    importer.import_entities(entities, target.modelspace())
    importer.finalize()
    if len(target.modelspace()) != len(entities):
        raise ValueError("Template import did not preserve every selected entity")
    audit = target.audit()
    if audit.errors or audit.fixes:
        raise ValueError("Imported template failed DXF integrity checks")
    return target
