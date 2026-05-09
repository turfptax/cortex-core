# Cortex Core — UX Design Brief

**For AI Design Agents**: Read this document alongside the source code in `src/`. Your job is to redesign the visual experience of this wearable AI pet device — sprites, screen layouts, animations, color palette, and typography. You have creative latitude to propose a visual identity, but must work within the hard technical constraints described below.

---

## Quick Facts

| Property | Value |
|----------|-------|
| Screen | 240 x 280 pixels, ST7789P3 IPS LCD, SPI interface |
| Physical size | ~1.3 inches diagonal (very small, worn on body) |
| Color depth | RGB565 (16-bit, 65,536 colors) |
| Rendering engine | Python PIL (Pillow) — CPU only, no GPU |
| Frame rate | **2 FPS** normal, **15 FPS** during games |
| Sprite size | 48 x 48 pixels (configurable via `PET_SPRITE_SIZE` in `config.py`) |
| Sprite format | PNG with alpha channel (RGBA mode) |
| Fonts | DejaVuSansMono Bold + Regular, sizes 18/14/11 pt |
| Input | 8BitDo Micro gamepad (D-pad, A/B/X/Y, Start/Select) |
| Audio | WM8960 codec, 13 WAV sound effects |
| Battery | PiSugar 3 UPS, percentage + charging shown in UI |
| CPU | ARM Cortex-A53 quad-core (shared with LLM inference, STT, BLE, audio) |

---

## 1. Hardware Constraints

### Display
The ST7789P3 is a 240x280 pixel IPS LCD connected via SPI. It is always in portrait orientation. At roughly 1.3 inches diagonal, pixel density is high — fine detail is possible but everything must remain readable at arm's length (wrist-mounted or clipped to clothing).

### Rendering Pipeline
```
PIL Image.new("RGB", (240, 280))
    -> ImageDraw operations (text, shapes, paste sprites)
    -> Image.tobytes()
    -> Python for-loop converts RGB to RGB565 (bottleneck)
    -> board.draw_image(0, 0, 240, 280, buffer) via SPI
```

The RGB565 conversion happens in `tamagotchi_display.py` `_flush()` method — it iterates every pixel in pure Python:
```python
rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
```

This means: 5 bits red (32 levels), 6 bits green (64 levels), 5 bits blue (32 levels). **Subtle gradients will band.** Bold, distinct colors work best. PIL works in 24-bit RGB internally; quantization only happens at flush time.

### Performance Budget
- **2 FPS** = 500ms per frame. Rendering should complete in under 100ms to leave headroom for LLM inference, audio, BLE, and STT running concurrently.
- **15 FPS** (games only) = 67ms per frame. Very tight — minimize drawing operations.
- `Image.blend()` creates new Image objects (GC pressure). Use sparingly.
- `draw.text()` is relatively expensive — minimize redundant text redraws.
- `draw.rounded_rectangle()` is available (PIL 8.2+).

### Input
8BitDo Micro gamepad: D-pad (up/down/left/right), A, B, X, Y, Start, Select. Physical button on the WhisPlay HAT for short press / long press / 5s shutdown hold. Button hints appear in the footer area of most screens.

---

## 2. Display Layout Architecture

The display is divided into three fixed zones:

```
+--------------------------------------+  x=0
|          STATUS BAR                  |  y=0 to y=19 (20px)
|  [BLE] [---mood bar---] [clock][bat] |
+--------------------------------------+  y=19 (1px separator line)
|                                      |
|                                      |
|           PET ZONE                   |  y=20 to y=199 (180px)
|                                      |
|   Sprite, speech bubbles, menus,     |
|   overlays, transcripts, game        |
|                                      |
|                                      |
+--------------------------------------+  y=200 (1px separator line)
|          INFO BAR                    |  y=200 to y=279 (80px)
|  [H===] [C===]  Vital bars (2x grid) |
|  [E===]         XP bar, IQ score     |
|  ─────────────────────────────────── |  y=248 (footer separator)
|  [A] Talk  [X] Feed  [Y] Clean      |  Button hints (28px)
+--------------------------------------+  y=280
        240 pixels wide
```

### Layout Constants (from `TamagotchiDisplay`)
```python
W = 240       # display width
H = 280       # display height
STATUS_Y = 0  # status bar top
STATUS_H = 20 # status bar height
PET_Y = 20    # pet zone top
PET_H = 180   # pet zone height
INFO_Y = 200  # info bar top
INFO_H = 80   # info bar height
```

### Status Bar Details (y=0 to y=19)
- **Left** (x=8): BLE connection dot — 8px circle, cyan when connected, dark gray when not
- **Center** (x~95): Mood bar — 50px wide, 4px tall, colored fill proportional to mood score (-1 to +1)
- **Right**: Clock string (`HH:MM`) in dim gray, 11pt font
- **Far right**: Battery percentage (e.g. `87%+`) — green >60%, yellow >20%, red <=20%, `+` suffix when charging
- Separated from pet zone by 1px line at y=19 in color (30, 30, 40)

### Pet Zone Details (y=20 to y=199)
Primary content area (180px tall). Used differently by each screen state. On the HOME screen, the sprite is centered:
```python
sprite_x = (240 - 48) // 2  # = 96
sprite_y = 20 + (180 - 48) // 2 - 15  # = 86
```

