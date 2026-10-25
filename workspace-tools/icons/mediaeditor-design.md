# 影视编辑图标 1.0.3

采用内置 image_gen 生成，随后仅导出为 300×300 RGBA PNG，保留生成图像的透明度。原始大图保存在本地调查资料中；发布源码使用 `mediaeditor-master.png`，`mediaeditor.py` 将该主图复制到插件 `src/ui/icon.png`，避免重建时恢复此前的程序绘制图标。

当前视觉方向：纯蓝色圆角底、白色场记板与两条调整滑杆，扁平二维风格。保留此前图标的主题与大致构图，移除立体效果、渐变、光泽、浮雕、阴影和多余装饰。

1.0.3 使用内置 image_gen 的扁平化编辑提示词：

> Use case: style-transfer. Edit target: the attached current 影视编辑 app icon. Convert it into a STRICTLY FLAT 2D app icon. Preserve the recognizable open clapperboard with two horizontal adjustment sliders, its centered composition and approximate proportions, and its rounded-square tile. Replace the background with ONE completely uniform solid royal-blue color (#2867D8). The clapperboard and its round pivot are pure white flat shapes. The stripes, slider tracks and slider knobs use the same solid blue or white only, with clean contrast and even simple geometry. Exactly TWO opaque colors inside the tile: blue and white. Absolutely NO gradients, shadows, highlights, glow, bevels, embossing, glossy materials, texture, lighting, translucency, three-dimensional depth or perspective. Clean vector-like straight edges and smooth rounded corners, bold readable silhouette at 48px. No letters, text, numbers, extra marks or objects. The rounded-square blue tile fills the square canvas edge to edge; genuinely transparent pixels outside its four rounded corners, with a crisp antialiased silhouette and no scattered pixels or outer shadow. Deliver only the standalone icon.

以下保留 1.0.2 的设计过程，当前发布不再使用其立体风格。

生成提示词：

> Use case: logo-brand. Asset type: production app icon for a Chinese NAS utility named 影视编辑, used in a grid of native system application icons. Create ONE beautiful, professionally designed app icon, square composition. Subject: a single clean white cinema clapperboard, with two subtle horizontal adjustment sliders integrated into its lower panel to communicate editing media metadata. Strong simple silhouette, generous balanced spacing, immediately legible at 48px. Premium restrained native-app aesthetic: soft dimensional edges, refined geometry, a very subtle surface sheen, minimal blue-to-indigo background, no excessive glass, no exaggerated 3D perspective. Face-on orthographic composition, precisely centered. The rounded-square background tile should fill the square canvas edge to edge with smoothly rounded corners and genuinely transparent pixels outside the corners. Main symbol takes about 60 percent of the tile. No text, letters, numbers, captions, watermarks, mockup, surrounding page, card stack, paper document, circular corner badge, pencil, play triangle, decorative particles or extra objects. Deliver a clean standalone PNG app icon with real alpha around its rounded-square corners.

定向清理提示词：

> Production cleanup of this app icon. Preserve the clapperboard-and-two-sliders design, colors, geometry, front view, materials and overall composition. Clean the alpha silhouette rigorously: remove ALL scattered colored specks, stray pixels, rough outlines, fuzz and glow outside the rounded-square blue background. Its exterior edge must be a perfect smooth rounded-square silhouette with clean antialiasing and fully transparent pixels outside, no outer shadow. Enlarge the clean rounded-square tile very slightly so its straight top/bottom/left/right extents meet the canvas edges with no transparent inset margin except the four rounded corners. Keep the centered white clapperboard at its current relative scale. No other changes, no text or watermark.
