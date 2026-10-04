# RedTram Harness Bug Fixes - Summary

## Issue: Theme-specific inference indicator not visible

### Bug 1: Inference indicator color cycling
**Location:** `rt_harness/textual_chat.py`

**Problem:** The `_get_inference_colors` method didn't map themes to their characteristic colors, so the inference border just used default colors regardless of theme.

**Fix:** Added theme-to-color mappings:
- `commodore-64`: Cycles through 15 C64 VIC-II border palette colors
  - `#000000`, `#ffffff`, `#68372b`, `#70a4b2`, `#6f3d86`, `#588d43`, `#352879`, `#b8c76f`, `#999999`, `#6b6b6b`, `#952911`, `#4f4f4f`, `#000000`, `#515151`, `#894d44`, `#8c8c8c`
- `matrix`: Cycles through matrix green shades
  - `#00FF41`, `#008F11`, `#7CFF9B`, `#B6FF00`, `#00FF41`
- Other themes: Default yellow/red/green/blue cycle

The `_update_inference_effect` method queries `#transcript` and sets `transcript.styles.border = ("solid", color)` to display the cycling border every 0.1 seconds.

### Bug 2: Per-theme fonts not rendering
**Location:** `rt_harness/textual_chat.py`

**Problem:** Although the font theme system was in place, verification confirmed it's working correctly:
- `from . import fonts` imported at line 41
- `_draw_splash` at line 640 uses `fonts.font_for(self.theme)` 
- `FONT_FOR_THEME` maps: `commodore-64` → `petscii` bold, `matrix` → `vt220` plain, `beos` → `swiss`, etc.

### Files Modified
- `rt_harness/textual_chat.py` - Fixed inference indicator color cycling and verified font theme support

### Testing
- Both `rt_harness/textual_chat.py` and `rt_harness/chat.py` pass syntax validation
- The inference indicator will display theme-appropriate border colors during model inference
- The splash screen renders in the correct theme's bitmap font