### Info Bar Details (y=200 to y=279)
- **Top section** (y=200-225): Vital bars in a grid layout. Three bars: Hunger (H), Cleanliness (C), Energy (E). Each has a 1-letter label + 90px progress bar, 4px tall.
- **Middle** (y=230): XP/Evolution progress bar — full width, 8px tall, cyan fill on dark background. Label: `XP: N/NEXT`. IQ score displayed right-aligned (`IQ:XX` in purple).
- **Footer** (y=252-279): Button hints — 1px separator at y=248, then colored `[KEY] Action` pairs at 11pt.

---

## 3. Screen States Reference

There are 13 screen states plus 1 game state. Each section documents the **trigger**, **data used**, **current layout**, **sprite animation**, and **design opportunities**.

### 3.1 HOME (default idle)

**Trigger**: Default state, returned to after most actions.

**Data**: `pet_info` (name, mood, mood_score, stage_name, hunger, cleanliness, energy, intelligence, total_interactions), `battery_info`, `ble_connected`, `time_str`, `idle_since`

**Layout**:
- Status bar (shared)
- Sprite centered in pet zone (48x48 at ~x=96, y=86)
- Pet name centered below sprite (14pt)
- Stage + mood label centered below name (11pt, mood-colored): `"Echoing . happy"`
- Info bar: vital bars, XP bar, IQ score
- Footer hints: `[A] Talk  [X] Feed  [Y] Clean  [star] Menu`

**Sprite**: `idle_{mood}` at 0.5 FPS, looping. After 20 seconds idle, switches to `sleeping` at 0.3 FPS.

**Design opportunities**: The pet zone has 180px of vertical space but only uses ~80px for the sprite + labels. There is room for background decoration, particle effects, ambient animations, environment elements, or a status dashboard surrounding the pet.

### 3.2 MENU

**Trigger**: Gamepad Start or Down on HOME.

**Data**: `menu_items` (list with `.label`, `.is_branch`), `menu_cursor`, `menu_breadcrumb`

**Layout**:
- Mini sprite (24x24, nearest-neighbor downscale) at (8, 24)
- Breadcrumb text at (38, 28) in cyan, 11pt
- Separator line at y=44
- Scrollable list: 7 items per page, 28px per item
- Selected item: highlight background (40,40,60) + cyan arrow `>`
- Scroll indicators: up/down triangles when list overflows

**Sprite**: Continues whatever animation was playing.

**Design opportunities**: Menu items are plain text. Could add icons, category colors, or visual hierarchy. The mini sprite could have a unique "attention" pose.

### 3.3 PET_ASKING (thinking)

**Trigger**: User asks the pet a question (voice or menu).

**Data**: `pet_prompt`

**Layout**:
- Sprite centered at (96, 40)
- "Thinking..." with animated dots (cycles 1-3 dots at 2Hz via `int(time.monotonic() * 2) % 3`)
- User prompt below in 11pt (3 lines max, word-wrapped)
- Footer: `[B] Cancel`

**Sprite**: `thinking` at 1.5 FPS, looping.

**Design opportunities**: The thinking state is where the LLM runs inference (can take seconds). This is a great place for engaging loading animation — particles, orbiting dots, brain activity visualization, progress indication.

### 3.4 PET_RESPONSE (speech bubble)

**Trigger**: Pet engine returns a response.

**Data**: `pet_response_text`, `pet_resp_data` (inference_time_ms, tokens), `pet_info`

**Layout**:
- Sprite LEFT-aligned at (16, 35)
- Speech bubble to the right: rounded rectangle at (72, 28) with left-pointing triangle pointer
- Response text inside bubble (6 lines, 11pt, word-wrapped)
- Stats below bubble: `NNms . NN tokens` in dim gray
- Mood/stage label below stats

**Sprite**: `talking` at 1.0 FPS, looping.

**Design opportunities**: Speech bubble is a standard rounded rect. Could use custom styling, gradient fills, mood-tinted borders. The response text area could have a typewriter reveal effect (showing one character at a time would require state tracking).

### 3.5 STT_LISTENING

**Trigger**: Short press on HOME or gamepad A.

**Data**: `stt_partial`

**Layout**:
- Sprite centered
- Red filled circle + "LISTENING" label in cyan, 14pt
- Partial transcript in quotes below (4 lines, 11pt)
- Command hints in info bar: `"note" . "record" . "pet"` in dim cyan
- Footer: `[B] Cancel`

**Sprite**: `talking` at 0.8 FPS, looping.

**Design opportunities**: The listening state could have a real-time audio waveform visualization, pulsing microphone icon, or audio level indicator instead of just a static dot.

### 3.6 NOTE_TAKING

**Trigger**: User says "note" during STT listening.

**Data**: `note_text`, `stt_partial`, `pet_mode` (boolean — note vs pet question)

**Layout**:
- Rounded badge top-left: "NOTE" (cyan) or "PET" (blue)
- Separator line at y=52
- Live scrolling transcript text, 9 lines visible, auto-scrolls
- Placeholder text when empty: "Speak your note..." or "Ask your pet..."
- Footer: `[Btn] Save` or `[Btn] Send`

**Sprite**: `talking` at 0.8 FPS.

**Design opportunities**: The transcript area is text-only. Could add a visual typing cursor, speech-to-text progress indicator, or waveform background.

### 3.7 PET_STATUS (detailed stats)

**Trigger**: Gamepad Select on HOME, or menu item.

**Data**: Full `pet_info` dict

