---
id: S11_Gimbal-Ring-Job
fixture: none
---

## Prompt

GOAL - the gyroscope's OUTER GIMBAL RING and its machining job.

- Model the ring as one component: a revolved ring, outer diameter about 120 mm, inner diameter
  about 100 mm, about 14 mm tall, with a small chamfer on both outer edges; then two pivot bosses
  on the outside at 180 degrees to each other, each a short round boss about 16 mm in diameter
  standing about 6 mm proud of the ring, each carrying a 6 mm through hole on the ring's diameter
  line. Exact numbers are yours; state them and drive them with named parameters.
- Machine it on a mill-turn from the machine library that carries turning (state the machine), with
  cutting tools you create in the document's tool library (state each tool and its size):
  1. First, a turning setup: the ring held on its outside, turning from the end that presents the
     bore and the face; the lathe cycles rough and finish what a lathe can reach, face, bore or
     profile, the outer profile as far as the chuck allows. Read every cycle's warnings.
  2. Second, a milling setup whose stock is what the turning setup left (rest stock from the
     previous setup, not a fresh box): drill the two pivot holes and finish the two bosses with
     milling operations aimed at that geometry, plus a facing pass if the turned face needs one.
  Every operation must generate and must cut material: an operation that computes healthy but
  produces an empty toolpath (zero machining time) is a defect to fix or report. Read each
  operation's machining time and the empty-toolpath census after generating.
- Show each setup's toolpaths one at a time (all hidden, one shown, screenshot, hidden) and say
  whether the pictures agree with the numbers.
- Post each setup to its own NC file: the turning setup through a turning post this installation
  ships (a numeric program name), the milling setup through the local 'haas' post. Report each
  file's path and size from the post's own read-back. If a post refuses an operation its machine
  cannot run, exclude that operation and say so.

Report: the ring's outer diameter and height and the two holes as fresh reads give them; the machine
both setups carry; the setup order in the tree; the milling setup's stock mode string and the turning
stock's allowance; every operation's state, machining time and warnings; the tool each operation
carries; the two NC files with what each holds and what was excluded.

## Grader notes

- A good result, opened in Fusion, judged as a machinist: the turning setup faces the bore end and
  stops the outer profile short of the chuck; the milling setup's stock names the previous setup;
  the pivot holes are drilled, not bored with an endmill; a 6 mm drill for a 6 mm hole; every
  operation has a nonzero machining time; no warning goes unexplained; two NC files land.
- What a weak agent does: mills what the lathe should have turned, boxes the milling stock around
  the part, merges the two holes into one through-bore, leaves the second boss unmachined, or passes
  an operation whose toolpath is empty because its state read healthy.
- Axis this discriminates: MCP tooling. The mill-turn machine selection, stock-from-previous-setup,
  turning cycles on a round part, cam_select_geometry aimed at bosses and holes, the machining-time
  read (hasToolpath reads true on an empty toolpath), and the shipped turning post are the levers.
- First A/B to run: `--deny mcp__fusion-essentials__sys_get_guidance` against the bare brief (a
  guided run took fewer calls and did not machine the part better), then `--deny
  mcp__fusion-essentials__cam_select_geometry`.
- Earlier runs surfaced: both cold runs sequenced turning first and milling on rest stock; both fell
  short on the opposed bosses; the shipped posts refuse multi-axis programs and the turning post
  wants a numeric program name; a setup time read fails whole when one operation is errored.
