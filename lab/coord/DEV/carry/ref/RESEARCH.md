# hive: premium web research

Researched 2026-11-17. Pages were opened in a browser at **1440x900**, and type sizes were measured in the live DOM. I only read pages. Nothing was installed or downloaded. Licence and price details marked "verify" came from third-party listings.

---

## TL;DR: 6 things to take

1. **Vercel's hero is our hero.** It has a headline on the left, one glowing object in the centre, and a tiny 3-line list on the far right. Keep the left headline and the honeycomb, and add a quiet right-edge rail if the hero needs more.
2. **Headings are two-tone sentences.** The lead is in white and the continuation is in grey, all in one paragraph (Linear, Raycast, Cursor, Supabase). This replaces the eyebrow + H2 + paragraph stack.
3. **Numbers sit in open columns with no boxes.** That means a big light-weight figure with a 2-line grey label (Stripe, Rivian, Framer). Only one number, or one bar, gets the accent (Apple).
4. **Data is drawn as a field of cells.** Modal shows GPUs as a grid of squares that light up. A honeycomb of cells is the hive version.
5. **One giant number per screen, pinned over the 3D object** (Zirka, Awwwards SOTD). It is the most premium way to show 3–4 key figures.
6. **The type scale is small and strict.** Display text is 48–64px, tracked tight (−2 to −6%), with line-height 1.0–1.1. Body text is 15–17px in grey. Use only 3 text tones (white / grey / dim).

---

## 1. Sites studied (15)

Measurements are font-size / weight / letter-spacing at a 1440px-wide viewport.

### 1. Linear: https://linear.app
- **Top to bottom:** thin "New" pill, then H1 with one-line sub, then a large product screenshot. After that: logo row, a two-tone statement paragraph, 3 "FIG 0.1/0.2/0.3" columns with line-art isometric drawings, 4 feature rows each ~1225px tall (H2 left, mockup), changelog list, 2 quotes and a single stat line, closing CTA.
- **Data and width:** max-width ~1436px with 76px side margins. The "Fig" trio is 3 equal open columns with no visible card boxes. Stats appear only once, as an inline sentence ("over 50,000 product teams"). The changelog is a stacked list of title, one line and a date.
- **Type:** H1 64/510/−1.4px with line-height 1.0. H2 48/510. Body 15px in grey `#8A8F98`. There are only 3 text colours: `#F7F8F8`, `#8A8F98` and `#62666D`.
- **Clutter control:** each row carries one idea and one visual, and the copy is 1–2 sentences. Section labels are tiny mono "FIG" captions instead of pills.
- **Motion:** the headline and screenshot blur and fade in on load. Mockups animate softly inside their frames.
- **Borrow:** the **two-tone statement paragraph** (white lead sentence + grey rest) as the intro for "how it decides".