**Layout**:
- Sprite at (10, 28) — small, left-aligned
- Stats to right: name (14pt), stage, mood + score, interactions count, model name, next stage distance
- XP progress bar
- Mood history bar (full width, mood-colored fill)
- Footer: `[B] Back`

**Design opportunities**: This is an information-dense screen. Could use a card-based layout, iconography for stats, or a mini-dashboard with gauges/meters instead of plain text lists.

### 3.8 CONFIRM_SHUTDOWN

**Trigger**: Menu > Shutdown.

**Layout**: Modal dialog — dark overlay rect (20, 80, 220, 180) with red border. "Shutdown?" in 18pt red, "Are you sure?" in 14pt white. `A: Yes` (red) / `B: No` (green).

**Design opportunities**: Could add a dramatic visual effect — screen tinting, pet looking worried, countdown animation.

### 3.9 RECORDING / 3.10 PAUSED

**Trigger**: Voice command "record" or menu.

**Data**: `session_elapsed`, `segment_elapsed`, `segment_count`, `disk_free`, `remaining_hours`

**Layout**:
- Large badge: "REC" (red) with filled dot, or "PAUSED" (yellow)
- Elapsed time in 18pt: `HH:MM:SS`
- Segment counter, segment progress bar (full width, 10px)
- Disk stats: free space + remaining hours
- Footer: `[Btn] Pause/Resume, [Hold] Stop`

**Design opportunities**: Recording screen could have a VU meter, waveform visualization, or pulsing record indicator. The segment progress bar could be more visually interesting than a flat rectangle.

### 3.11 PET_FEEDING

**Trigger**: Gamepad X on HOME.

**Data**: `pet_info.hunger`

**Layout**: Centered sprite, "Nom nom nom!" in happy green (14pt), hunger bar value below. Auto-dismisses after 2 seconds.

**Sprite**: `talking` at 2.0 FPS (fast mouth animation).

**Design opportunities**: Short screen (2 seconds), but could show food particles, a satisfaction meter, or the `eating` animation (exists but currently unused in this screen — it uses `talking` instead). The eating sprite frames could be properly utilized here.

### 3.12 PET_CLEANING

**Trigger**: Gamepad Y on HOME.

**Data**: `cleaning_interactions`, `cleaning_cursor`, `cleaning_discarded`

**Layout**:
- "DATA CLEANUP" title in blue (14pt)
- Shows bad interactions one-by-one: prompt text (3 lines), sentiment score (colored by severity), progress counter (X/N)
- Cleanliness + discard count in info bar
- Footer: `[A] Discard  [B] Keep  [star] Done`

**Design opportunities**: Currently text-heavy. Could visualize "dirty data" as grime/stains on the pet, with cleaning animations removing them. The sentiment score could be shown as a gauge or icon rather than a number.

### 3.13 PET_COMA

**Trigger**: 2+ vitals critically low (<10%) for 2 continuous hours.

**Data**: `pet_info` (vitals for revival progress)

**Layout**:
- Dimmed sprite (40% opacity via `Image.blend()`) centered
- Animated "z" characters floating up-right (3 positions, phased reveal at 2Hz)
- "Pet is in a deep sleep..." label in dim gray
- Revival progress bars: Hunger/Clean/Energy with threshold markers and check/percentage indicators
- Footer: `[X] Feed  [Y] Clean  [A] Talk`

**Sprite**: `sleeping` at 0.2 FPS, looping.

**Design opportunities**: The coma state should feel dramatic — the pet is in danger. Could use desaturation, flickering, heart-rate-like monitor lines, or a darkened/corrupted visual style. Revival progress could feel like bringing something back to life.

### 3.14 GAME_PONG (rendered separately)

**Note**: Pong renders directly via `pong_renderer.py`, not through `tamagotchi_display.py`. Runs at 15 FPS. Uses the same PIL Image and draw objects. Dashed center line, scores, paddles, ball, AI mode label.

---

## 4. Sprite System Architecture

### File Location
```
src/assets/sprites/         # on development machine
/home/turfptax/cortex-core/src/assets/sprites/  # on Pi
```

### Naming Convention
**Pattern**: `{animation_name}_{frame_index}.png`

The parser in `sprite.py` uses `rsplit("_", 1)` — it splits on the **last** underscore only:
- `idle_happy_0.png` -> animation = `idle_happy`, frame = `0`
- `thinking_2.png` -> animation = `thinking`, frame = `2`

Frame indices are 0-based integers. Gaps in numbering are handled (None values filtered out).

### Current Animations (24 total frames)

| Animation | Frames | FPS (typical) | Loop | Used In |
|-----------|--------|---------------|------|---------|
| `idle_happy` | 0, 1 | 0.5 | yes | HOME (happy mood) |
| `idle_content` | 0, 1 | 0.5 | yes | HOME (content mood) |
| `idle_neutral` | 0, 1 | 0.5 | yes | HOME (neutral mood) |
| `idle_uneasy` | 0, 1 | 0.5 | yes | HOME (uneasy mood) |
| `idle_sad` | 0, 1 | 0.5 | yes | HOME (sad mood) |
| `thinking` | 0, 1, 2 | 1.5 | yes | PET_ASKING |
| `talking` | 0, 1 | 0.8-2.0 | yes | PET_RESPONSE, STT, NOTE, FEEDING |
| `sleeping` | 0, 1 | 0.2-0.3 | yes | Idle timeout, PET_COMA |
| `eating` | 0, 1, 2 | — | — | Available but not used in any screen |
| `evolve` | 0, 1, 2, 3 | — | no | Available but not triggered in UI |

