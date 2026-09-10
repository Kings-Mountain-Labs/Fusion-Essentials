---
name: parametric-cad-design
description: >-
  Use when designing or modelling in Fusion through the fusion-essentials tools - building a
  part, a surfaced product, an assembly, or a machining job, not running a fixed procedure.
  Practice read out of Autodesk's own sample designs: what to settle before the first feature,
  how a sketch carries intent, the feature order of a moulded or surfaced part, how an
  assembly's freedom is structured, and which machining strategy fits. Rules say when they apply
  and what to read back; recipes are ordered tool sequences with a bar for done and an exemplar
  to X-ray.
---

# Designing in Fusion

Rules for judgment, recipes for sequence, a bar for done - read out of the designs Autodesk ships as samples.

## Kernel

**declare-acceptance-and-interfaces** - Write what the design must satisfy and each interface as a checkable number - seating faces, aligned axes, clearance, wall thickness when you take on a brief; not for a throwaway probe. Prove: `workspace_orient`: the active document, its units and what is already in it.

**sequence-is-the-design** - Decide the feature order before drawing: form, then fillets, then shell, then bosses and holes, then cosmetics - a timeline built in that order edits cleanly, one built by accretion does not when the first feature is next; not for a one-feature part. Prove: `design_get`: the timeline reads as the order you planned.

**encode-intended-changeability** - Type a wall thickness or a pitch ONCE and reference it by name everywhere else; promote it to a named parameter only when a family or a configuration is expected when a value carries a decision; not for a measured value, held in one parameter saying so. Prove: `sketch_get`: dimensions whose expression is another dimension's name, not a repeated literal.

**build-and-observe-in-milestones** - Read it back against intent before the next feature: a bounding box, a volume, a screenshot - never only at the end when a milestone lands; not for a milestone a write already verified. Prove: `model_inspect`: bounding box and volume against the numbers you wrote down.

**compare-the-artifact-with-acceptance** - Compare it with the acceptance you wrote and say what you could not meet; a screenshot that reads as the object is part of the acceptance when the design looks finished; not for acceptance the brief left open. Prove: `view_screenshot`: does it read as the object it should be.

## Playbooks

A playbook `<id>` is the file `playbooks/<id>.md`, or `sys_get_guidance(section="<id>")`.

- `plan` - before the first component exists - naming, variants, what is bought
- `sketch` - any profile that must survive a size change or drive a feature
- `model` - turning sketches into a part - order, carving, patterns, frozen bodies
- `surface` - a shell, skin or product form that no extrude or revolve describes
- `assemble` - more than one component - how they are held, joined and moved
- `validate` - the geometry exists and must be proved against the brief
- `finish` - handing the work on
- `manufacture` - a machining job - setups, strategy choice, what a toolpath must prove

## Recipes

A recipe `<id>` is `sys_get_guidance(recipe="<id>")` - ordered steps, a read-back per step, a bar for done. Its prefix names the playbook it belongs to.

- `sketch-anchored-profile` - a bracket, plate or revolved profile that must resize by intent
- `sketch-link-between-bores` - a rocker, lever, connecting link or any web joining round bosses
- `sketch-organic-outline` - a mouse, handle or shell silhouette that must stay smooth while its proportions change
- `model-moulded-part` - a basket, cover, drawer front or housing with a wall, bosses and clips
- `model-frozen-body-with-interfaces` - a purchased part, an imported STEP or a generative outcome that needs holes and seats
- `model-parametric-family` - one part in several sizes
- `surface-swept-bottle` - a container whose section changes along a curved spine, its neck narrower than the body
- `surface-skin-into-parts` - a product whose top, base and middle share one outer surface
- `assemble-part-modelled-in-place` - a rocker, bracket or lever designed between parts that already sit where they belong
- `assemble-screw-motion` - a threaded cap, lead screw or any turn-to-advance pair
- `manufacture-choose-a-strategy` - a face, pocket, wall, hole or free-form surface needs an operation
- `manufacture-prove-a-toolpath` - an operation generated and must be trusted
- `manufacture-radial-hole-across-the-axis` - a hole whose axis runs across the part axis - a cross-drilling, or a hole through a boss on a turned part
