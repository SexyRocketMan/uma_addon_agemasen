# How results get verified from here

## The change, and why

Every automatic comparison this work attempted between the Blender port and the game's own render was invalid,
and the reason was structural rather than a bug to patch: **the two images came from different pipelines.** One
renderer blanked everything but the face and the other did not, so one frame was a face on a flat background
and the other was a whole character. On top of that, the instrument that classified pixels did so by hue over
the whole frame, which made the sky, the hair and the clothing "shaded regions".

The specific numbers that had to be withdrawn because of it:

| claim | how it was measured | why it failed |
| --- | --- | --- |
| the port's boundary is as smooth as the game's (27.3° against 22.3° mean turn) | a boundary walker applied to both renders | 113 and 26 boundary points, on a face occupying 2.5% of one frame, through a crop box containing hair |
| the port's lit fraction is 63.8% against the game's 68.0% | hue classification over both frames | the game's frame is ~11.6% face; the rest is background, hair, body and sky, and most of it landed in the "shaded" class |
| the port's shaded region is the same shape as the game's | reading a hand-made three-panel sheet | the blue in that sheet was the *background colour*, not the shaded branch |
| the port's boundary tracks the light (13% swing at low elevation) | the same hue classifier | same reason |
| the normal redirection is "nearly a no-op on this character" | renders that were lit from behind | measured nothing |

The withdrawn claims are a small lesson in themselves: an instrument that quietly transforms its input looks
exactly like a working instrument. The light vector was negated by hand inside a probe before the dot product
was taken, so the probe's own numbers could not reveal the mistake; and the hue classifier's acceptance of a
blue background is the same failure in a different guise.

## What replaces it

**Numbers that can be computed from source data stay automatic.** These have a right answer that does not
depend on rendering: the mesh's normals and UVs, the texture's channels, the material's values from the PMX
comment, the disassembly, and any expression that can be evaluated twice - once on the shader's nodes and once
in Python. That is the check that found the light bug, and it is worth keeping.

**Everything about how an image looks goes to a human.** Any judgement about a boundary line's smoothness, a
region's shape, or whether two renders agree is made by a person reading a labelled sheet. My own vision turned
out to be unusable - the model in use here cannot accept image input at all - and even where it can, reading a
blue background as a shaded region is not a mistake a second opinion should be needed for.

**So renders are produced in batches, as labelled sheets.** One variable per sheet, every cell labelled with
its value, the covered geometry stated in the caption. The reader answers a specific question.

## The sheets

Produced by `run_verification_sheets.ps1` and assembled by `build_verification_sheets.py`, into
`C:\Users\user\Documents\umaviewer_exports\sheets\`:

| sheet | the variable | the question it answers |
| --- | --- | --- |
| `sheet1_mask_pinned.png` | the step's mask pinned to 0.25, 0.4, 0.6, 0.8, 1.0 | with the texture taken out, is the line still chunky? If it is, the texture is not the cause |
| `sheet2_normal_field.png` | the game's redirected normals against the mesh's own | does the redirection do anything visible on this character? |
| `sheet3_light_sweep.png` | the sun's azimuth at four values, at two elevations | how far does the boundary move, and does the elevation change it as predicted? |

The renderer behind them is `render_game_step_mask.py`: the face's two branches painted as flat red and flat
blue on a flat yellow background, every material but the face's replaced with a transparent shader, the sun
driven from the command line, and the camera distance **solved from the wanted framing** rather than guessed.
That last part matters more than it looks: the old formula put the face across 2.5% of the frame, and at that
size the boundary is a couple of hundred pixels long and any measurement of it is mostly rasterisation. The
face now spans 82% of the frame height at 39.6% coverage, with no stray geometry.

## What a sheet has to contain to be worth reading

Learned from the sheets that misled: the caption states the variable, the values, what everything else was held
at, and which colour means what. Where a claim is about a specific piece of geometry - the face rather than the
hair, say - that geometry is the only thing in the frame, so a reader cannot mistake one for the other.
