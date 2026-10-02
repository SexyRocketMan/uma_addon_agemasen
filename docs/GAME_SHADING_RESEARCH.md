<!-- superseded -->
> **Note.** Superseded. This file reads the article the task pointed at, and its reading is wrong: that article does not discuss a jagged face boundary, does not mention face normals, and its normal work is for the outline. What it does say - that polygon count limits fine shading so the boundary is painted into a shadow mask - is recorded in `GROUND_TRUTH.md`.

# What Cygames said about the shading, and how it matches what we built

Primary source: the character model team's talk 「ウマ娘 プリティーダービー 3DCGキャラクター事例 ～基本設計と
ウマ娘ならではの表現について～」 at Cygames Tech Conference 2021, as written up in full at
<https://game.watch.impress.co.jp/docs/kikaku/1366154.html>. This note quotes that article; the quotes are the
evidence, and the translations are mine.

## 1. Why a shadow mask exists at all — our jagged-boundary problem, named

On the control maps (制御用マップ), which are **two maps carrying five channels** — shadow, specular, cutoff,
environment and rim:

> 影をコントロールするシャドウマスクは、制御用マップの中では最もクオリティへの影響が大きく、テクスチャ作業の
> 大半がシャドウマスクの調整にかけているという。**形状にあった陰影ではアニメ的な表現をするのは難しく、また、
> ポリゴン数などが原因で細かな陰影を表現するには限界があるためシャドウマスクを使って制御している。**

> "The shadow mask that controls shadows has the largest effect on quality of all the control maps, and most of
> the texture work goes into tuning it. **Shading that follows the geometry makes anime-style expression
> difficult, and there is a limit to how fine the shading can be because of the polygon count — so a shadow mask
> is used to control it.**"

That is the diagnosis this project reached by measurement, in the vendor's own words: a boundary computed from
the surface cannot be fine enough at game polygon counts, so it is **moved into a texture**.

Their model budget, from the same article: **just under 20,000 polygons**, 6 materials, ~160 joints, ~20 textures,
2 influences. Ours, measured, is **26,648 polygons** — so we are *denser* than the game and still saw the artefact,
which rules out density as the explanation and confirms the mask is the mechanism rather than more triangles.

## 2. The detail mask on the head — our painted-mask-times-light hybrid, named

On the head specifically (ディテールマスク):

> ウマ娘モデルの頭部では、絵的なシェーディングを実現するためディテールマスクを追加している。ディテールマスクでは、
> 主に**ほっぺ・鼻**のシェーディングを制御するために使用されており、**ライトの角度と組み合わせて補正をかける**
> ことで多様な影の表現が可能になっている。

> "For the head, a **detail mask** is added to achieve picture-like shading. The detail mask is used mainly to
> control the shading of the **cheeks and nose**, and **by combining it with the light angle to apply corrections**,
> a wide variety of shadow expressions becomes possible."

Two things follow, and both are what this addon now does by default:

* the mask controls **where** the shadow falls, in texture space — cheeks and nose, which is exactly the area the
  community ports put their half-plane test, and exactly where our jagged edge was worst;
* the **light angle is combined with it** — the mask does not replace the lighting, it corrects it. Our
  implementation multiplies the painted mask by the light term, which is that sentence.

The colour maps, separately, are **two**: diffuse and shadow colour (ディフューズ, シャドウカラー) — the blend this
project reproduces as `lerp(_ToonMap, _MainTex, …)`.

## 3. Outlines — why the normal set lives in a UV channel

> アウトラインは、面を反転し、法線の方向に膨らませたモデルを生成することで実現している。
> アウトラインをキレイに出すためには法線調整を行なう必要があるが、法線は他の要素にも影響を与える。…
> **大元のモデルにシェーディング用の法線をもたせ、アウトライン用の法線調整を施したアウトライン用モデルから
> 大元のモデルのUVsetの中に移し替えることでそれぞれに適した法線情報を両立させている。**

> "The outline is made by generating an inverted model expanded along the normals. Getting clean outlines requires
> adjusting the normals, but normals affect other things as well… **the base model keeps its shading normals, and
> the normals adjusted on the outline model are transferred into the base model's UV set** — that way each has the
> normals suited to it."

This is the `UV3` outline normal set that UmaViewer's `Uma Outlines` geometry-nodes group reads with a
`Named Attribute` node, and which our export does not write — so the outline shell is pushed along whatever is in
that channel (our vertex colours), which is why the whole model renders inside out when outlines are enabled.

Cause now confirmed from the primary source, and the fix is the one already scoped: write the outline normal set
into the spare extra UV channel at export, and point the group's attribute name at it. Note also that the *shading*
normals must stay as they are — the article says adjusting normals for outlines is what breaks shading, which is
exactly why the two sets are kept apart.

## 4. What the article does not give

No shader code, no thresholds, and no numbers for the mask channels. It describes the *design* and the *reasons*,
which is what we needed: it confirms the mechanism, and it confirms that the mechanism is the intended answer to
the artefact rather than a workaround on our side.

The remaining unknown is the same one as before — which channel of the two control maps carries the face's shadow
mask, and the constants the shader uses. Our export carries `_TripleMaskMap` bound to the base map, whose green
channel is flat 0.52 on this costume, while its red channel is bimodal. The article says the shadow mask is the
most-tuned part of the texture work, which is consistent with a channel that has been painted into exactly the
shape wanted — and is a reason to keep treating the red channel as the painted shadow until the game's own
material says otherwise.
