import gdsfactory as gf
import os
from pathlib import Path
from shapeic_cellkit import CellKitCatalog, PrimitiveGeometry

def test_block() -> gf.Component:
    """Create a 10 × 5 µm block with two electrical ports."""
    c = gf.Component()

    layer = (1, 0)

    c.add_polygon(
        [(0, 0), (10, 0), (10, 5), (0, 5)],
        layer=layer,
    )

    c.add_port(
        name="left",
        center=(0, 2.5),
        width=5,
        orientation=180,
        layer=layer,
        port_type="electrical",
    )

    c.add_port(
        name="right",
        center=(10, 2.5),
        width=5,
        orientation=0,
        layer=layer,
        port_type="electrical",
    )

    return c

def simplediffpair() -> gf.Component:
    
    catalog = CellKitCatalog.open(
        root=Path("/foss/designs/shapeforge/shapeic-cellkit"),
        pdk="ihp-sg13g2",
        pdk_root=os.environ["PDK_ROOT"],
    )
    primitive = catalog.primitive("simplediffpair")
    geometry = PrimitiveGeometry(
        length_m=0.4e-6,
        finger_width_m=5.0e-6,
        nf=4,
    )
    rendered = primitive.render(geometry)

    return rendered.component