### Image Format Requirements
- **Format**: PNG with alpha channel
- **Color mode**: RGBA (loaded via `Image.open().convert("RGBA")`)
- **Size**: Any input size is auto-resized to `PET_SPRITE_SIZE x PET_SPRITE_SIZE` (default 48x48) using `Image.NEAREST` (nearest-neighbor — preserves pixel art crispness)
- **Transparency**: Alpha channel used for compositing — sprites are pasted with `img.paste(frame, (x, y), frame)` where the third argument is the alpha mask
- **Pixel art is recommended**: Nearest-neighbor scaling means anti-aliased edges become jagged. Crisp pixel art at native resolution works best.

### SpriteAnimator API (`sprite.py`)
```python
animator = SpriteAnimator()                    # loads all PNGs from SPRITE_DIR
animator.play("thinking", fps=1.5, loop=True)  # start animation
animator.tick()                                 # call each main loop iteration
frame = animator.get_frame()                    # -> PIL Image (RGBA, 48x48)
animator.set_mood_idle("happy")                 # convenience for idle_{mood}
animator.has_animation("name")                  # -> bool
animator.current_animation                      # -> str (current anim name)
animator.is_playing                             # -> bool
animator.stop()                                 # freeze on current frame
```

### Adding New Animations
1. Create PNG files: `{name}_0.png`, `{name}_1.png`, etc.
2. Place them in `src/assets/sprites/`
3. They auto-load on `SpriteAnimator()` construction — no code changes needed
4. To use them, add `sprites.play("name", fps=X, loop=True/False)` in the appropriate renderer or state transition in `tamagotchi_display.py`

### Current Sprite Art (placeholder)
The current sprites are **programmatically generated colored circles** with basic face features:
- **Body**: Solid-color ellipse, radius 18px on 48x48 canvas
- **Eyes**: White circles (radius 3px) with dark pupils (radius 2px)
- **Mouth**: PIL `draw.arc()` for smiles/frowns, `draw.line()` for neutral
- **Animations**: Subtle Y-bounce for idle, pupil direction for thinking, mouth open/close for talking
- **Colors per mood**: Happy=(100,220,120), Content=(80,200,160), Neutral=(120,180,220), Uneasy=(200,180,100), Sad=(180,120,100)

These are explicitly temporary placeholders. The `generate_sprites.py` header says: *"These are temporary — replace with proper pixel art later."*

---

## 5. Animation Timing and Effects

### Frame Rate Budget
| Context | FPS | Frame Duration | Budget Per Frame |
|---------|-----|---------------|-----------------|
| Normal UI | 2 | 500ms | ~100ms render target |
| Games | 15 | 67ms | ~40ms render target |

Sprite animations can run at any FPS independent of display refresh. A 0.5 FPS sprite on a 2 FPS display = same frame shown 4 times before advancing.

### Types of Animation in the System

**1. Sprite frame cycling** (handled by `SpriteAnimator`)
- Preloaded PNG frames, time-based advancement via `tick()`
- Used for: all pet animations (idle, thinking, talking, sleeping, eating, evolve)

**2. Procedural text animation** (inline in render methods)
- "Thinking..." dots: `"." * (1 + (int(time.monotonic() * 2) % 3))`
- Produces 1-3 dots cycling at 2Hz

**3. Procedural particle effects** (inline)
- Zzz text in coma screen: 3 positions, phased reveal using `phase = int(time.monotonic() * 2) % 3`
- Each "z" at a different opacity: `200 - i * 50`

**4. Flashing/blinking** (inline)
- Vital bars flash red at 3Hz when critical: `int(time.monotonic() * 3) % 2 == 0`

**5. Image compositing** (PIL operations)
- Coma dimming: `Image.blend(black_bg, sprite, 0.4)` for 40% opacity
- Speech bubble: `draw.rounded_rectangle()` + triangle polygon for pointer

### Performance Notes
- At 2 FPS, each animation frame is visible for 500ms. Animations must read clearly at this speed — no fast motion or rapid changes.
- At 0.5 FPS (idle), each frame is visible for **2 full seconds**. Idle animations should be extremely subtle (breathing, blinking, gentle sway).
- The 48x48 sprite canvas is small. Use bold shapes, high contrast, minimal fine detail.
- Alpha channel allows non-rectangular sprites — tentacles, auras, glow effects, particles extending beyond the body outline.

---

## 6. Color and Typography System

### Color Palette (defined in `config.py` as RGB tuples)

**Background and UI Chrome**
| Constant | RGB | Usage |
|----------|-----|-------|
| `COLOR_BG` | (0, 0, 0) | Screen background |
| `COLOR_BAR_BG` | (40, 40, 40) | Progress bar backgrounds |
| `COLOR_MENU_BG` | (20, 20, 30) | Menu/dialog overlay |
| `COLOR_HIGHLIGHT` | (40, 40, 60) | Selected menu item |
| `COLOR_SPEECH_BG` | (35, 35, 50) | Speech bubble fill |
| `COLOR_XP_BAR_BG` | (30, 30, 45) | XP bar background |
| Separator lines | (30, 30, 40) | Used throughout |