### 2. Vercel: https://vercel.com
- **Top to bottom:** split hero, then a logo strip, then 3 use-case sections with the same template, "Recently shipped" (3 items), a deploy CTA, and brand assets.
- **Hero:** H1 "Agentic Infrastructure" on the left (64/400/**−3.84px**, about −6%). A **single glowing black triangle** sits in the centre. On the far right is a **3-line plain list** ("For coding agents / To ship apps… / Automated…").
- **Data and width:** in each section a big product mockup takes about 2/3 of the width on the left. A **narrow right rail** holds one proof sentence with the metric in white inside grey text ("Mintlify powers documentation for over **20,000** companies"), then a 4-item feature list with names only.
- **Type:** H2 56/450/−3.36px. Lead paragraphs are 24px in grey `#A1A1A1`, with the key phrase in white.
- **Clutter control:** pure black background, no cards around text, and features listed as names only.
- **Motion:** light glows around the 3D object, and mockups are live.
- **Borrow:** the **far-right micro-list in the hero**, plus the "metric inside a grey sentence" proof line.

### 3. Stripe: https://stripe.com
- **Top to bottom:** a live ticking counter above the H1 ("Global GDP running on Stripe: 1.75…%"), then the H1 + lead with animated gradient art and a logo marquee. After that: a bento of 6 products, then "The backbone of global commerce" with a 4-number stat row and a data-viz globe of lines. Then case-study rows (logo + 2 metrics + link), developer metrics (500m+ API req/day), a news carousel, a CTA and a dense footer.
- **Data and width:** the stat row is **4 centred open columns**. Numbers are 48px weight **300**, and labels are 2 lines at about 13px. **Only one number is dark and the others are grey**, so the highlight cycles. There are no boxes, and the row sits on white space above a visualisation.
- **Type:** Söhne. H1 48/300, H2 32–56/300. The light weight is what makes the big numbers feel expensive.
- **Clutter control:** each number gets one short label with no icons. The data viz carries the emotion.
- **Motion:** live counter, gradient wave, logo marquee, and the stat highlight cycling across numbers.
- **Borrow:** a **live counter line** above the hero headline (e.g. "chunks processed on hive today: 1,284,…") and the 4-up light-weight stat row.

### 4. Apple Mac Studio: https://www.apple.com/mac-studio/
- **Top to bottom:** sticky product sub-nav (Overview / Tech Specs / Compare / Buy), a huge headline (96/700) over the product, the price line, then a chip story. That is followed by **comparison bars**, a bento of highlights ("Up to 2.5x faster", "10.7x faster"), ports, environment, and buy.
- **Data and width:** the comparison block has a slash-separated list of workloads acting as tabs, with the active one in the accent colour. Below it are **thin horizontal bars**: the new model in an accent gradient, older models in grey, each labelled underneath, with the **number (48/600) at the right end of each bar** in accent or grey. It uses a single text column of about 900px with lots of empty side space.
- **Clutter control:** each block makes one claim, and footnote superscripts carry the fine print. The accent colour is used only on "ours".
- **Motion:** bars grow on scroll, the sticky sub-nav appears, and the product reveals cinematically.
- **Borrow:** **comparison bars**, e.g. "1 laptop vs hive of 4". Only the hive bar is honey-coloured and the rest are grey.

### 5. Raycast: https://www.raycast.com
- **Top to bottom:** centred H1 (64/600) with a short lead over striking red diagonal-stripe art, then a download button with a small mono platform note. Then a product demo with an **icon dock that switches the demo**, then feature sections.
- **Data and width:** section headings are **small (H2 20/500)** and written as a sentence: a "**Remember everything.** grey continuation…" line centred under the demo. The demo is the hero of each section.
- **Clutter control:** headings are deliberately small so the product UI dominates, and there is one demo per screen.
- **Motion:** the hero art drifts, and dock icons cross-fade the demo.
- **Borrow:** **small sentence-style section heads** under a big visual, used where the 3D honeycomb is the star.

### 6. Modal: https://modal.com
- **Hero:** centred H1 (45/500), with an accent-green key phrase, over a **voxel / dot-matrix 3D cube** made of tiny glowing squares. This is the closest analogue to our honeycomb cluster.
- **Layout:** a **sticky left index** (Inference / Training / Sandboxes / Functions) highlights while you scroll. Next to it, a 2x2 mini-feature grid (icon, bold title, 3 grey lines) sits on the left and a big dark panel with a live diagram sits on the right. There is an animated **GPU grid of squares** that light up as work is assigned, and a **hex-dot world map** with labelled regions.
- **Data and width:** a 4-column feature row under the map (icon + bold title + grey text) uses open columns with no borders. Fake-live dashboards ("Containers 100/50/0, Live", "GPU utilization 0%") are used as visual proof.
- **Type:** H2 38/500. Body is grey-green on `#181818`.
- **Borrow:** **cells that light up = work being placed.** That is exactly hive's story (chunks into laptops). Also the sticky left index for "how it decides".

### 7. PlanetScale: https://planetscale.com
- **Layout:** like a text document. **Everything is 16px monospace**, including H1 and H2, with hierarchy from bold, colour links and order only. The logo wall is a hairline grid (1px lines, no cards). Sections run Performance / Uptime / Cost / Security / Features.
- **Data:** a **real production chart** (p50/p95/p99 latency before and after migration) with metric tabs above it, a one-line caption, and a customer quote. Proof comes from real data, not adjectives.
- **Clutter control:** one font size and one column of about 1100px, with no imagery except the chart and logos.
- **Motion:** essentially none.
- **Borrow:** **one real benchmark chart from a real hive run, with a caption**. It is more credible than 4 stat tiles.

### 8. Cursor: https://cursor.com
- **Background:** `rgb(20,18,11)`, a **warm brown-black** very close to our `#0B0A08`.
- **Hero:** the H1 is only **26px**, left-aligned with 2 buttons, and a huge product window takes the rest of the screen.
- **Rows:** about 1/3 is a text column (an 18px sentence that is "bold title + grey continuation", plus an orange link). About 2/3 is a demo panel placed on a painted landscape backdrop.
- **Clutter control:** the copy is very short and the demo does the talking.
- **Borrow:** confirmation that **warm near-black + one warm accent** reads premium. It also shows that a small H1 works when the visual is strong.

### 9. Supabase: https://supabase.com
- **Hero:** split. The H1 (46/500) sits left, with the second line in accent green. A short paragraph sits right, with 2 buttons. Below is a bento of 7 product tiles, a logo grid, then tabbed dashboard demos.
- **Headings:** H3 34/600 two-tone (white phrase + grey continuation).
- **Clutter note:** the bento tiles are boxed and fairly busy. Use it as a counter-example for hive's "no boxes" rule.
- **Borrow:** the split hero text, with the headline left and a 3-line explainer on the right baseline. This is an alternative to Vercel's micro-list.

### 10. Teenage Engineering OP-1 field: https://teenage.engineering/products/op-1
- **Layout:** a lowercase voice ("the beauty of evolution."), the product photographed **top-down at true scale**, then short centred statements. A centred small-caps stack lists "100 new features". Orthographic views (back, side) come with tiny caps captions, and the product family sits in a **3-column hairline grid** (1px lines, no cards).
- **Type:** light weight 300 throughout, on a strict scale of **13 / 16 / 19 / 26 / 53px**.
- **Clutter control:** grey paper background, photography as hero, and almost no UI chrome.
- **Borrow:** the **hairline grid of hardware** for "parts" (laptop / controller / CLI / dashboard), with each cell holding a clean render and one caption line.

### 11. Rivian R2: https://rivian.com/r2
- **Layout:** a video hero, then "Explore the lineup" with trim tabs (Standard / Premium / Performance + Compare). The car render is below, and under it a **spec strip of 4 numbers** (275 mi, 5.9 sec, 350 hp, RWD), each 36/600 with a tiny label below. Changing the tab swaps the numbers and the car. A sticky sub-nav (Lineup / Performance / Interior / Technology / Charging) and 56–72px statement headings follow.
- **Borrow:** a **tabbed spec strip under the 3D object.** For hive that could be hive size (2 / 4 / 8 laptops) changing the numbers and the honeycomb cluster together.

### 12. Nothing Headphone (1) Pro: https://nothing.tech/products/headphone-1-pro
- **Layout:** the product emerges from pure black with rim light. A thumbnail rail sits on the left and a **sticky bottom product bar** (name / Overview / Specs / price / colour / CTA) runs across. Rows alternate media and text. An exploded or cut-away product image has a waveform.
- **Type:** brand **dot-matrix display font** (Ndot) at 20–40px, NType at 48px, and mono labels at 11–12px.
- **Accent:** **yellow is used only for the active tab** (OVERVIEW / BASS / PRECISION / TREBLE) and the "save" chip.
- **Borrow:** **honey used only for the active state.** Also the exploded view, for later laptop/GPU models.

### 13. Framer: https://www.framer.com
- **Layout:** H1 54/500/−2.16px on the left, then a big canvas demo with a glowing blue frame. Below the demo is a **3-step row** (icon + bold title + grey continuation) in open columns with no borders. Then a "99.99%" stat at 54px, a showcase and a marketplace.
- **Borrow:** the 3-step row with no boxes, a good fit for "how it decides" (split, assign, merge).

### 14. Arc: https://arc.net
- **Status:** it is now a single **farewell page** (Arc sunsets on 22 Dec 2026). The page has one logo, a big serif H1, 2 lines, one button, a long letter in one narrow column, and handwritten team signatures.
- **Borrow:** proof that a single centred column with a serif headline and a human sign-off can feel premium. This could be useful for an "about / why we built hive" page.

### 15. Zirka Interceptor (Awwwards SOTD, Nov 2026): https://www.zrk.technology
- **Stack:** Webflow + **GSAP + ScrollTrigger + Lenis** + 2 canvases, read from the page's scripts.
- **Layout:** a black `#050505` background with a HUD top bar (mono 10px telemetry, a progress tick strip and one orange accent). The H1 is bottom-left. Then a **pinned scroll story**: the 3D hardware stays centred and **one giant number (about 200px light) overlays it**, with the label on the left ("Flight time") and the index on the right ("002"). FPV video sections have HUD numerals, followed by a feature panel at bottom-left.
- **Type:** only 72 / 48 / 32px display sizes, 14px body and 10px mono.
- **Borrow:** a **pinned honeycomb with one giant number per scroll step** for the "numbers" section, plus a quiet mono HUD line (e.g. `nodes 4 · chunks 1,284 · uptime 99.9%`).

---

## 2. Tools

**Fit** is judged against our stack: a static HTML site with three.js r128 loaded from a CDN as a global script, and no build step.

### Motion and scroll
| Tool | What it does | Fit for hive | Cost / licence |
|---|---|---|---|
| **GSAP 3.15 + ScrollTrigger** (gsap.com) | Timelines, scroll-scrubbed and pinned scenes. SplitText for text reveals. | **Yes.** UMD scripts via CDN (`gsap@3.15.0/dist/gsap.min.js`, `ScrollTrigger.min.js`). Use it for pinned "numbers" scenes and for driving the three.js camera with scroll. | **Free incl. all plugins**, under Webflow's "Standard No-Charge" licence (Apr 2025). It forbids use in no-code animation tools that compete with Webflow, which doesn't affect us. |
| **Motion (motion.dev)** | Small `animate()`, `scroll()`, `inView()` and springs for vanilla JS. | **Yes.** The legacy global script `motion/dist/motion.js` is lighter than GSAP for simple reveals. Pin an exact version. | MIT (open-source core). Motion+ is paid. |
| **Lenis 1.3.27** (darkroom.engineering) | Smooth scroll on top of native scroll, so sticky and anchors still work. | **Yes.** `unpkg.com/lenis@1.3.27/dist/lenis.min.js` + `lenis.css`. Hook it into the GSAP ticker. It respects reduced-motion by default. | MIT, free |
| **Theatre.js** | A visual keyframe editor for three.js camera and objects. | **Partial.** It is ESM-first and heavy, and the studio is a dev-only overlay. Only worth it for a long choreographed camera move. | Core Apache-2.0. Studio AGPL-3.0, dev-only (verify). |
| **CSS scroll-driven animations** (`animation-timeline: view()`) | Native scroll reveals with no JS. | **Yes, as progressive enhancement** (Chromium-first). Add a fallback. | Free |

### 3D
| Tool | What it does | Fit for hive | Cost / licence |
|---|---|---|---|
| **three.js r128 `examples/js/*`** | Non-module addons: GLTFLoader, DRACOLoader, OrbitControls, EffectComposer + UnrealBloomPass, RoundedBoxGeometry, BufferGeometryUtils. | **Yes, the native fit.** r128 still ships `examples/js` (removed in r148), loadable as plain scripts from `cdn.jsdelivr.net/npm/three@0.128.0/examples/js/...`. Check each file path exists before use. | MIT |
| **pmndrs: react-three-fiber, drei, postprocessing** | React renderer for three.js, plus helpers and effects. | **No** for runtime: it needs React and a build step. Their examples and sandboxes are still great **reference** for lighting and material recipes. | MIT |
| **Spline** (spline.design) | Browser 3D design tool with its own web runtime. | **Weak.** The runtime is its own engine and doesn't merge with our r128 scene. It could be used to **sketch** a scene or export glTF and then rebuild it in three.js. | Free tier. Code export and paid tiers about $15–30/mo (sources conflict, verify). |
| **Blender 5.3** (installed) | Model and light the hardware (laptops, GPUs, controller), bake lighting, export **glTF/GLB**. | **Yes.** It has a built-in Khronos glTF exporter with Draco compression. Baking light into textures keeps runtime lighting cheap. | GPL, free |
| **glTF-Transform CLI / gltf.report** | Compress, dedupe, resize textures, Draco/Meshopt for GLB. gltf.report is a web inspector. | **Yes,** as an offline step before shipping models. | MIT, free |
| **three.js editor** (threejs.org/editor) | In-browser scene setup and quick material tests. Exports JSON/GLB. | Handy for tuning materials and lights quickly. | MIT, free |

### 2D mascot and micro-animation
Per memory, the bee is 2D (B6), with no 3D.

| Tool | What it does | Fit for hive | Cost / licence |
|---|---|---|---|
| **Rive** (rive.app) | Vector animation with **state machines** (idle / hover / talk / think). Web runtime via a canvas script. | **Best fit for an interactive 2D bee avatar** that reacts to state. | Editor free tier. Export and runtime access may need Cadet ($9/mo) or Pro (verify). Runtime repos are open source. |
| **Lottie** (lottie-web / dotLottie) | Plays After Effects or LottieFiles animations as JSON. | **Yes** for simple looped bee moments (wave, buzz). Less interactive than Rive. | Player MIT. The LottieFiles creator has paid tiers. |

### Design apps and plugins
| Tool | Use | Cost |
|---|---|---|
| **Figma** + contrast plugins (Stark / Contrast) | Check honey on near-black against WCAG/APCA, and lay out the type scale. | Figma free tier. Stark is freemium. |
| **Figma "Typescale"-style plugins** | Generate a modular type scale (e.g. 1.25 or 1.333). | Free |
| **Builder.io Figma-to-code** | Turns a frame into HTML/CSS. It produces a starting point only, and often bloated markup. | Freemium. Low priority. |
| **Easing Wizard / cubic-bezier.com** | Tune easing curves visually. | Free |

### Existing Claude Code skills and plugins
These are read only. I did not install them, because installing via `npx skills add` runs third-party code, which is the user's call.

| Repo / skill | What it gives | Fit | Licence |
|---|---|---|---|
| **anthropics/skills → `frontend-design`** | A studio-critique design process: token plan, a strong "restraint" section, "remove one accessory" before finishing, and anti-cliché lists. | **High.** The closest to what we want. Other skills in the repo: `canvas-design`, `theme-factory`, `brand-guidelines`, `web-artifacts-builder`, `webapp-testing`, `algorithmic-art`. | Per-skill (many Apache-2.0, check LICENSE.txt) |
| **vercel-labs/agent-skills → `web-design-guidelines`** | Audits code against Vercel's Web Interface Guidelines and reports file:line findings. | **High** as a review pass (focus, reduced motion, `transition: all`, contrast, layout shift). | Open source (verify) |
| **greensock/gsap-skills** | Official GSAP agent skills: core, timelines, ScrollTrigger, plugins, vanilla JS, performance. | **High** if we adopt GSAP. | MIT |
| **emilkowalski/skills → `emil-design-eng`** (+ `review-animations`, `improve-animations`) | Animation-decision philosophy (durations, easing, when not to animate). | **Medium-high** for motion review. It is long (about 660 lines). | MIT (per listings) |
| Source text: **vercel-labs/web-interface-guidelines** | Interaction, animation, layout and performance rules as one README. | Good checklist source for our own skill later. | Open source |

---

## 3. How data is arranged on premium pages

Each pattern lists where it was seen and where it fits on hive. The hive sections are: **hero · how it decides · numbers · parts · coming next**.

| # | Pattern | Seen on | Rules that make it work | Use on hive |
|---|---|---|---|---|
| 1 | **Split hero + quiet right rail** | Vercel, Supabase | Headline on the left takes about 40% of the width. The object is centred or right. The rail holds 3 short lines in 14–15px grey, aligned to the headline baseline. | **Hero:** keep the left headline and the 3D honeycomb cluster. Optionally add a 3-line rail at the right edge ("Splits the job · Uses every laptop · Merges the result"). |
| 2 | **Live counter line** | Stripe | One ticking number in small text above the H1, using tabular numerals and no box. | **Hero:** "chunks processed across hives: 1,284,503" in mono 13px above the headline, ticking slowly. |
| 3 | **Two-tone statement paragraph** | Linear, Raycast, Cursor, Supabase | White lead sentence + grey continuation in one 32–48px block, max about 22 words, with no eyebrow pill. | **How it decides:** intro statement. Repeat as the intro for every section instead of eyebrow + H2 + paragraph. |
| 4 | **Sticky index + live panel** | Modal (left index), Nothing (inline tabs), Raycast (icon dock) | A short list of steps on the left (the active one in white or honey, the rest grey) and one live visual on the right that changes per step. | **How it decides:** steps (Split → Score laptops → Assign → Merge) on the left. The honeycomb on the right lights the cells each step touches. |
| 5 | **Field-of-cells data viz** | Modal (GPU grid, hex-dot map) | Many small equal cells. Grey = idle, accent = busy. Movement shows work flowing. No axes needed. | **How it decides / numbers:** honeycomb cells = laptops or chunks. Honey fill = assigned work, a pulse = done. On brand and self-explaining. |
| 6 | **Open stat strip** | Stripe, Rivian, Framer | 3–4 numbers in equal columns with no boxes. Figure 48–64px, light weight (300–500), tabular numerals. Label 13–14px grey, 2 lines max. Only one figure is highlighted. | **Numbers:** e.g. `4x` faster · `0` cloud cost · `8` laptops · `1` command. Honey on the hero figure only. |
| 7 | **Comparison bars** | Apple Mac Studio | Thin bars (4–6px) with labels under them and the number at the bar end. "Ours" in accent, the rest grey. Bars grow on scroll. | **Numbers:** "1 laptop / hive of 2 / hive of 4" render time. Only the hive bars are honey. |
| 8 | **One giant number per screen, pinned over the object** | Zirka (SOTD) | The section pins. The 3D object stays and rotates slightly per step. A ~160–200px light number overlays it, with the label on the left and the index (001/002) on the right. | **Numbers (premium version):** pin the honeycomb and step through 3 numbers. Optional mono HUD line at the top. |
| 9 | **Hairline grid of hardware** | Teenage Engineering, PlanetScale | 3–4 columns separated by 1px lines at about 8% white, not cards. Each cell has a render and one caption line in tiny caps or mono. | **Parts:** laptop app / controller / CLI / dashboard, each a clean render (later Blender → GLB) plus one line. |
| 10 | **Real chart + caption + quote** | PlanetScale | One honest chart with metric tabs above it, a one-sentence caption, and an optional quote under it. | **Numbers:** a real benchmark from a hive run (time vs number of laptops) instead of more tiles. |
| 11 | **Changelog-style list** | Linear, Vercel "Recently shipped" | Stacked rows: bold title, one grey line, and a date or status at the right edge. Hairline between rows, no cards. | **Coming next:** rows like "GPU sharing · next", "Mac support · planned", with the status in mono on the right. |
| 12 | **Tabbed spec strip under the object** | Rivian, Apple workload tabs | A small tab row (the active tab in accent) swaps both the numbers and the 3D state. | **Numbers / parts:** "2 / 4 / 8 laptops" tabs. The honeycomb grows and the numbers update. |

### Type and spacing benchmarks (measured)
| Site | H1 | H2 | Body | Tracking on display |
|---|---|---|---|---|
| Linear | 64 / 510 | 48 / 510 | 15 grey | −2.2% |
| Vercel | 64 / 400 | 56 / 450 | 24 lead, 14 small | −6% |
| Stripe | 48 / 300 | 32–56 / 300 | — | −2% |
| Framer | 54 / 500 | 44 / 500 | — | −4% |
| Modal | 45 / 500 | 38 / 500 | — | −1% |
| Raycast | 64 / 600 | 20 (sentence heads) | 18–20 grey | 0 |
| Cursor | 26 / 400 | 36 | 17 grey 55% | −1.25% |
| Apple Mac Studio | 96 / 700 | — | numbers 48 / 600 | — |
| Zirka | 72 display, 200 numbers | 48 / 32 | 14, mono 10 | — |

**Patterns across these sites:**
- Display line-height is 1.0–1.15.
- Every dark site uses **3 text tones**: about 96% white, about 55–60% grey and about 38% dim.
- Sections are about 1 viewport tall (900–1230px at 1440px).
- Side margins are 24–76px, with a max content width of about 1080–1440px.

---

## Sources
- Linear https://linear.app · Vercel https://vercel.com · Stripe https://stripe.com · Apple https://www.apple.com/mac-studio/ · Raycast https://www.raycast.com · Modal https://modal.com · PlanetScale https://planetscale.com · Cursor https://cursor.com · Supabase https://supabase.com · Teenage Engineering https://teenage.engineering/products/op-1 · Rivian https://rivian.com/r2 · Nothing https://nothing.tech/products/headphone-1-pro · Framer https://www.framer.com · Arc https://arc.net · Zirka https://www.zrk.technology · Awwwards SOTD list https://www.awwwards.com/websites/sites_of_the_day/
- Vercel Web Interface Guidelines https://github.com/vercel-labs/web-interface-guidelines
- Rauno Freiberg, Invisible Details of Interaction Design https://rauno.me/craft/interaction-design
- Emil Kowalski, Great animations https://emilkowal.ski/ui/great-animations
- Josh Comeau, springs https://www.joshwcomeau.com/animation/a-friendly-introduction-to-spring-physics/ and CSS transitions https://www.joshwcomeau.com/animation/css-transitions/
- GSAP ScrollTrigger docs https://gsap.com/docs/v3/Plugins/ScrollTrigger/ · GSAP licence https://gsap.com/community/standard-license/ · https://webflow.com/blog/gsap-becomes-free
- Lenis https://github.com/darkroomengineering/lenis · Motion https://motion.dev/docs/quick-start
- Theatre.js https://github.com/theatre-js/theatre · Spline pricing https://www.spline.design/pricing · Rive pricing https://rive.app/docs/account-admin/pricing.md
- anthropics/skills https://github.com/anthropics/skills (frontend-design SKILL.md) · vercel-labs/agent-skills web-design-guidelines · greensock/gsap-skills https://github.com/greensock/gsap-skills · emilkowalski/skills (emil-design-eng)
