# Morph (shape key) names

UmaViewer can spell a morph three ways, and this addon does not care which one a model uses. Operators ask
for a morph by *meaning* - group, action tag and side - and `uma_addon/addons/uma_addon/utils/naming.py`
resolves whichever spelling the mesh actually carries.

| spelling | example | who writes it |
|---|---|---|
| `tagged` | `EyeBrow_1_R(WaraiA)[M_Face]` | stock UmaViewer, and the Agemasen fork in mode 0 |
| `short` | `EyeBrow_1_R` | Agemasen fork modes 1 and 2, older forks |
| `unified` | `Brow_WaraiA_R` | Agemasen fork mode 3, the default there |

`unified` exists because a vmd morph name field holds only 15 bytes of shift-jis: the `tagged` spelling can
never be used to drive an exported model, and the `short` spelling says nothing about what the morph does.
The unified spelling is `<group token>_<romaji tag>[_<side>]`, with `EyelidHideA/B` abbreviated to
`LidHideA/B` so the longest tags still fit.

## Asking for a morph

```python
from ..utils import naming

# the name of the key describing (group, tag, side), or None
name = naming.find(shape_keys, "Eye", "XRange", "R", index=20, mesh="M_Face")

# both sides at once: (left, right), either of which may be None
left, right = naming.pairs(shape_keys, "EyeBrow", "WaraiA", index=1, mesh="M_Face")

# the name a *new* key should get, following whatever the mesh already uses
name = naming.target_name(shape_keys, "Ear", tag, side, index=i, mesh="M_Hair")
```

Every lookup tries the unified spelling first, then the tagged one, then the short one, and finally falls back
to scanning the mesh: any key that parses to the same group/tag/side is accepted, whatever its spelling. The
`index` and `mesh` arguments are only needed for the two older spellings, which carry them.

`naming.convention(shape_keys)` reports which spelling a mesh mostly uses (`"unified"`, `"tagged"`,
`"short"`, or `None`), and `naming.parse(name)` splits any of the three into
`{"group", "index", "side", "tag", "spelling"}`.

## Overrides

An optional `naming_overrides.json` next to this addon can force a spelling or rename a tag, for a model whose
names do not fit the grammar:

```json
{
  "convention": "unified",
  "tags": { "EyelidHideA": "LidHideA", "SomeNewTag": "Short" }
}
```

A broken override file is ignored with a message rather than breaking the addon.

## Where it is used

* `operators/AddonOperators.py` - the eye range morphs, when it builds the eye controls, and the eye-control
  guard that refuses to delete the `Eye_L` / `Eye_R` vertex groups unless all four morphs resolved.
* `operators/AddonOperators2.py` - `CombineShapekeys`, which pairs the left and right keys of an eyebrow, eye
  or ear morph.
* `operators/EarConvert.py` - creating the missing ear keys and finding them again for the drivers.