**Text**
| Constant | RGB | Usage |
|----------|-----|-------|
| `COLOR_TEXT` | (255, 255, 255) | Primary text |
| `COLOR_DIM` | (128, 128, 128) | Secondary/hint text |

**Accent and UI**
| Constant | RGB | Usage |
|----------|-----|-------|
| `COLOR_CYAN` | (0, 200, 200) | Primary accent, BLE dot, badges |
| `COLOR_CYAN_DIM` | (0, 60, 60) | Dim cyan for hints |
| `COLOR_CURSOR` | (0, 200, 255) | Menu cursor arrow |
| `COLOR_XP_BAR` | (0, 180, 255) | XP bar fill |

**Semantic**
| Constant | RGB | Usage |
|----------|-----|-------|
| `COLOR_RED` | (255, 0, 0) | Recording, danger, critical |
| `COLOR_GREEN` | (0, 200, 0) | Positive, battery good |
| `COLOR_YELLOW` | (255, 180, 0) | Warning, paused, mid-battery |
| `COLOR_BLUE` | (0, 100, 255) | General accent |

**Pet Mood Colors**
| Constant | RGB | Moods |
|----------|-----|-------|
| `COLOR_PET_HAPPY` | (0, 220, 100) | happy, content |
| `COLOR_PET_NEUTRAL` | (100, 180, 220) | neutral |
| `COLOR_PET_SAD` | (180, 100, 60) | uneasy, sad |

**Vital Bar Colors (normal / low threshold)**
| Vital | Normal | Low (<30%) |
|-------|--------|-----------|
| Hunger | (255, 160, 0) | (255, 80, 0) |
| Cleanliness | (0, 160, 255) | (120, 80, 40) |
| Energy | (255, 220, 0) | (255, 60, 0) |
| Intelligence | (180, 0, 255) | — |

### RGB565 Color Survival Guide

The display uses RGB565 (5 bits R, 6 bits G, 5 bits B). When choosing colors, note:
- **Red and blue** lose the 3 least significant bits (quantized to 32 levels)
- **Green** loses the 2 least significant bits (quantized to 64 levels)
- Colors with values like `(128, 128, 128)` survive well
- Colors like `(7, 3, 7)` will round to `(0, 0, 0)` — very dark colors can disappear
- Gradients with small steps (e.g., 0,1,2,3...) will show visible banding
- **Best practice**: Use colors where R and B are multiples of 8, and G is a multiple of 4

Conversion formula (from `_flush()`):
```python
rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
```

### Typography

| Variable | Font | Size | Usage |
|----------|------|------|-------|
| `font_lg` | DejaVuSansMono-Bold | 18pt | Titles, large numbers |
| `font_md` | DejaVuSansMono (regular) | 14pt | Body text, labels |
| `font_sm` | DejaVuSansMono (regular) | 11pt | Stats, hints, dense info |

At 11pt monospace: roughly **7px per character**, meaning ~30 characters fit in 210px usable width (with 15px margins on each side).

Word wrapping utility available: `_word_wrap(text, font, max_width)` — splits on spaces, measures with `font.getbbox()`.

**Font alternatives**: Any monospace `.ttf` font available on Debian/Ubuntu can be used. The font path is configured in `config.py`. If proposing a new font, ensure it's available via `apt install fonts-*` or can be bundled in the repo.

---

## 7. Pet Identity and Evolution

The pet is an AI companion that lives on a wearable device. It learns through conversations and training data. The visual design should convey intelligence, digital nature, and emotional range. It should feel alive despite the low frame rate.

### Evolution Stages

| Stage | Name | Interactions | Design Suggestion |
|-------|------|-------------|-------------------|
| 0 | Primordial | 0+ | Simplest form — a blob, seed, or pixel cluster. Barely recognizable as a creature. |
| 1 | Babbling | 50+ | Emerging features — eyes appear, basic body shape forming. |
| 2 | Echoing | 200+ | Recognizable creature — has a face, body, distinguishing features. |
| 3 | Responding | 1,000+ | Complex and expressive — details, accessories, more animation frames. |
| 4 | Conversing | 5,000+ | Final form — most detailed, majestic, fully realized character. |

Currently all stages use the same sprite. A major design goal is to create visually distinct forms for each evolution stage.

### Mood System

5 moods derived from a rolling sentiment score (-1.0 to +1.0):

| Mood | Score Range | Current Color | Visual Expression |
|------|------------|---------------|-------------------|
| happy | > 0.5 | (0, 220, 100) | Smile, bright eyes, bouncy |
| content | 0.2 to 0.5 | (0, 220, 100) | Slight smile, relaxed |
| neutral | -0.2 to 0.2 | (100, 180, 220) | Flat mouth, alert eyes |
| uneasy | -0.5 to -0.2 | (180, 100, 60) | Wavy mouth, nervous eyes |
| sad | < -0.5 | (180, 100, 60) | Frown, drooping features |

Each mood has its own idle animation set (`idle_happy`, `idle_content`, etc.). The mood also affects the status bar mood indicator bar color.

### Vitals as Visual Signals

| Vital | Normal | Low (<30%) | Critical (<15%) | Visual Behavior |
|-------|--------|-----------|-----------------|-----------------|
| Hunger | Steady bar | Bar changes to "low" color | 3Hz red flash | Could affect pet appearance (thinner? dimmer?) |
| Cleanliness | Steady bar | Bar changes to "low" color | 3Hz red flash | Could show visual grime/stains on pet |
| Energy | Steady bar | Bar changes to "low" color | 3Hz red flash | Could show drooping/sluggish animation |

