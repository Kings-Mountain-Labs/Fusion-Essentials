## Plan

**name-parts-for-what-they-are** - A made part takes a functional name, a bought part its spec - 'SS 6003 2RS (10 X 17 X 35mm)', 'SHCS M8 X 65mm' - so the browser is the bill of materials when a part enters the tree; not for imported names. Prove: `design_get`: no 'Component1' and no 'Sketch1' left in the tree.

**variants-are-configurations** - Author them as configurations of one document driven by a few named parameters and a part-number property; check the active row before calling a failed compute a defect when the brief names several variants; not for a single variant. Prove: `design_get`: the configuration table, its columns and the active row.
Example: the Configured Dumbbell: 14 rows varying weight_width, weight_length, weight_text and a Part Number.

**bought-parts-are-frozen** - Hold it as a base feature in its own component and re-cut only the interfaces on top of it with hole and chamfer features; never edit the frozen body when a purchased, imported or generative body enters the design; not for a body you are modelling yourself. Prove: `design_get`: the timeline: a BaseFeature row, then Hole rows on the same component.
