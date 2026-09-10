---
id: S2c_Regeneration
fixture: S2b_Hardware-Interfaces
---

## Prompt

The active document is the gyroscope with its pivot pins, bores and bearing seats: zero overlap
anywhere, support contact only at the carrier-on-pedestal and crank-on-frame engagements,
clearance at every pin, bore and seat. Do not save at any point; this stage proves a property and
leaves the document as found, with your restore verified by reads.

GOAL - prove the model regenerates:

1. Find the driving diameter parameter the design was built from (read the user parameters, pick
   it by name and comment, say why).
2. Record the baseline: the driver's value, a fresh health read, a fresh interference check, and
   fresh geometry reads of three solids at different depths of the chain (a ring's outer radius, a
   pin's radius or position, the rotor's extent), with units.
3. Bump the driver by 10 to 15 percent. Recompute if the design does not do so itself.
4. Read the same three values fresh. Each must have moved consistently with the bump; a value that
   did not change names a part whose solids are not driven by the parameter. Report it as the
   defect it is.
5. Health read: zero timeline errors at the new scale.
6. Interference check at the new scale: zero overlapping pairs, contacts only at the two support
   engagements. The clearance architecture has to survive scaling, not exist at one lucky size.
7. Restore the driver to its exact baseline value and read the three values, the health and the
   interference again; they match the baseline, and the document was never saved.

Do not fix anything you find. This stage measures; a failure here is upstream work, and your
precise report of what broke and where is the deliverable.

## Grader notes

- A good result: a short run of reads, before/after/restored numbers for three parts that all moved
  in proportion, a clean health read and a zero-overlap check at the bumped scale, and a document
  that reads unmodified at the end. A precise FAIL report naming a frozen part is also a good run.
- What a weak agent does: repairs the defect it was told not to touch, saves, or reads one value
  and extrapolates the other two.
- Axis this discriminates: the harness. This is a pure read-and-report stage; a cheaper model should
  do it as well as an expensive one, which makes it the place to try `--model` variants.
- First A/B to run: the same brief on two models; the numbers should agree with each other.
- Earlier runs surfaced: a frozen-pin defect, pins built from literal coordinates, reported precisely
  here after passing every read in its own stage.