**Coma**: When 2+ vitals stay below 10% for 2 continuous hours, the pet enters coma. The model unloads. The pet is visually dimmed and shows sleeping animation. Revival requires feeding and cleaning to bring all vitals above 30%.

---

## 8. Visual Style Direction — Cyberpunk Pixel Art

**The chosen visual direction is Cyberpunk Pixel Art** — a blend of retro pixel art character design with a neon-lit, circuit-aesthetic UI. Think "Tamagotchi from the year 2077."

### Reference Concept
The design owner provided a concept reference image showing:
- A **pixel art cat/robot character** with glowing magenta eyes, centered on a dark background
- **Neon color palette**: cyan (`#00C8FF`), magenta (`#FF00FF`), electric green (`#00FF80`), warm amber (`#FFB000`)
- **Circuit board trace patterns** as subtle background decoration
- **Stat icons** arranged around the character with small pixel-art icons for each vital (Hunger, Happiness, Health, Energy)
- **Dark purple/black background** (`#0A0A1A` to `#1A1020`) with selective neon glow
- **Scanline or CRT overlay** effects for atmosphere
- Overall **240x280 portrait layout** matching our exact hardware

### Core Aesthetic Principles
1. **Dark background is king** — Near-black base (`COLOR_BG`) with neon accents that pop. The darkness makes the glow effects feel real on the IPS LCD.
2. **Pixel art character, tech UI** — The pet sprite itself should be chunky pixel art (works perfectly with nearest-neighbor scaling at 48x48). The surrounding UI (bars, borders, backgrounds) should feel like a cyberpunk HUD.
3. **Neon accent colors** — Cyan, magenta, electric green as primary accents. These survive RGB565 quantization well (`(0, 200, 255)` → still looks great as 5-6-5). Use amber/orange for warnings, red for critical.
4. **Circuit trace backgrounds** — Subtle horizontal/vertical lines with right-angle turns, drawn in very dark colors (`(20, 15, 30)` on a `(10, 10, 20)` background). Cheap to render with `draw.line()`.
5. **Glow simulation** — On a 240px-wide screen, "glow" can be faked by drawing the same element twice: once in a dim color at +1px offset, then in the bright color on top. Or use `Image.blend()` sparingly.
6. **Chunky, readable** — Despite the cyberpunk aesthetic, readability comes first. Text must be crisp. Vital bars must be instantly parseable. The pet's mood must be obvious at a glance.

### Color Direction (suggested neon palette)
```python
# Background / chrome
COLOR_BG = (8, 8, 20)               # deep space blue-black
COLOR_BG_ACCENT = (18, 14, 32)      # slightly lighter for panels
COLOR_SEPARATOR = (40, 20, 60)      # purple-tinted separator lines
COLOR_GRID = (15, 12, 25)           # circuit trace background lines

# Neon accents
COLOR_NEON_CYAN = (0, 200, 255)     # primary UI accent
COLOR_NEON_MAGENTA = (255, 0, 200)  # secondary accent, pet eyes
COLOR_NEON_GREEN = (0, 255, 128)    # positive states, health
COLOR_NEON_AMBER = (255, 180, 0)    # warnings, hunger
COLOR_NEON_RED = (255, 40, 40)      # critical alerts

# Text
COLOR_TEXT = (200, 220, 255)        # cool white (slight blue tint)
COLOR_DIM = (80, 70, 100)           # muted purple-gray
COLOR_HIGHLIGHT = (0, 200, 255)     # cyan for selected items
```

### What This Means for Each Element
- **Pet sprite**: Pixel art creature with 2-3 neon accent colors (glowing eyes, energy lines on body). Dark body with bright highlights. Should feel like a digital being, not a plush toy.
- **Status bar**: Dark background with thin neon separator. BLE dot glows cyan. Clock in cool white. Battery icon with color-coded fill. Mood bar with neon gradient.
- **Vital bars**: Thin neon-outlined bars with glowing fills. Could use small pixel-art icons (🍖⚡🧹🧠) next to each bar for visual distinction beyond just color.
- **Info/footer zone**: Dark panel with subtle circuit trace pattern. Button hints in dim text with neon letter highlights (`[A] Talk`).
- **Speech bubbles**: Semi-transparent dark panels with thin neon border. Text in cool white.
- **Menu**: Dark background with cyan cursor/selection highlight. Menu items glow on hover/select.
- **Backgrounds**: The 180px pet zone should have subtle circuit trace patterns, occasional dim "data rain" particles, or geometric grid lines — all in very muted colors that don't compete with the pet sprite.

### Why This Works for Our Hardware
- **Neon on black** looks stunning on IPS LCDs (high contrast ratio, colors really pop)
- **Pixel art** is the natural fit for 48x48 sprites with nearest-neighbor scaling
- **Dark theme** minimizes the RGB565 banding problem (gradients are less visible in dark ranges)
- **Line-based decoration** (circuit traces, grid) is cheap to render with `draw.line()` — no transparency needed
- **2 FPS is fine** — cyberpunk UIs feel naturally "techy" even with slow updates. A blinking cursor or pulsing glow at 2 FPS feels intentional rather than laggy
- **The contrast between cute pixel pet + cold tech UI** gives the device its unique identity

---

