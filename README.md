# Avatar Legends modding tools

Python tools for digging into Avatar Legends (Abare engine): pak extraction and repacking, munged image decoding, and sprbin poking. Built these while replacing a character's full frame set, so everything here has been tested against the real game, not just against the file formats.

## What's here

- `pak_extract.py` / `Abare_Engine_PAK.py` — extract and rebuild the game's .pak archives. Rebuilds are byte-identical roundtrips of the shipped paks and the game accepts modified ones. No integrity checks. Data is 4096-aligned starting at 0x1000, directory sits right after the last file, entries are alphabetical.
- `munged_extract.py` — decodes the munged image formats (palettized types, RLE variants) out of the paks.
- `AvatarLegendsPak.bt` — 010 Editor binary template for the pak format if you'd rather browse by hand.
- `sprbin_tool/` + `SPRBIN_Extractor.7z` — sprbin extraction. Character costume/palette tables live in here.

## Hard-won facts, so you don't re-test them

**Loading:** the ONLY way to get a mod in is editing a shipped pak directly. Loose files mirroring internal paths are never read. An extra pak dropped in data_packages mounts (you can see it in the abare log) but never overrides anything — tested sorting it first and last, there's no cross-pak path override. Game updates clobber your mods; keep your edits scripted so you can reapply.

**Palettes:** costume tables are 64 rows x 4 cols RGBA in the character's sprbin. Column 3 is shader metadata — leave it alone. Byte-edit and repack gives a full recolor across all frames. Don't assume a row is unused without checking: census the R-channel of decoded frames first, our test character used 61/64 rows across main + supports. Support characters render from the MAIN character's costume tables.

**Custom frames:** the engine accepts fully synthesized geometry — arbitrary crop, canvas, atlas layout, full occupancy. Characters live in a 3840x2160 canvas space. Vacant atlas cells must use the type's transparent value (type-3: 0,255,0,0) — zero-fill renders as opaque black. Type-6 in character frame slots is a dead end: scale_code 0 is invisible, scale_code 1 renders 2x anchored to screen origin. The sprite pipeline is type-3 only; type-6 is fine for UI assets. For custom type-3, avoid palette row 0 with G=255, that's the transparency sentinel.

**.lvl files** are plain text scene scripts (layers, sprites, lighting, parallax) — moddable with a text editor. "png/" references resolve to dds/ inside the pak.

## AI frame generation: what did NOT work

If you're thinking about replacing a character's art with AI-generated frames (~1600 frames per character), save yourself some weeks. Single frames are easy — SDXL/Illustrious with a character LoRA plus openpose/lineart controlnet over the decoded frames gets usable quality. Sequences are the wall. All of these failed:

- **Per-frame generation with a fixed seed/anchor** — motion flattens out and details drift frame to frame. Unwatchable in-game.
- **Optical-flow warping an anchor frame along the source motion** — smears. You're applying flow computed from the original body to a different silhouette, and flat-color art gives the flow estimator almost nothing to grab (aperture problem).
- **RBF/bone puppet warps** — wobbles, reads as Live2D, not as drawn frames.
- **Part-cutout copy-paste rigging** — works in principle, but the segmentation QC never ends. Not viable at 1600 frames.
- **Small-delta chaining** only holds for tiny pose steps, then falls apart.

What finally held: pose-sequence-conditioned video diffusion (reference image + control frames, temporal attention — VACE). The 1.3B model has artifacts that look like blockers (swimming patterns, mangled hands); the 14B quantized to Q4 fixed those on a 24GB machine at ~35 min per run. If you're stuck on 1.3B output, it's the model size, not your workflow.

One more thing that helps: frame visuals are arbitrary — gameplay and hitboxes come from the animation data, so you can re-author how a move looks instead of trying to faithfully copy the original frames.
