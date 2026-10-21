"""Cheap native snapshots for UI refresh, including edits made outside ShapeForge."""


def token(session):
    if session is None:
        return None
    session.validate()
    instances = list(session.cell.each_inst())
    cells = {i.cell_index: session.layout.cell(i.cell_index).dbbox().to_s() for i in instances}
    geometry = tuple((i.cell_index, i.prop_id, i.dcplx_trans.to_s()) for i in instances)
    shapes = tuple((layer, s.prop_id, s.bbox().to_s(), s.is_box())
                   for layer in session.layout.layer_indexes() for s in session.cell.shapes(layer).each())
    selection = []
    for obj in session.cellview.view().object_selection:
        if obj.is_cell_inst():
            target = obj.inst()
            identity = next((index for index, candidate in enumerate(instances) if candidate == target), -1)
        else:
            identity = (hash(obj.shape), obj.layer)
        selection.append((obj.cv_index, obj.top, len(obj.path), obj.is_cell_inst(), identity))
    return (id(session), session.cell.prop_id, session.layout.dbu, session.grid_um, session.layout.is_editable(),
            session.cellview.cell == session.cell, geometry, tuple(sorted(cells.items())), shapes, tuple(selection))