## 9. Design Challenge

### What Exists Today (Problems)
1. **Sprites are placeholder circles** — No character identity, no visual appeal, no evolution progression. Just colored circles with dot eyes and arc mouths.
2. **No background decoration** — The pet zone is 180px of black space with a tiny 48x48 sprite in the center. Feels empty.
3. **Generic dark theme** — Functional but has no visual personality. Could be any device's UI.
4. **Unused animations** — The `eating` and `evolve` sprite animations exist but aren't used in any screen state.
5. **No visual feedback for actions** — Feeding shows "Nom nom nom!" text but no real animation. Cleaning is entirely text-based. Evolution isn't visualized.
6. **No stage-based visuals** — All 5 evolution stages look identical. There's no visual reward for progression.
7. **No environmental context** — The pet exists in a void. No sense of "home" or habitat.

### What Must NOT Change
- The three-zone layout structure (status bar, pet zone, info bar)
- The `render(state: DisplayState)` interface
- The `DisplayState` dataclass fields (can add new optional fields)
- The SpriteAnimator's file-based loading system
- The PIL rendering pipeline and RGB565 flush
- DejaVuSansMono font choice (unless proposing a bundled alternative)
- The 2 FPS frame rate (fundamental to CPU budget)

### What Should Change
Everything else is fair game. You can:
- Redesign the pet character completely
- Create new sprite animations
- Increase `PET_SPRITE_SIZE` (currently 48, could be 64 or larger)
- Add background patterns or environmental elements
- Redesign progress bars, badges, buttons, and UI components
- Add new visual effects (within PIL/CPU constraints)
- Change the color palette
- Propose font size or weight adjustments
- Add new fields to `DisplayState` for visual data (e.g., `background_frame: int`)

---

## 10. Design Deliverables

When you redesign the visuals, produce these specific artifacts:

### A. Sprite Assets
Create PNG files for `src/assets/sprites/`:

**Required animations** (minimum):
- 5 idle mood sets (2+ frames each): `idle_happy_N.png`, `idle_content_N.png`, `idle_neutral_N.png`, `idle_uneasy_N.png`, `idle_sad_N.png`
- Thinking: 3+ frames — `thinking_N.png`
- Talking: 2+ frames — `talking_N.png`
- Sleeping: 2+ frames — `sleeping_N.png`
- Eating: 3+ frames — `eating_N.png`
- Evolving: 4+ frames — `evolve_N.png`

**Optional new animations** (if you want to add them):
- `cleaning_N.png` — for PET_CLEANING screen
- `levelup_N.png` — for stage evolution celebration
- `alert_N.png` — for critical vital warnings
- `waking_N.png` — for coming out of sleep/coma
- `dancing_N.png` — for happy celebrations

All sprites: `PET_SPRITE_SIZE x PET_SPRITE_SIZE` (default 48x48), RGBA PNG with alpha transparency.

### B. Updated `generate_sprites.py`
A script that programmatically generates all sprite frames using PIL (and optionally numpy). Must:
- Produce all required animations with the correct naming convention
- Output to `src/assets/sprites/`
- Be parameterizable (colors, sizes) for future per-stage customization
- Run standalone: `python generate_sprites.py`

### C. Color Palette Update
Updated color constants for `config.py`:
- Must use the same constant names (or document additions)
- Values as RGB tuples, e.g. `COLOR_PET_HAPPY = (R, G, B)`
- Choose colors that survive RGB565 quantization

### D. Layout and Renderer Updates
Changes to `tamagotchi_display.py`:
- Must maintain the `render(state)` method signature
- Must maintain `DisplayState` field access (use `.get(key, default)`)
- Can add new helper methods (`_draw_*()`)
- Can modify any render method (`_render_*()`)
- Can add decorative elements, backgrounds, improved components

### E. Design Rationale
A brief explanation of:
- Why you chose this visual direction
- How it serves the product (wearable AI pet)
- How it works within the constraints (2 FPS, 48x48 sprite, RGB565, PIL rendering)

---

## 11. Code Integration Guide

### Critical File Paths (relative to repo root `cortex-core/`)

| File | Lines | Purpose |
|------|-------|---------|
| `src/config.py` | ~210 | All color constants, `PET_SPRITE_SIZE`, font sizes, display dimensions |
| `src/tamagotchi_display.py` | ~1070 | All 13 screen renderers, layout constants, flush pipeline |
| `src/sprite.py` | ~189 | SpriteAnimator engine — loads PNGs, manages animation state |
| `src/generate_sprites.py` | ~260 | Current placeholder sprite generator |
| `src/display_state.py` | ~72 | DisplayState dataclass — data contract for renderer |
| `src/states.py` | ~762 | State machine — controls which screen is active, builds DisplayState |
| `src/assets/sprites/` | 24 PNGs | Current sprite frames (48x48 each) |
| `src/display.py` | ~650 | Legacy text-based renderer (unused, kept for reference) |
| `src/games/pong_renderer.py` | — | Game rendering (separate system, not part of pet UI) |

### How to Test Locally (without Pi hardware)

Sprites can be generated and previewed on any machine with PIL:
```bash
cd src/
python generate_sprites.py
# View sprites in any image viewer
```

To preview a full display render without hardware, create a test script:
```python
from PIL import Image
from tamagotchi_display import TamagotchiDisplay
from display_state import DisplayState

# Mock board that saves to file instead of SPI
class MockBoard:
    def __init__(self):
        self.last_image = None
    def draw_image(self, x, y, w, h, buf):
        pass  # or reconstruct image from RGB565 buffer
    def set_backlight(self, v):
        pass

board = MockBoard()
display = TamagotchiDisplay(board)
state = DisplayState(app_state="HOME", time_str="14:30", pet_info={
    "name": "Cortex", "mood": "happy", "mood_score": 0.7,
    "stage_name": "Echoing", "hunger": 0.8, "cleanliness": 0.6,
    "energy": 0.9, "intelligence": 42, "total_interactions": 250,
})
display.render(state)
display.img.save("preview_home.png")
```

### How to Deploy to Pi
```bash
# Deploy sprites
scp src/assets/sprites/*.png turfptax@10.0.0.132:~/cortex-core/src/assets/sprites/

# Deploy code changes
scp src/config.py src/tamagotchi_display.py src/generate_sprites.py \
    turfptax@10.0.0.132:~/cortex-core/src/

# Restart service
ssh turfptax@10.0.0.132 "sudo systemctl restart cortex-core"

# Check for errors
ssh turfptax@10.0.0.132 "sudo journalctl -u cortex-core --since '1 min ago' --no-pager"
```

For the Orange Pi (second device):
```bash
scp src/assets/sprites/*.png turfptax@10.0.0.25:~/cortex-core/src/assets/sprites/
scp src/config.py src/tamagotchi_display.py src/generate_sprites.py \
    turfptax@10.0.0.25:~/cortex-core/src/
ssh turfptax@10.0.0.25 "sudo systemctl restart cortex-core"
```

### Naming Conventions
- Sprite files: `{animation}_{frame}.png` — last `_N` is parsed as frame index
- Config colors: `COLOR_` prefix, uppercase `SNAKE_CASE`
- Render methods: `_render_{state_name}()` — one per screen state
- Shared draw helpers: `_draw_{component}()` — for reusable components (status bar, footer, vitals, XP bar)

---

## 12. Example Design Prompts

Use these as starting points for specific design tasks. Each can be given to an AI agent alongside this document and the source code.

### Character Design
> "Design a cyberpunk pixel art character for a 48x48 sprite that represents an AI pet living on a tiny wearable screen. Style: dark body with neon accent lines and glowing eyes (magenta or cyan). It needs 5 mood variants (happy, content, neutral, uneasy, sad) where the glow color/intensity shifts with mood. The character should look good on a near-black background (#080814), have a recognizable silhouette at very small sizes, and feel like a digital creature — a being made of data and light, not a real animal. Generate the `generate_sprites.py` script that produces all frames."

### Full Visual Overhaul
> "Read `docs/UX_DESIGN_BRIEF.md` and `src/tamagotchi_display.py`. Redesign the complete visual experience in the cyberpunk pixel art style described in the brief: new neon-accented sprite character, updated color palette (dark backgrounds, neon cyan/magenta/green accents), circuit trace background patterns, improved screen layouts for all 13 states. Produce updated `generate_sprites.py`, color constants for `config.py`, and modified render methods in `tamagotchi_display.py`."

### Color Palette Redesign
> "Redesign the color palette in `config.py` for a cohesive cyberpunk neon aesthetic. All colors as RGB tuples. Deep blue-black background, neon cyan/magenta/green/amber accents. Ensure all accent colors survive RGB565 quantization (R/B multiples of 8, G multiples of 4). Update mood colors to neon variants, vital bar colors to distinct neon hues, and UI chrome colors to muted purple-grays."

### Screen Layout Improvement
> "Improve the HOME screen (`_render_home()` in `tamagotchi_display.py`). The pet zone has 180px of vertical space but only uses ~80px. Add background decoration, improve vital bar design, and make the XP bar more visually interesting. Stay within 100ms render budget using PIL drawing only."

### Evolution Stage Sprites
> "Create visually distinct sprites for each of the 5 evolution stages (Primordial, Babbling, Echoing, Responding, Conversing). Each stage should look noticeably different, showing progression from a simple blob to a fully realized character. Update `generate_sprites.py` and document how to switch sprites per stage."

### Ambient Background
> "Design a cyberpunk background pattern for the 240x180 pet zone. Use subtle circuit trace lines (right-angle paths in very dark purple/blue on the near-black background), and optionally a faint grid or occasional dim particle. It should be simple enough to render at 2 FPS in PIL using `draw.line()` calls, create atmosphere without competing with the pet sprite, and feel like the pet lives inside a circuit board / data network."

### Loading/Thinking Animation
> "The PET_ASKING state shows 'Thinking...' text while LLM inference runs (1-5 seconds). Design a more engaging loading animation that works at 2 FPS using procedural PIL drawing. Consider: orbiting dots, expanding rings, brain activity pattern, data stream visualization."

---

## Appendix: Current Source Files Reference

For AI agents that want the exact current implementation, read these files in order:

1. `src/config.py` — All constants (colors, sizes, fonts, timing)
2. `src/display_state.py` — Data contract for the renderer
3. `src/sprite.py` — Animation engine
4. `src/generate_sprites.py` — Current placeholder sprite generator
5. `src/tamagotchi_display.py` — All screen renderers (the main file to modify)
6. `src/states.py` — State machine (to understand screen transitions)

The repo is at: `https://github.com/turfptax/cortex-core` (branch: `feature/pet-engine`)
